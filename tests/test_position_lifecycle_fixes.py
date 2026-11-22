"""Regression tests for position-lifecycle bugs found in the November 2026 audit.

L1: a position closed outside the bot (e.g. by hand while the bot was down)
left its trailing-stop high and entry timestamp behind; the next entry
inherited them and was sold on the first exit check ("Trailing stop hit
(high $200.00, now $150.00)").
"""

import json

import pytest
import requests

from alphalive import main as M
from alphalive.broker.alpaca_broker import AlpacaBroker
from alphalive.execution.order_manager import OrderManager
from alphalive.execution.risk_manager import GlobalRiskManager, RiskManager
from alphalive.state import BotState
from alphalive.strategy.signal_engine import SignalEngine
from alphalive.strategy_schema import StrategySchema
from tests.broker_sim.alpaca_sim import AlpacaSim
from tests.test_execution_lifecycle_sim import CONFIG


class _Notifier:
    def __init__(self):
        self.msgs = []

    def send_message(self, m, parse_mode=None):
        self.msgs.append(m)

    send_alert = send_error_alert = send_message

    def send_trade_notification(self, **k):
        self.msgs.append(str(k))


class _AppCfg:
    dry_run = False


class _MarketData:
    def __init__(self, price):
        self.price = price

    def get_current_price(self, ticker):
        return self.price


