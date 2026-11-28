"""
Execution-lifecycle scenarios through the REAL adapter stack against the
local Alpaca REST simulator (tests/broker_sim/alpaca_sim.py):

    OrderManager -> AlpacaBroker -> alpaca-py -> HTTP -> simulator

Orders are NOT filled on submission: each test drives accept / partial /
fill / reject / cancel / timeout explicitly. Real RiskManager and a real
BotState on a tmp file are used throughout. No network beyond 127.0.0.1.
"""

import json
import os
import subprocess
import sys
import textwrap

import pandas as pd
import pytest
import requests

from alphalive.broker.alpaca_broker import AlpacaBroker
from alphalive.execution.order_manager import OrderManager
from alphalive.execution.risk_manager import GlobalRiskManager, RiskManager
from alphalive.state import BotState, StateLockError
from alphalive.strategy_schema import StrategySchema
from tests.broker_sim.alpaca_sim import AlpacaSim

CONFIG = {
    "schema_version": "1.0",
    "strategy": {
        "name": "ma_crossover",
        "parameters": {"fast_period": 10, "slow_period": 20},
    },
    "ticker": "AAPL",
    "timeframe": "1Day",
    "risk": {
        "stop_loss_pct": 2.0,
        "take_profit_pct": 5.0,
        "max_position_size_pct": 10.0,
        "max_daily_loss_pct": 3.0,
        "max_open_positions": 5,
        "portfolio_max_positions": 10,
        "trailing_stop_enabled": False,
        "trailing_stop_pct": 3.0,
        "commission_per_trade": 0.0,
    },
    "execution": {"order_type": "market", "limit_offset_pct": 0.1, "cooldown_bars": 1},
    "safety_limits": {
        "max_trades_per_day": 20,
        "max_api_calls_per_hour": 500,
        "signal_generation_timeout_seconds": 5.0,
        "broker_degraded_mode_threshold_failures": 3,
    },
    "metadata": {
        "exported_from": "AlphaLab",
        "exported_at": "2026-10-18T00:00:00Z",
        "alphalab_version": "0.1.0",
        "backtest_id": "sim",
        "backtest_period": {"start": "2020-01-01", "end": "2024-12-31"},
        "performance": {
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "total_trades": 0,
            "calmar_ratio": 0.0,
        },
    },
}
BUY = {"signal": "BUY", "reason": "sim"}
SELL = {"signal": "SELL", "reason": "sim"}


