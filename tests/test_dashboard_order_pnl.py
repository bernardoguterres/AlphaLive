from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from dashboard import server


def test_dashboard_trades_match_each_visible_buy_lot_once(monkeypatch):
    def order(number, side, price, quantity=10, status="filled", ticker="AAPL"):
        filled = datetime(2026, 11, 25, number + 10, tzinfo=timezone.utc)
        return SimpleNamespace(
            id=str(number),
            symbol=ticker,
            side=side,
            qty=quantity,
            filled_qty=quantity,
            filled_avg_price=price,
            order_type="market",
            status=status,
            submitted_at=filled,
            updated_at=filled,
            filled_at=filled if status == "filled" else None,
        )

    broker = Mock()
    broker._retry_with_backoff.return_value = [
        order(6, "sell", 125, ticker="MSFT"),
        order(4, "sell", 125),
        order(3, "buy", 120, status="canceled"),
        order(2, "sell", 110),
        order(1, "buy", 100),
    ]
    monkeypatch.setattr(server, "_require_broker", lambda: broker)
    monkeypatch.setattr(server, "_read_state", dict)
    monkeypatch.setattr(server, "_PASSWORD", "")
    response = TestClient(server.app).get("/api/trades")
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["orders"]}
    assert rows["2"]["realized_pnl"] == 100
    assert rows["4"]["realized_pnl"] == 50
    assert rows["6"]["realized_pnl"] is None


@pytest.mark.parametrize(
    "midnight_utc, eastern_day, hours_since_midnight",
    [
        (datetime(2026, 11, 23, 5, tzinfo=timezone.utc), "2026-11-23", 21),
        (datetime(2026, 11, 23, 5, tzinfo=timezone.utc), "2026-11-23", 14),
        (datetime(2026, 7, 20, 4, tzinfo=timezone.utc), "2026-07-20", 21),
    ],
    ids=["winter-after-utc-rollover", "winter-before-utc-rollover", "summer"],
)
def test_dashboard_trades_and_daily_pnl_use_same_eastern_day(
    monkeypatch, midnight_utc, eastern_day, hours_since_midnight
):
    now = midnight_utc + timedelta(hours=hours_since_midnight, minutes=30)

    class HostDate(date):
        @classmethod
        def today(cls):
            return now.date()

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.replace(tzinfo=None) if tz is None else now.astimezone(tz)

    def order(order_id, at, price, side):
        return SimpleNamespace(
            id=order_id,
            symbol="AAPL",
            side=side,
            qty=10,
            filled_qty=10,
            filled_avg_price=price,
            order_type="market",
            status="filled",
            submitted_at=at,
            updated_at=at,
            filled_at=at,
        )

    orders = [
        order("today-sell", midnight_utc + timedelta(hours=14), 110, "sell"),
        order("today-buy", midnight_utc + timedelta(hours=10), 100, "buy"),
        order("prior-buy", midnight_utc - timedelta(hours=3), 90, "buy"),
    ]

    def get_orders(request):
        return [order for order in orders if order.submitted_at > request.after][
            : request.limit
        ]

    broker = Mock()
    broker.trading_client.get_orders.side_effect = get_orders
    broker._retry_with_backoff.side_effect = lambda call, request: call(request)
    monkeypatch.setattr(server, "date", HostDate, raising=False)
    monkeypatch.setattr(server, "datetime", FixedDatetime)
    monkeypatch.setattr(server, "_require_broker", lambda: broker)
    monkeypatch.setattr(server, "_read_state", dict)
    monkeypatch.setattr(server, "_PASSWORD", "")
    assert (
        server._todays_daily_pnl({"daily_pnl_date": eastern_day, "daily_pnl": 100})
        == 100
    )

    response = TestClient(server.app).get("/api/trades")
    assert response.status_code == 200
    listed = response.json()["orders"]
    assert [row["id"] for row in listed] == ["today-sell", "today-buy"]
    assert listed[0]["realized_pnl"] == 100
    assert broker.trading_client.get_orders.call_args.args[0].after == midnight_utc