@pytest.fixture
def sim(monkeypatch):
    monkeypatch.setenv("ALPHALIVE_BROKER_READ_ONLY", "false")
    monkeypatch.setenv("ALPACA_HTTP_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("TRADING_PAUSED", "false")
    with AlpacaSim(cash=1000.0) as s:
        monkeypatch.setenv("ALPACA_DATA_URL", s.url)
        yield s


def _ctl(sim, path, **body):
    r = requests.post(sim.url + "/sim/" + path, json=body)
    assert r.status_code == 200, r.text


def _stack(sim, state_file):
    cfg = json.loads(json.dumps(CONFIG))
    cfg["risk"].update(
        trailing_stop_enabled=True,
        trailing_stop_pct=5.0,
        stop_loss_pct=15.0,
        take_profit_pct=50.0,
        max_position_size_pct=50.0,
    )
    sc = StrategySchema(**cfg)
    broker = AlpacaBroker("SIMKEY", "SIMSECRET", paper=True, base_url=sim.url)
    assert broker.connect()
    state = BotState(str(state_file), exclusive=True)
    g = GlobalRiskManager(bot_state=state)
    rm = RiskManager(
        sc.risk, sc.execution, sc.strategy.name, sc.safety_limits, global_risk=g
    )
    rm.attach_state(state, "AAPL")
    g.register_strategy("AAPL", rm)
    n = _Notifier()
    om = OrderManager(broker, rm, sc, notifier=n, dry_run=False, state=state)
    return broker, state, g, n, {"AAPL": om}, {"AAPL": SignalEngine(sc)}


def _buy(sim, om, price):
    om._recent_order_index.clear()
    r = om.execute_signal("AAPL", {"signal": "BUY", "reason": "x"}, price, 1000.0, 0, 0)
    _ctl(
        sim,
        "fill",
        client_order_id=sim.client_ids()[-1],
        qty=r["requested_qty"],
        price=price,
    )
    om.sync_open_intents()
    return r["requested_qty"]


def _strategy_sells(sim):
    return [
        o
        for o in sim.state.orders.values()
        if o["side"] == "sell" and o["client_order_id"] != "manual-1"
    ]


def test_manual_close_while_down_does_not_leave_a_stale_high(sim, tmp_path):
    sf = tmp_path / "state.json"
    broker, state, g, n, oms, engs = _stack(sim, sf)
    _ctl(sim, "price", symbol="AAPL", price=200.0)
    qty = _buy(sim, oms["AAPL"], 200.0)
    M._run_exit_checks(broker, _MarketData(200.0), oms, state, _AppCfg, n, g, engs)
    assert state.get_position_high("AAPL") == 200.0
    state.close()

    # Bot down; position closed by hand at the broker.
    broker.place_market_order("AAPL", qty, "sell", client_order_id="manual-1")
    _ctl(sim, "fill", client_order_id="manual-1", qty=qty, price=160.0)

    # Restart, same startup order as main().
    broker, state, g, n, oms, engs = _stack(sim, sf)
    M._reconcile_open_submission_intents(oms, state, n)
    M._sync_position_ledger(broker, state, n)
    assert state.get_open_positions() == {}
    assert state.get_position_high("AAPL") is None
    assert "AAPL" not in state.state.get("entry_timestamps", {})

    # New entry at 150: the first exit check (price unchanged) must not sell it.
    _ctl(sim, "price", symbol="AAPL", price=150.0)
    _buy(sim, oms["AAPL"], 150.0)
    M._run_exit_checks(broker, _MarketData(150.0), oms, state, _AppCfg, n, g, engs)
    assert _strategy_sells(sim) == []


def test_fresh_entry_resets_a_stale_high_even_without_startup_cleanup(sim, tmp_path):
    """Backstop: a BUY fill that opens a position from flat starts the
    trailing-stop high and entry time fresh, whatever was left in state."""
    broker, state, g, n, oms, engs = _stack(sim, tmp_path / "state.json")
    state.set_position_high("AAPL", 200.0)
    state.record_entry("AAPL")
    old_entry = state.state["entry_timestamps"]["AAPL"]

    _ctl(sim, "price", symbol="AAPL", price=150.0)
    _buy(sim, oms["AAPL"], 150.0)
    assert state.get_position_high("AAPL") in (None, 150.0)
    assert state.state["entry_timestamps"]["AAPL"] >= old_entry

    M._run_exit_checks(broker, _MarketData(150.0), oms, state, _AppCfg, n, g, engs)
    assert _strategy_sells(sim) == []


def test_fill_result_reports_opened_from_flat(tmp_path):
    state = BotState(str(tmp_path / "s.json"))
    intent = state.create_submission_intent("AAPL", "BUY", 2.0, "market")
    first = state.apply_intent_fill_progress(intent["intent_id"], 1.0, 100.0)
    second = state.apply_intent_fill_progress(intent["intent_id"], 2.0, 100.0)
    assert first["opened_from_flat"] is True
    assert second["opened_from_flat"] is False


# --------------------------------------------------------------------------
# L3: cost-safety halts (max trades/day, API calls/hour) used to set the
# manual-pause flag, which neither the daily reset nor /resume cleared.
def _risk_manager(max_trades=3, max_calls=500):
    from datetime import timedelta

    cfg = json.loads(json.dumps(CONFIG))
    cfg["safety_limits"]["max_trades_per_day"] = max_trades
    cfg["safety_limits"]["max_api_calls_per_hour"] = max_calls
    sc = StrategySchema(**cfg)
    rm = RiskManager(sc.risk, sc.execution, sc.strategy.name, sc.safety_limits)
    return rm, timedelta


def test_max_trades_halt_lifts_at_the_daily_reset(monkeypatch):
    monkeypatch.setenv("TRADING_PAUSED", "false")
    rm, timedelta = _risk_manager(max_trades=3)
    rm.trades_today = 3
    assert rm.can_trade("AAPL", "BUY", 100_000.0, 0, 0)[0] is False
    assert not getattr(rm, "trading_paused_manual", False)  # no latch
    assert (
        rm.can_trade("AAPL", "SELL", 100_000.0, 1, 1)[0] is True
    )  # exits never blocked

    rm.last_reset_date = __import__("datetime").date(2000, 1, 1)  # a later trading day
    rm.reset_daily()
    assert rm.trades_today == 0
    assert rm.can_trade("AAPL", "BUY", 100_000.0, 0, 0) == (True, "OK")


def test_api_budget_halt_lifts_at_the_top_of_the_hour(monkeypatch):
    monkeypatch.setenv("TRADING_PAUSED", "false")
    rm, timedelta = _risk_manager(max_calls=10)
    rm.api_calls_this_hour = 10
    assert rm.can_trade("AAPL", "BUY", 100_000.0, 0, 0)[0] is False
    assert not getattr(rm, "trading_paused_manual", False)
    rm.last_hour_reset = rm.last_hour_reset - timedelta(hours=1)  # an hour later
    assert rm.can_trade("AAPL", "BUY", 100_000.0, 0, 0) == (True, "OK")
    assert rm.api_calls_this_hour == 0


# --------------------------------------------------------------------------
# L2: a strategy SELL was dropped for the day when the latest-trade price
# fetch failed, although a SELL sells the held quantity.
def _signal_check(signal, positions):
    from datetime import datetime
    from unittest import mock

    import pandas as pd

    cfg = json.loads(json.dumps(CONFIG))
    sc = StrategySchema(**cfg)
    idx = pd.date_range("2026-01-01", periods=60, freq="B", tz="America/New_York")
    df = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e6},
        index=idx,
    )
    md = mock.Mock()
    md.get_latest_bars.return_value = df
    md.get_current_price.side_effect = Exception("latest trade unavailable")
    eng = mock.Mock()
    eng.generate_signal.return_value = {
        "signal": signal,
        "confidence": 1.0,
        "reason": "x",
    }
    eng.get_state.return_value = {}
    om = mock.Mock()
    om.risk.signal_timeout_seconds = 5
    om.execute_signal.return_value = {
        "status": "success",
        "order_id": "o",
        "fill_status": "accepted",
    }
    broker = mock.Mock()
    broker.get_all_positions.return_value = positions
    broker.get_account.return_value = mock.Mock(equity=110.0)
    import tempfile, os

    st = BotState(os.path.join(tempfile.mkdtemp(), "s.json"))
    st.record_position_open("AAPL", 0.33, 100.0)
    done = set()
    M._check_signal_for_strategy(
        sc,
        datetime(2026, 11, 20, 9, 36, tzinfo=M.ET),
        done,
        {},
        md,
        {"AAPL": eng},
        {"AAPL": om},
        None,
        broker,
        _Notifier(),
        mock.Mock(),
        st,
    )
    return om, done


def test_sell_survives_price_fetch_failure_using_broker_mark():
    from unittest import mock

    om, _ = _signal_check(
        "SELL", [mock.Mock(symbol="AAPL", current_price=98.5, qty=0.33)]
    )
    assert om.execute_signal.called
    assert om.execute_signal.call_args.kwargs["current_price"] == 98.5


def test_sell_falls_back_to_last_close_without_a_mark():
    om, _ = _signal_check("SELL", [])
    assert om.execute_signal.called
    assert om.execute_signal.call_args.kwargs["current_price"] == 100.0


def test_buy_still_needs_a_live_price():
    """Unchanged behaviour: a BUY can't be sized without a live price, so no
    order is placed; the check is only marked attempted in memory (no durable
    daily-eval marker), as for any failed evaluation."""
    om, done = _signal_check("BUY", [])
    assert not om.execute_signal.called