@pytest.fixture
def sim(monkeypatch):
    # These tests submit orders to the simulator on purpose. Without this,
    # DRY_RUN=true (as CI sets it) arms the read-only transport and every
    # submission is refused before it reaches the simulator.
    monkeypatch.setenv("ALPHALIVE_BROKER_READ_ONLY", "false")
    monkeypatch.setenv("ALPACA_HTTP_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("TRADING_PAUSED", "false")
    with AlpacaSim() as s:
        monkeypatch.setenv("ALPACA_DATA_URL", s.url)
        requests.post(s.url + "/sim/price", json={"symbol": "AAPL", "price": 100.0})
        yield s


def ctl(sim, _control, **body):
    r = requests.post(sim.url + "/sim/" + _control, json=body)
    assert r.status_code == 200, r.text


def build(sim, state_file, overrides=None, exclusive=False):
    cfg = json.loads(json.dumps(CONFIG))
    for k, v in (overrides or {}).items():
        cfg[k].update(v)
    sc = StrategySchema(**cfg)
    broker = AlpacaBroker("SIMKEY", "SIMSECRET", paper=True, base_url=sim.url)
    assert broker.connect()
    state = BotState(str(state_file), exclusive=exclusive)
    g = GlobalRiskManager(bot_state=state)
    rm = RiskManager(
        sc.risk, sc.execution, sc.strategy.name, sc.safety_limits, global_risk=g
    )
    rm.attach_state(state, "AAPL")
    g.register_strategy("AAPL", rm)
    om = OrderManager(broker, rm, sc, notifier=None, dry_run=False, state=state)
    return om, rm, state


def submit(om, signal, price=100.0, equity=100_000.0, bars=None):
    om._recent_order_index.clear()  # isolate from the 60s same-process burst guard
    return om.execute_signal("AAPL", signal, price, equity, 0, 0, current_bar=bars)


def last_coid(sim):
    return sim.client_ids()[-1]


def ledger_qty(state):
    return state.get_open_positions().get("AAPL", {}).get("qty", 0.0)


def test_accepted_partial_full_books_cumulative_fills_once(sim, tmp_path):
    om, rm, state = build(sim, tmp_path / "s.json")
    r = submit(om, BUY)
    assert r["status"] == "success" and r["order_status"] == "submitted"
    assert r["requested_qty"] == 100.0 and r["filled_qty"] == 0.0
    assert ledger_qty(state) == 0.0  # accepted is not filled
    coid = last_coid(sim)

    ctl(sim, "fill", client_order_id=coid, qty=40, price=100.0)
    om.sync_open_intents()
    om.sync_open_intents()  # duplicate observation of the same cumulative fill
    assert ledger_qty(state) == 40.0
    assert state.list_open_intents()[0]["status"] == "partially_filled"

    ctl(sim, "fill", client_order_id=coid, qty=60, price=101.0)
    om.sync_open_intents()
    pos = state.get_open_positions()["AAPL"]
    assert pos["qty"] == 100.0
    assert pos["entry_price"] == pytest.approx(100.6)  # (40*100 + 60*101)/100
    assert state.list_open_intents() == []
    assert rm.trades_today == 1


def test_submitted_intent_with_broker_order_id_is_persisted_to_disk(sim, tmp_path):
    """alpaca-py returns order ids as uuid.UUID; before the adapter coerced
    them to str, every save after submission failed ("Object of type UUID is
    not JSON serializable") and the on-disk intent never got its broker id.
    Found by running run.py against the simulator."""
    om, rm, state = build(sim, tmp_path / "s.json")
    submit(om, BUY)
    on_disk = json.loads((tmp_path / "s.json").read_text())
    (intent,) = on_disk["submission_intents"].values()
    oid = sim.order(last_coid(sim))["id"]
    assert intent["broker_order_id"] == oid and intent["status"] == "submitted"


def test_rejected_and_cancelled_orders_leave_no_phantom_position(sim, tmp_path):
    om, rm, state = build(sim, tmp_path / "s.json")
    submit(om, BUY)
    ctl(sim, "status", client_order_id=last_coid(sim), status="rejected")
    om.sync_open_intents()
    assert ledger_qty(state) == 0.0
    assert state.list_all_intents()[0]["status"] == "rejected"

    submit(om, BUY)
    coid = last_coid(sim)
    ctl(sim, "fill", client_order_id=coid, qty=30, price=100.0)
    ctl(sim, "status", client_order_id=coid, status="canceled")
    om.sync_open_intents()
    assert ledger_qty(state) == 30.0  # the confirmed partial survives the cancel
    assert om.broker.get_position("AAPL").qty == 30.0


def test_timeout_after_broker_accepts_yields_exactly_one_order(sim, tmp_path):
    om, rm, state = build(sim, tmp_path / "s.json")
    # The broker creates the order, then stalls past the 1s client timeout.
    ctl(
        sim,
        "fault",
        path="/v2/orders",
        method="POST",
        mode="accept_then_drop",
        count=1,
        seconds=3,
    )
    r = submit(om, BUY)
    # The retry under the SAME client_order_id hits 409 and recovers the order.
    assert r["status"] == "success"
    assert len(sim.client_ids()) == 1
    ctl(sim, "fill", client_order_id=last_coid(sim), qty=100, price=100.0)
    om.sync_open_intents()
    assert ledger_qty(state) == 100.0


def test_unrecoverable_ambiguity_quarantines_then_abandons_after_grace(
    sim, tmp_path, monkeypatch
):
    om, rm, state = build(sim, tmp_path / "s.json")
    # Every submit attempt fails with 5xx: the broker never creates the order.
    ctl(sim, "fault", path="/v2/orders", method="POST", mode="error500", count=10)
    monkeypatch.setattr("time.sleep", lambda s: None)
    r = submit(om, BUY)
    assert r["status"] == "error"
    assert state.list_all_intents()[0]["status"] == "uncertain"
    blocked = submit(om, BUY)
    assert blocked["status"] == "blocked"  # quarantined: no blind resubmit
    monkeypatch.setenv("ALPHALIVE_UNKNOWN_ORDER_GRACE_SECONDS", "0")
    om.sync_open_intents()
    assert state.list_all_intents()[0]["status"] == "abandoned"
    assert ledger_qty(state) == 0.0


def test_restart_while_order_pending_books_fill_once_on_recovery(sim, tmp_path):
    from alphalive import main as main_module

    om, rm, state = build(sim, tmp_path / "s.json", exclusive=True)
    submit(om, BUY)
    coid = last_coid(sim)
    state.close()  # process "dies" with the order still accepted
    del om, rm, state

    ctl(sim, "fill", client_order_id=coid, qty=100, price=99.5)  # fills while down

    om2, rm2, state2 = build(sim, tmp_path / "s.json", exclusive=True)
    main_module._reconcile_open_submission_intents({"AAPL": om2}, state2, _Notifier())
    main_module._sync_position_ledger(
        om2.broker, state2, _Notifier()
    )  # nothing to adopt
    assert ledger_qty(state2) == 100.0
    assert state2.get_open_positions()["AAPL"]["entry_price"] == pytest.approx(99.5)
    assert rm2.trades_today == 1  # restored from persisted counters, not reset
    om2.sync_open_intents()
    assert ledger_qty(state2) == 100.0


def test_duplicate_and_out_of_order_fill_reports_never_double_count(sim, tmp_path):
    om, rm, state = build(sim, tmp_path / "s.json")
    submit(om, BUY)
    coid = last_coid(sim)
    ctl(sim, "fill", client_order_id=coid, qty=70, price=100.0)
    om.sync_open_intents()
    # stale report (older, smaller cumulative fill) arrives after the newer one
    ctl(
        sim,
        "report",
        client_order_id=coid,
        filled_qty=20,
        filled_avg_price=100.0,
        status="partially_filled",
    )
    om.sync_open_intents()
    assert ledger_qty(state) == 70.0
    ctl(sim, "fill", client_order_id=coid, qty=30, price=100.0)
    for _ in range(3):
        om.sync_open_intents()
    assert ledger_qty(state) == 100.0


def test_buy_sell_then_genuinely_new_buy_is_executed(sim, tmp_path):
    om, rm, state = build(sim, tmp_path / "s.json")
    submit(om, BUY)
    ctl(sim, "fill", client_order_id=last_coid(sim), qty=100, price=100.0)
    om.sync_open_intents()
    submit(om, SELL, price=105.0)
    ctl(sim, "fill", client_order_id=last_coid(sim), qty=100, price=105.0)
    om.sync_open_intents()
    assert ledger_qty(state) == 0.0
    assert rm.daily_pnl == pytest.approx(500.0)

    r = submit(om, BUY, price=90.0)
    assert r["status"] == "success" and not r.get("recovered")
    assert len(sim.client_ids()) == 3  # the third decision reached the broker
    ctl(sim, "fill", client_order_id=last_coid(sim), qty=r["requested_qty"], price=90.0)
    om.sync_open_intents()
    assert ledger_qty(state) == r["requested_qty"]


def test_add_on_buy_respects_cumulative_cap(sim, tmp_path):
    om, rm, state = build(sim, tmp_path / "s.json")
    submit(om, BUY)  # 10% of 100k at $100 = 100 shares
    ctl(sim, "fill", client_order_id=last_coid(sim), qty=100, price=100.0)
    om.sync_open_intents()
    r = submit(om, BUY)
    assert r["status"] == "blocked"  # existing 10k position already uses the cap
    assert len(sim.client_ids()) == 1
    ctl(sim, "price", symbol="AAPL", price=80.0)  # position now worth 8k
    r2 = submit(om, BUY, price=80.0)
    assert r2["status"] == "success"
    assert r2["requested_qty"] == pytest.approx(25.0)  # 2k headroom / $80


def test_repeated_losing_round_trips_trip_breaker_and_loss_limit(sim, tmp_path):
    om, rm, state = build(
        sim, tmp_path / "s.json", {"risk": {"max_daily_loss_pct": 1.0}}
    )
    for i in range(3):
        r = submit(om, BUY, price=100.0)
        assert r["status"] == "success", r
        ctl(
            sim,
            "fill",
            client_order_id=last_coid(sim),
            qty=r["requested_qty"],
            price=100.0,
        )
        om.sync_open_intents()
        ctl(sim, "price", symbol="AAPL", price=96.0)
        submit(om, SELL, price=96.0)
        ctl(
            sim,
            "fill",
            client_order_id=last_coid(sim),
            qty=r["requested_qty"],
            price=96.0,
        )
        om.sync_open_intents()
        ctl(sim, "price", symbol="AAPL", price=100.0)
    assert rm.consecutive_losses == 3
    assert rm.trading_paused_by_circuit_breaker is True
    assert rm.daily_pnl == pytest.approx(-1200.0)  # 3 x 100 sh x -$4
    assert rm.trades_today == 6
    blocked = submit(om, BUY)
    assert blocked["status"] == "blocked"
    assert "loss" in blocked["reason"].lower() or "circuit" in blocked["reason"].lower()
    # persisted: a "restart" restores the same limits
    rm_restarted = RiskManager(
        om.config.risk, om.config.execution, "x", om.config.safety_limits
    )
    rm_restarted.attach_state(state, "AAPL")
    assert rm_restarted.trading_paused_by_circuit_breaker is True
    assert rm_restarted.daily_pnl == pytest.approx(-1200.0)


def test_max_trades_per_day_counts_strategy_orders_but_never_blocks_exits(
    sim, tmp_path
):
    om, rm, state = build(
        sim, tmp_path / "s.json", {"safety_limits": {"max_trades_per_day": 3}}
    )

    def round_leg(signal, price=100.0):
        r = submit(om, signal, price=price)
        assert r["status"] == "success", r
        ctl(
            sim,
            "fill",
            client_order_id=last_coid(sim),
            qty=r["requested_qty"],
            price=price,
        )
        om.sync_open_intents()

    round_leg(BUY)
    round_leg(SELL)
    round_leg(BUY)
    assert rm.trades_today == 3
    # limit reached: the exit is still allowed (exposure-reducing) ...
    round_leg(SELL)
    assert ledger_qty(state) == 0.0
    # ... a new entry is not
    assert submit(om, BUY)["status"] == "blocked"


def test_cooldown_counts_completed_bars_not_window_length(sim, tmp_path):
    om, rm, state = build(sim, tmp_path / "s.json", {"execution": {"cooldown_bars": 2}})
    idx = pd.bdate_range("2026-10-01", periods=200, tz="America/New_York")
    submit(om, BUY, bars=idx)
    ctl(sim, "status", client_order_id=last_coid(sim), status="rejected")
    om.sync_open_intents()
    assert submit(om, BUY, bars=idx)["status"] == "blocked"  # same bar
    assert (
        submit(
            om,
            BUY,
            bars=idx[1:].append(pd.DatetimeIndex([idx[-1] + pd.offsets.BDay(1)])),
        )["status"]
        == "blocked"
    )  # 1 new bar
    two_later = idx[2:].append(
        pd.DatetimeIndex([idx[-1] + pd.offsets.BDay(1), idx[-1] + pd.offsets.BDay(2)])
    )
    assert submit(om, BUY, bars=two_later)["status"] == "success"


def test_close_position_partial_then_full_books_realised_pnl_once(sim, tmp_path):
    om, rm, state = build(sim, tmp_path / "s.json")
    submit(om, BUY)
    ctl(sim, "fill", client_order_id=last_coid(sim), qty=100, price=100.0)
    om.sync_open_intents()
    res = om.close_position("AAPL", "stop loss")
    assert res["status"] == "pending"
    coid = last_coid(sim)
    ctl(sim, "fill", client_order_id=coid, qty=50, price=97.0)
    om.sync_open_intents()
    assert ledger_qty(state) == 50.0
    ctl(sim, "fill", client_order_id=coid, qty=50, price=96.0)
    om.sync_open_intents()
    om.sync_open_intents()
    assert ledger_qty(state) == 0.0
    assert rm.daily_pnl == pytest.approx(-350.0)  # 50*-3 + 50*-4, once
    assert rm.consecutive_losses == 1


def test_second_process_cannot_take_the_same_execution_state(tmp_path):
    state_file = tmp_path / "shared.json"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {os.getcwd()!r})
            from alphalive.state import BotState
            s = BotState({str(state_file)!r}, exclusive=True)
            s.create_submission_intent("AAPL", "BUY", 1, "market")
            print("held", flush=True)
            time.sleep(30)
        """),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(StateLockError):
            BotState(str(state_file), exclusive=True)
        # the holder's durable intent is intact on disk
        assert len(BotState(str(state_file)).list_all_intents()) == 1
    finally:
        holder.kill()
        holder.wait()
    # once the owner is gone (even killed), the lock is free again
    s = BotState(str(state_file), exclusive=True)
    s.close()


class _Notifier:
    def send_alert(self, *a, **k):
        pass

    send_error_alert = send_alert
    send_position_closed_notification = send_alert


# ---------------------------------------------------------------------------
# Batch 2: pause policy, completed bars / adjustment, corporate actions
# ---------------------------------------------------------------------------


def test_pause_cancels_open_entry_order_and_keeps_partial(sim, tmp_path):
    om, rm, state = build(sim, tmp_path / "s.json")
    submit(om, BUY)
    coid = last_coid(sim)
    ctl(sim, "fill", client_order_id=coid, qty=30, price=100.0)
    om.sync_open_intents()
    assert om.cancel_open_entry_orders("dashboard kill switch") == 1
    assert sim.order(coid)["status"] == "canceled"
    assert state.list_open_intents() == []
    assert ledger_qty(state) == 30.0  # the executed part is kept and booked


def _daily_bars(n, end_date, start_price=100.0):
    days = pd.bdate_range(end=end_date, periods=n)
    bars = []
    for i, d in enumerate(days):
        px = start_price + i * 0.1
        bars.append(
            {
                "t": f"{d.date()}T04:00:00Z",
                "o": px,
                "h": px + 1,
                "l": px - 1,
                "c": px,
                "v": 1_000_000,
            }
        )
    return bars


class _FrozenDT(__import__("datetime").datetime):
    frozen = None

    @classmethod
    def now(cls, tz=None):
        return cls.frozen.astimezone(tz) if tz else cls.frozen


def test_market_data_uses_completed_split_adjusted_bars_via_adapter(sim, monkeypatch):
    from zoneinfo import ZoneInfo
    import alphalive.data.market_data as md

    et = ZoneInfo("America/New_York")
    # 260 sessions ending TODAY (2026-10-15, a Thursday); today's bar is partial
    ctl(sim, "bars", symbol="AAPL", bars=_daily_bars(260, "2026-10-15"))
    ctl(sim, "split", symbol="AAPL", ratio=4, date="2026-09-01T00:00:00Z")
    _FrozenDT.frozen = _FrozenDT(2026, 10, 15, 9, 35, tzinfo=et)
    monkeypatch.setattr(md, "datetime", _FrozenDT)

    f = md.MarketDataFetcher("SIMKEY", "SIMSECRET")
    df = f.get_latest_bars("AAPL", "1Day", lookback_bars=200)
    assert str(df.index[-1].date()) == "2026-10-14"  # partial 10-15 bar dropped
    jumps = df["close"].pct_change().abs().max()
    assert jumps < 0.01  # adjusted history: no -75% "crash" at the split
    # the executable price is the RAW post-split trade, not an adjusted bar
    ctl(sim, "price", symbol="AAPL", price=31.0)
    assert f.get_current_price("AAPL") == 31.0

    # after the close, today's bar is complete and used
    f.clear_cache()
    _FrozenDT.frozen = _FrozenDT(2026, 10, 15, 16, 5, tzinfo=et)
    df2 = f.get_latest_bars("AAPL", "1Day", lookback_bars=200)
    assert str(df2.index[-1].date()) == "2026-10-15"


def test_broker_split_rescales_raw_bookkeeping_without_false_exit(sim, tmp_path):
    from alphalive import main as main_module

    om, rm, state = build(
        sim,
        tmp_path / "s.json",
        {
            "risk": {
                "trailing_stop_enabled": True,
                "trailing_stop_pct": 5.0,
                "stop_loss_pct": 10.0,
                "take_profit_pct": 50.0,
            }
        },
    )
    submit(om, BUY)
    ctl(sim, "fill", client_order_id=last_coid(sim), qty=100, price=100.0)
    om.sync_open_intents()
    state.set_position_high("AAPL", 110.0)
    ctl(sim, "price", symbol="AAPL", price=105.0)
    ctl(
        sim, "split", symbol="AAPL", ratio=4, date="2026-10-15T00:00:00Z"
    )  # price -> 26.25

    md = type(
        "MD",
        (),
        {"get_current_price": lambda self, t: om.broker.get_position(t).current_price},
    )()
    n_orders = len(sim.client_ids())
    main_module._run_exit_checks(
        om.broker,
        md,
        {"AAPL": om},
        state,
        type("C", (), {"dry_run": False})(),
        _Notifier(),
        om.risk.global_risk,
        None,
    )
    assert len(sim.client_ids()) == n_orders  # no false trailing-stop/stop-loss exit
    pos = state.get_open_positions()["AAPL"]
    assert pos["qty"] == 400.0 and pos["entry_price"] == pytest.approx(25.0)
    assert state.get_position_high("AAPL") == pytest.approx(27.5)


def test_protective_close_cancels_working_entry_then_closes_confirmed_qty(
    sim, tmp_path
):
    """Regression (2026-10 review): a stop-loss while the entry BUY was still
    partially filled and working sold the partial position while the BUY kept
    filling, leaving the CLOSE "filled but not flat" and quarantined. The
    entry is now cancelled first and only the confirmed quantity is sold."""
    om, rm, state = build(sim, tmp_path / "s.json")
    submit(om, BUY)
    buy_coid = last_coid(sim)
    ctl(sim, "fill", client_order_id=buy_coid, qty=30, price=100.0)
    om.sync_open_intents()

    res = om.close_position("AAPL", "stop loss")
    assert sim.order(buy_coid)["status"] == "canceled"
    close_coid = last_coid(sim)
    assert close_coid != buy_coid and res["status"] == "pending"
    assert float(sim.order(close_coid)["qty"]) == 30.0
    ctl(sim, "fill", client_order_id=close_coid, qty=30, price=97.0)
    om.sync_open_intents()
    assert ledger_qty(state) == 0.0
    assert state.list_open_intents() == []
    assert rm.daily_pnl == pytest.approx(-90.0)


def test_protective_close_does_not_double_sell_while_strategy_sell_works(sim, tmp_path):
    om, rm, state = build(sim, tmp_path / "s.json")
    submit(om, BUY)
    ctl(sim, "fill", client_order_id=last_coid(sim), qty=100, price=100.0)
    om.sync_open_intents()
    submit(om, SELL)
    sell_coid = last_coid(sim)
    n = len(sim.client_ids())

    res = om.close_position("AAPL", "stop loss")
    assert res["status"] == "pending" and "already working" in res["reason"]
    assert len(sim.client_ids()) == n  # no second sell order

    ctl(sim, "fill", client_order_id=sell_coid, qty=100, price=98.0)
    om.sync_open_intents()
    assert om.close_position("AAPL", "stop loss")["fill_status"] == "already_flat"
    assert len(sim.client_ids()) == n


def test_error_after_broker_accepts_never_marks_the_live_order_rejected(
    sim, tmp_path, monkeypatch
):
    """Regression (2026-10 review): a ValueError raised by local bookkeeping
    after the broker accepted the order was classified as a definite
    rejection - the intent went terminal, later fills were never booked and
    a re-signal could place a second order."""
    om, rm, state = build(sim, tmp_path / "s.json")
    real = om._apply_order_progress
    calls = {"n": 0}

    def flaky(intent, order):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("local bookkeeping failure")
        return real(intent, order)

    monkeypatch.setattr(om, "_apply_order_progress", flaky)
    submit(om, BUY)
    coid = last_coid(sim)
    (intent,) = state.list_open_intents()
    assert intent["status"] == "submitted"
    assert intent["broker_order_id"] == sim.order(coid)["id"]

    submit(om, BUY)  # re-signal while the order is still live
    assert len(sim.client_ids()) == 1
    ctl(sim, "fill", client_order_id=coid, qty=100, price=100.0)
    om.sync_open_intents()
    assert ledger_qty(state) == 100.0


# --- read-only transport guard (2026-10-19) ---------------------------------


def _mutations_seen(sim):
    reqs = requests.get(sim.url + "/sim/state").json()["requests"]
    return [
        (r["method"], r["path"])
        for r in reqs
        if r["path"].startswith("/v2/") and r["method"] not in ("GET", "HEAD")
    ]


def test_read_only_transport_blocks_every_broker_mutation(sim, tmp_path, monkeypatch):
    """Even when application logic reaches the broker (dry_run=False here),
    the read-only transport refuses submit/cancel/close before any request
    leaves the process; reads keep working."""
    from alphalive.broker import transport

    monkeypatch.setenv(transport.READ_ONLY_ENV, "true")
    transport.blocked_counts.clear()
    om, rm, state = build(sim, tmp_path / "s.json")
    broker = om.broker

    result = submit(om, BUY)
    assert result["status"] == "error"
    assert "read-only" in result["reason"]
    with pytest.raises(Exception):
        broker.trading_client.cancel_order_by_id("00000000-0000-0000-0000-000000000000")
    with pytest.raises(Exception):
        broker.trading_client.close_position("AAPL")
    assert broker.get_account().equity > 0  # GET still allowed

    assert _mutations_seen(sim) == []
    assert sim.client_ids() == []
    methods = {k[0] for k in transport.blocked_counts}
    assert methods == {"POST", "DELETE"}
    # Definitely never sent: rejected, not quarantined as uncertain, and
    # refused once (no retries of a deterministic block).
    intents = list(state.state["submission_intents"].values())
    assert [i["status"] for i in intents] == ["rejected"]
    assert transport.blocked_counts[("POST", "127.0.0.1", "/v2/orders")] == 1


def test_dry_run_arms_read_only_transport_by_default(monkeypatch):
    from alphalive.broker import transport

    monkeypatch.delenv(transport.READ_ONLY_ENV, raising=False)
    monkeypatch.setenv("DRY_RUN", "true")
    assert transport.broker_read_only()
    monkeypatch.setenv("DRY_RUN", "false")
    assert not transport.broker_read_only()
    monkeypatch.setenv(transport.READ_ONLY_ENV, "true")
    assert transport.broker_read_only()


def test_without_read_only_mutations_still_reach_the_broker(sim, tmp_path, monkeypatch):
    """Guard is opt-in outside dry run: normal paper/live order flow is
    unchanged."""
    from alphalive.broker import transport

    monkeypatch.delenv(transport.READ_ONLY_ENV, raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
    om, rm, state = build(sim, tmp_path / "s.json")
    assert submit(om, BUY)["status"] == "success"
    assert ("POST", "/v2/orders") in _mutations_seen(sim)


@pytest.mark.parametrize("write_fails", [False, True])
def test_restart_during_partial_close_booking_preserves_loss(
    sim, tmp_path, monkeypatch, write_fails
):
    from alphalive import main as main_module

    class SimulatedCrash(BaseException):
        pass

    def crash_after_fill_cursor(_):
        raise SimulatedCrash

    state_path = tmp_path / "s.json"
    om, rm, state = build(sim, state_path, exclusive=True)
    assert submit(om, BUY)["status"] == "success"
    ctl(sim, "fill", client_order_id=last_coid(sim), qty=100, price=100.0)
    om.sync_open_intents()
    assert om.close_position("AAPL", "stop loss")["status"] == "pending"
    close_id = last_coid(sim)
    ctl(sim, "fill", client_order_id=close_id, qty=50, price=97.0)

    if write_fails:
        original_save = state.save

        def fail_partial_fill_write():
            # Fail exactly when the in-memory ledger advances, leaving
            # the previously published state file intact.
            if ledger_qty(state) == 50.0:
                return False
            return original_save()

        monkeypatch.setattr(state, "save", fail_partial_fill_write)
    monkeypatch.setattr(rm, "record_fill_result", crash_after_fill_cursor)
    with pytest.raises(SimulatedCrash):
        om.sync_open_intents()
    assert ledger_qty(state) == 50.0
    assert state.list_open_intents()[0]["filled_qty_applied"] == 50.0
    persisted = json.loads(state_path.read_text())
    position = persisted["open_positions"]["AAPL"]
    assert position["qty"] == (100.0 if write_fails else 50.0)
    assert persisted["daily_pnl"] == (0.0 if write_fails else -150.0)
    state.close()

    om2, rm2, state2 = build(sim, state_path, exclusive=True)
    main_module._reconcile_open_submission_intents({"AAPL": om2}, state2, _Notifier())
    main_module._sync_position_ledger(om2.broker, state2, _Notifier())
    assert ledger_qty(state2) == 50.0
    assert rm2.daily_pnl == pytest.approx(-150.0)
    ctl(sim, "fill", client_order_id=close_id, qty=50, price=96.0)
    om2.sync_open_intents()
    om2.sync_open_intents()
    assert len(sim.client_ids()) == 2
    assert ledger_qty(state2) == 0.0
    assert rm2.consecutive_losses == 1
    assert rm2.daily_pnl == pytest.approx(-350.0)
    assert state2.state["daily_pnl"] == pytest.approx(-350.0)
    assert submit(om2, BUY, equity=10_000.0)["status"] == "blocked"
    assert len(sim.client_ids()) == 2


@pytest.mark.parametrize("has_previous_counters", [False, True])
def test_partial_close_booked_after_trading_day_rollover(
    sim, tmp_path, monkeypatch, has_previous_counters
):
    from datetime import datetime, timedelta

    from alphalive import state as state_module
    from alphalive.execution import risk_manager as risk_module

    om, rm, state = build(sim, tmp_path / "s.json")
    assert submit(om, BUY)["status"] == "success"
    ctl(sim, "fill", client_order_id=last_coid(sim), qty=100, price=100.0)
    om.sync_open_intents()
    assert om.close_position("AAPL", "stop loss")["status"] == "pending"
    close_id = last_coid(sim)
    ctl(sim, "fill", client_order_id=close_id, qty=50, price=97.0)
    om.sync_open_intents()
    assert rm.daily_pnl == pytest.approx(-150.0)

    tomorrow = datetime.now(state_module.ET) + timedelta(days=1)

    class NextDay(datetime):
        @classmethod
        def now(cls, tz=None):
            return tomorrow.astimezone(tz) if tz else tomorrow.replace(tzinfo=None)

    monkeypatch.setattr(state_module, "datetime", NextDay)
    monkeypatch.setattr(risk_module, "datetime", NextDay)
    if not has_previous_counters:
        state.state["risk_counters"].pop("AAPL")
    ctl(sim, "fill", client_order_id=close_id, qty=50, price=96.0)
    om.sync_open_intents()
    om.sync_open_intents()

    assert state.state["daily_pnl"] == pytest.approx(-200.0)
    restored = state.get_risk_counters("AAPL", tomorrow.date().isoformat())
    assert restored["daily_pnl"] == rm.daily_pnl == pytest.approx(-200.0)
    assert restored["trades_today"] == rm.trades_today == 0
    assert restored["consecutive_losses"] == rm.consecutive_losses == 1


def test_order_counter_write_preserves_concurrent_fill_loss(sim, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    state_path = tmp_path / "s.json"
    om, rm, state = build(sim, state_path, exclusive=True)
    assert submit(om, BUY)["status"] == "success"
    ctl(sim, "fill", client_order_id=last_coid(sim), qty=100, price=100.0)
    om.sync_open_intents()
    assert om.close_position("AAPL", "stop loss")["status"] == "pending"
    ctl(sim, "fill", client_order_id=last_coid(sim), qty=50, price=97.0)

    submission_started = Event()
    submission_finished = Event()
    record_fill = rm.record_fill_result

    def count_submission():
        submission_started.set()
        rm.record_order_submitted("AAPL")
        submission_finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = []

        def interleave_submission(fill):
            # The ledger write is complete but the risk manager hasn't
            # adopted it yet. Try persisting another order at this boundary.
            pending.append(pool.submit(count_submission))
            assert submission_started.wait(timeout=2)
            submission_finished.wait(timeout=0.2)
            record_fill(fill)

        monkeypatch.setattr(rm, "record_fill_result", interleave_submission)
        om.sync_open_intents()
        pending[0].result(timeout=2)

    assert rm.daily_pnl == pytest.approx(-150.0)
    assert rm.trades_today == 3
    state.close()

    _, restored, state2 = build(sim, state_path, exclusive=True)
    assert ledger_qty(state2) == 50.0
    assert restored.daily_pnl == pytest.approx(-150.0)
    assert restored.trades_today == 3


@pytest.mark.parametrize(
    "terminal_status", ["canceled", "expired", "filled", "rejected"]
)
def test_stale_terminal_report_does_not_drop_partial_intent(
    sim, tmp_path, terminal_status
):
    path = tmp_path / "s.json"
    om, _, state = build(sim, path, exclusive=True)
    assert submit(om, BUY)["status"] == "success"
    coid = last_coid(sim)
    ctl(sim, "fill", client_order_id=coid, qty=70, price=100)
    om.sync_open_intents()
    assert ledger_qty(state) == 70

    # Two broker lookups briefly return a contradictory terminal snapshot.
    # The simulated order and its actual position are unchanged underneath.
    stale = {
        "client_order_id": coid,
        "filled_qty": 20,
        "filled_avg_price": 100,
        "status": terminal_status,
    }
    ctl(sim, "report", **stale)
    om.sync_open_intents()
    assert ledger_qty(state) == 70
    assert state.get_open_intent("AAPL", "BUY")["client_order_id"] == coid
    ctl(sim, "report", **stale)
    result = submit(om, SELL)
    assert result["status"] == "blocked" and "in flight/unresolved" in result["reason"]
    assert sim.client_ids() == [coid]
    state.close()

    ctl(sim, "fill", client_order_id=coid, qty=30, price=101)
    restored, _, persisted = build(sim, path, exclusive=True)
    from alphalive.main import _reconcile_open_submission_intents

    _reconcile_open_submission_intents({"AAPL": restored}, persisted, _Notifier())
    restored.sync_open_intents()
    assert sim.client_ids() == [coid]
    assert ledger_qty(persisted) == 100.0
    assert persisted.get_open_positions()["AAPL"]["entry_price"] == pytest.approx(
        100.30
    )
    persisted.close()


def test_close_rejects_stale_cancellation_then_recovers_same_order(sim, tmp_path):
    path = tmp_path / "s.json"
    om, _, state = build(sim, path, exclusive=True)
    assert submit(om, BUY)["status"] == "success"
    entry_id = last_coid(sim)
    ctl(sim, "fill", client_order_id=entry_id, qty=100, price=100)
    om.sync_open_intents()
    assert om.close_position("AAPL", "stop loss")["status"] == "pending"
    close_id = last_coid(sim)
    ctl(sim, "fill", client_order_id=close_id, qty=70, price=97)
    om.sync_open_intents()

    ctl(
        sim,
        "report",
        client_order_id=close_id,
        filled_qty=20,
        filled_avg_price=97,
        status="canceled",
    )
    assert om.close_position("AAPL", "stop loss")["status"] == "blocked"
    assert state.get_open_intent("AAPL", "CLOSE")["client_order_id"] == close_id
    assert sim.client_ids() == [entry_id, close_id]
    state.close()

    ctl(sim, "fill", client_order_id=close_id, qty=30, price=96)
    restored, risk, persisted = build(sim, path, exclusive=True)
    restored.sync_open_intents()
    assert ledger_qty(persisted) == 0
    assert risk.daily_pnl == pytest.approx(-330)
    assert persisted.list_open_intents() == []
    assert sim.client_ids() == [entry_id, close_id]
    persisted.close()


def test_partial_cancellation_with_current_quantity_is_terminal(sim, tmp_path):
    om, _, state = build(sim, tmp_path / "s.json")
    assert submit(om, BUY)["status"] == "success"
    coid = last_coid(sim)
    ctl(sim, "fill", client_order_id=coid, qty=70, price=100)
    om.sync_open_intents()

    ctl(sim, "status", client_order_id=coid, status="canceled")
    om.sync_open_intents()
    assert state.get_open_intent("AAPL", "BUY") is None
    assert state.list_all_intents()[-1]["status"] == "cancelled"
    assert ledger_qty(state) == 70


def test_reconciliation_does_not_reopen_terminal_intent(sim, tmp_path):
    om, _, state = build(sim, tmp_path / "s.json")
    assert submit(om, BUY)["status"] == "success"
    coid = last_coid(sim)
    earlier_intent = state.get_open_intent("AAPL", "BUY")
    ctl(sim, "fill", client_order_id=coid, qty=100, price=100)
    om.sync_open_intents()
    assert state.get_open_intent("AAPL", "BUY") is None

    # Another reconciliation took its open-intent snapshot before the fill
    # committed but processes its older response afterward.
    ctl(
        sim,
        "report",
        client_order_id=coid,
        filled_qty=20,
        filled_avg_price=100,
        status="partially_filled",
    )
    assert om._reconcile_intent(earlier_intent)["action"] == "superseded"
    # Even a matching terminal response cannot resume an in-flight caller.
    assert om._reconcile_intent(earlier_intent)["action"] == "superseded"
    assert state.get_open_intent("AAPL", "BUY") is None
    assert ledger_qty(state) == 100


def test_startup_stale_close_does_not_throw_away_cost_basis(sim, tmp_path):
    from alphalive.main import _reconcile_open_submission_intents, _sync_position_ledger

    path = tmp_path / "s.json"
    om, _, state = build(sim, path, exclusive=True)
    assert submit(om, BUY)["status"] == "success"
    entry_id = last_coid(sim)
    ctl(sim, "fill", client_order_id=entry_id, qty=100, price=100)
    om.sync_open_intents()
    assert om.close_position("AAPL", "stop loss")["status"] == "pending"
    close_id = last_coid(sim)
    ctl(sim, "fill", client_order_id=close_id, qty=70, price=97)
    om.sync_open_intents()
    assert ledger_qty(state) == 30
    state.close()

    ctl(sim, "fill", client_order_id=close_id, qty=30, price=96)
    ctl(
        sim,
        "report",
        client_order_id=close_id,
        filled_qty=20,
        filled_avg_price=97,
        status="canceled",
    )
    restored, risk, persisted = build(sim, path, exclusive=True)
    _reconcile_open_submission_intents({"AAPL": restored}, persisted, _Notifier())
    _sync_position_ledger(restored.broker, persisted, _Notifier())
    restored.sync_open_intents()
    assert ledger_qty(persisted) == 0
    assert sim.client_ids() == [entry_id, close_id]
    assert risk.daily_pnl == pytest.approx(-330)
    persisted.close()


def test_startup_does_not_adopt_shares_a_pending_buy_will_book(sim, tmp_path):
    from alphalive.main import _reconcile_open_submission_intents, _sync_position_ledger

    path = tmp_path / "s.json"
    om, _, state = build(sim, path, exclusive=True)
    assert submit(om, BUY)["status"] == "success"
    coid = last_coid(sim)
    state.close()

    ctl(sim, "fill", client_order_id=coid, qty=100, price=99)
    ctl(
        sim,
        "report",
        client_order_id=coid,
        filled_qty=0,
        filled_avg_price=0,
        status="accepted",
    )
    restored, _, persisted = build(sim, path, exclusive=True)
    _reconcile_open_submission_intents({"AAPL": restored}, persisted, _Notifier())
    _sync_position_ledger(restored.broker, persisted, _Notifier())
    restored.sync_open_intents()
    assert ledger_qty(persisted) == 100
    assert persisted.get_open_positions()["AAPL"]["entry_price"] == pytest.approx(99)
    assert not persisted.list_open_intents()
    assert sim.client_ids() == [coid]
    persisted.close()


@pytest.mark.parametrize(
    "lookup_raises", [True, False], ids=["late-error", "late-not-found"]
)
def test_delayed_lookup_failure_does_not_reopen_terminal(
    sim, tmp_path, monkeypatch, lookup_raises
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    path = tmp_path / "s.json"
    om, _, state = build(sim, path)
    assert submit(om, BUY)["status"] == "success"
    intent_id = state.get_open_intent("AAPL", "BUY")["intent_id"]
    coid = last_coid(sim)
    original = om.broker.get_order_by_client_id
    started = Event()
    finished = Event()

    def lookup(order_id):
        if not started.is_set():
            started.set()
            assert finished.wait(5)
            if lookup_raises:
                raise RuntimeError("simulated late transport failure")
            return None
        return original(order_id)

    monkeypatch.setattr(om.broker, "get_order_by_client_id", lookup)
    with ThreadPoolExecutor(max_workers=1) as pool:
        earlier = pool.submit(om.sync_open_intents)
        try:
            assert started.wait(5)
            ctl(sim, "fill", client_order_id=coid, qty=100, price=100)
            om.sync_open_intents()
            assert state.get_open_intent("AAPL", "BUY") is None
        finally:
            finished.set()
        assert earlier.result(timeout=5)[0]["action"] == "superseded"
    restored = BotState(str(path))
    assert ledger_qty(restored) == 100
    assert restored.get_open_intent("AAPL", "BUY") is None
    assert restored.get_submission_intent(intent_id)["status"] == "filled"
    assert sim.client_ids() == [coid]


def test_recovered_order_starts_a_fresh_missing_grace_period(
    sim, tmp_path, monkeypatch
):
    from datetime import datetime, timedelta
    from unittest.mock import Mock
    from zoneinfo import ZoneInfo

    from alphalive.execution import order_manager

    om, _, state = build(sim, tmp_path / "s.json")
    assert submit(om, BUY)["status"] == "success"
    coid = last_coid(sim)
    lookup = om.broker.get_order_by_client_id
    monkeypatch.setattr(om.broker, "get_order_by_client_id", lambda _: None)
    now = datetime(2026, 11, 27, 9, 45, tzinfo=ZoneInfo("America/New_York"))
    clock = Mock(wraps=datetime)
    clock.now.return_value = now
    monkeypatch.setattr(order_manager, "datetime", clock)
    monkeypatch.setenv("ALPHALIVE_UNKNOWN_ORDER_GRACE_SECONDS", "60")

    [first] = om.sync_open_intents()
    assert first["action"] == "quarantine"
    monkeypatch.setattr(om.broker, "get_order_by_client_id", lookup)
    [recovered] = om.sync_open_intents()
    assert recovered["action"] == "in_flight"
    # The broker recognized the order between outages; a new 404 has age 0.
    clock.now.return_value = now + timedelta(seconds=61)
    monkeypatch.setattr(om.broker, "get_order_by_client_id", lambda _: None)
    [second] = om.sync_open_intents()
    assert second["action"] == "quarantine"
    assert state.get_open_intent("AAPL", "BUY") is not None
    assert sim.client_ids() == [coid]
