from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

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
