"""Reporting-script trade matching (L8/L9, Nov 2026 audit).

The performance tracker, trade journal and weekly report crashed on every run
(QueryOrderStatus.FILLED doesn't exist in alpaca-py 0.28) and, had they run,
paired one SELL with one whole BUY lot."""

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from alphalive.utils.fill_matching import (
    fetch_filled_orders,
    match_fills_fifo,
    open_quantities,
)

T0 = datetime(2026, 1, 5, 10, 0)


def fill(side, qty, price, minutes, symbol="AAPL", oid=None):
    return {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "fill_price": price,
        "filled_at": T0 + timedelta(minutes=minutes),
        "id": oid,
    }


def test_add_on_buy_is_matched_by_quantity():
    fills = [
        fill("buy", 10, 100, 0, oid="b1"),
        fill("buy", 5, 110, 1, oid="b2"),
        fill("sell", 15, 120, 2, oid="s1"),
    ]
    trades = match_fills_fifo(fills)
    assert len(trades) == 1
    assert trades[0]["pnl"] == pytest.approx(250.0)  # 10*20 + 5*10 (old code: 200)
    assert trades[0]["qty"] == 15
    assert trades[0]["entry_price"] == pytest.approx(1550 / 15)
    assert trades[0]["buy_order_ids"] == ["b1", "b2"]
    assert open_quantities(fills) == {}  # old code reported 1 open position


def test_partial_sell_uses_the_sold_quantity():
    fills = [fill("buy", 10, 100, 0), fill("sell", 4, 120, 1)]
    trades = match_fills_fifo(fills)
    assert trades[0]["pnl"] == pytest.approx(80.0)  # old code: 200
    assert open_quantities(fills) == {"AAPL": 6}


def test_symbols_are_matched_separately_and_orphan_sells_skipped():
    fills = [
        fill("sell", 5, 50, 0, symbol="MSFT"),  # position predates the window
        fill("buy", 1, 100, 1),
        fill("buy", 2, 300, 2, symbol="MSFT"),
        fill("sell", 1, 90, 3),
        fill("sell", 2, 330, 4, symbol="MSFT"),
    ]
    trades = match_fills_fifo(fills)
    assert [(t["symbol"], round(t["pnl"], 6)) for t in trades] == [
        ("AAPL", -10.0),
        ("MSFT", 60.0),
    ]


def test_fetch_requests_closed_orders_and_keeps_filled_ones():
    from alpaca.trading.enums import QueryOrderStatus

    seen = {}

    class Client:
        def get_orders(self, filter):
            seen["status"] = filter.status
            return [
                SimpleNamespace(filled_qty="2"),
                SimpleNamespace(filled_qty="0"),
                SimpleNamespace(filled_qty=None),
            ]

    orders = fetch_filled_orders(Client(), after=T0)
    assert seen["status"] == QueryOrderStatus.CLOSED
    assert len(orders) == 1


@pytest.mark.parametrize(
    "name", ["performance_tracker", "generate_trade_journal", "automated_weekly_report"]
)
def test_report_scripts_import_and_no_longer_use_filled_status(name):
    path = Path(__file__).resolve().parent.parent / "scripts" / f"{name}.py"
    assert "QueryOrderStatus.FILLED" not in path.read_text()
    spec = importlib.util.spec_from_file_location(name, path)
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
