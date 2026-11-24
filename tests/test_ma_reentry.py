"""ma_crossover re-entry after a protective exit (opt-in).

Without reentry_after_protective_exit, a position closed by a stop-loss /
take-profit / trailing stop stays flat until the next golden cross even when
the trend is intact. With it, the engine emits BUY while fast SMA > slow SMA
once more than reentry_wait_bars completed bars have passed since the exit
bar. Mirrors AlphaLab's BacktestEngine rule; the cross-repo test at the
bottom checks both land on the same day.
"""

import copy
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from alphalive.strategy.signal_engine import SignalEngine
from alphalive.strategy_schema import StrategySchema

ET = ZoneInfo("America/New_York")
STEADY_RISE = np.r_[np.full(60, 100.0), np.linspace(100, 160, 80)]


def _bars(close):
    close = np.asarray(close, dtype=float)
    idx = pd.DatetimeIndex(
        [
            pd.Timestamp(d).tz_localize(ET)
            for d in pd.bdate_range("2021-01-04", periods=len(close))
        ]
    )
    return pd.DataFrame(
        {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1_000_000.0,
        },
        index=idx,
    )


def _engine(sample_strategy_dict, **params):
    d = copy.deepcopy(sample_strategy_dict)
    d["strategy"]["name"] = "ma_crossover"
    d["strategy"]["parameters"] = {
        "fast_period": 5,
        "slow_period": 20,
        "cooldown_days": 0,
        **params,
    }
    return SignalEngine(StrategySchema(**d))


def test_schema_accepts_and_validates_new_fields(sample_strategy_dict):
    d = copy.deepcopy(sample_strategy_dict)
    d["strategy"]["parameters"] = {
        "fast_period": 5,
        "slow_period": 20,
        "reentry_after_protective_exit": True,
        "reentry_wait_bars": 3,
    }
    StrategySchema(**d)
    d["strategy"]["parameters"]["reentry_wait_bars"] = -1
    with pytest.raises(ValidationError):
        StrategySchema(**d)


def test_disabled_by_default_holds_after_protective_exit(sample_strategy_dict):
    eng = _engine(sample_strategy_dict)
    bars = _bars(STEADY_RISE)
    eng.mark_protective_exit(bars.index[100] + pd.Timedelta(hours=12))
    assert eng.generate_signal(bars.iloc[:110])["signal"] == "HOLD"


def test_reentry_waits_then_buys(sample_strategy_dict):
    eng = _engine(
        sample_strategy_dict, reentry_after_protective_exit=True, reentry_wait_bars=1
    )
    bars = _bars(STEADY_RISE)
    exit_day = 100
    eng.mark_protective_exit(
        bars.index[exit_day] + pd.Timedelta(hours=12)
    )  # intraday exit
    # Morning check on day D sees completed bars through D-1.
    assert (
        eng.generate_signal(bars.iloc[: exit_day + 1])["signal"] == "HOLD"
    )  # 0 bars after
    assert (
        eng.generate_signal(bars.iloc[: exit_day + 2])["signal"] == "HOLD"
    )  # 1 bar after
    sig = eng.generate_signal(bars.iloc[: exit_day + 3])  # 2 bars after > wait 1
    assert sig["signal"] == "BUY"
    assert sig["reason"].startswith("Re-entry after protective exit")


def test_no_reentry_when_trend_reversed(sample_strategy_dict):
    # Exit, then a fall: the short MA crosses under the long MA before the
    # 10-bar wait elapses, so there is never a re-entry (same scenario as
    # AlphaLab's test_no_reentry_after_trend_reverses).
    close = np.r_[
        np.full(60, 100.0), np.linspace(100, 115, 15), np.linspace(114, 80, 25)
    ]
    eng = _engine(
        sample_strategy_dict, reentry_after_protective_exit=True, reentry_wait_bars=10
    )
    bars = _bars(close)
    eng.mark_protective_exit(bars.index[74] + pd.Timedelta(hours=12))
    sigs = [eng.generate_signal(bars.iloc[:k])["signal"] for k in range(76, len(close))]
    assert "BUY" not in sigs
    assert "SELL" in sigs  # the bearish cross that closed the window
    assert eng.get_state()["protective_exit_ts"] is None


def test_ledger_position_and_bearish_cross_close_window(sample_strategy_dict):
    eng = _engine(sample_strategy_dict, reentry_after_protective_exit=True)
    bars = _bars(STEADY_RISE)
    eng.mark_protective_exit(bars.index[100] + pd.Timedelta(hours=12))
    eng.sync_position(True, 150.0)  # re-opened (e.g. by the re-entry BUY filling)
    assert eng.get_state()["protective_exit_ts"] is None

    eng.sync_position(False)
    eng.mark_protective_exit(bars.index[100] + pd.Timedelta(hours=12))
    eng._in_position = False
    # Already in position -> no re-entry BUY even with the window open
    eng.sync_position(True, 150.0)
    assert eng.generate_signal(bars.iloc[:110])["signal"] == "HOLD"


def test_state_round_trip(sample_strategy_dict):
    eng = _engine(sample_strategy_dict, reentry_after_protective_exit=True)
    eng.mark_protective_exit(pd.Timestamp("2021-05-10 11:30"))
    state = eng.get_state()
    assert state["protective_exit_ts"].startswith("2021-05-10T11:30:00")
    fresh = _engine(sample_strategy_dict, reentry_after_protective_exit=True)
    fresh.restore_state(state)
    assert fresh.get_state()["protective_exit_ts"] == state["protective_exit_ts"]


# ------------------------------------------------------------ cross-repo parity
ALPHALAB_BACKEND = (
    Path(__file__).resolve().parent.parent.parent / "AlphaLab" / "backend"
).resolve()
try:
    if ALPHALAB_BACKEND.is_dir() and str(ALPHALAB_BACKEND) not in sys.path:
        sys.path.insert(0, str(ALPHALAB_BACKEND))
    from alphalab.backtest.engine import BacktestEngine  # type: ignore
    from alphalab.strategies.implementations.moving_average_crossover import (  # type: ignore
        MovingAverageCrossover,
    )

    _MA_HAS_REENTRY = (
        "reentry_wait_bars"
        in MovingAverageCrossover({"short_window": 5, "long_window": 20}).params
    )
except Exception:
    _MA_HAS_REENTRY = False


@pytest.mark.skipif(
    not _MA_HAS_REENTRY, reason="AlphaLab sibling checkout with re-entry not available"
)
@pytest.mark.parametrize("wait", [0, 1, 3])
def test_reentry_day_matches_alphalab(sample_strategy_dict, wait):
    """AlphaLab fills a take-profit exit inside bar t (intrabar, like live)
    and re-enters at the open of the first bar after i = t + wait + 1. Live
    AlphaLive exits intraday on day t; its morning check on day D sees bars
    through D-1. Both must re-enter on the same calendar day."""
    close = STEADY_RISE
    lab = pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1e6},
        index=pd.bdate_range("2021-01-04", periods=len(close)),
    )
    lab.attrs["ticker"] = "TEST"
    res = BacktestEngine().run_backtest(
        MovingAverageCrossover(
            {
                "short_window": 5,
                "long_window": 20,
                "volume_confirmation": False,
                "cooldown_days": 0,
                "reentry_after_protective_exit": True,
                "reentry_wait_bars": wait,
            }
        ),
        lab,
        risk_settings={
            "stop_loss_pct": 50.0,
            "take_profit_pct": 10.0,
            "max_position_size_pct": 100.0,
        },
        drawdown_halt_enabled=False,
    )
    days = [str(d)[:10] for d in lab.index]
    sell = next(t for t in res.trades if t["side"] == "sell")
    rebuy = next(t for t in res.trades if t["reason"].startswith("Re-entry"))
    t_exit = days.index(str(sell["timestamp"])[:10])  # exit fills within bar t
    lab_reentry_day = days.index(str(rebuy["timestamp"])[:10])

    eng = _engine(
        sample_strategy_dict, reentry_after_protective_exit=True, reentry_wait_bars=wait
    )
    bars = _bars(close)
    eng.mark_protective_exit(bars.index[t_exit] + pd.Timedelta(hours=12))
    live_day = next(
        d
        for d in range(t_exit + 1, len(close))
        if eng.generate_signal(bars.iloc[:d])["signal"] == "BUY"
    )
    assert live_day == lab_reentry_day


# ------------------------------------------------- live exit path (simulator)
def test_reentry_window_opens_when_close_is_only_accepted(tmp_path, monkeypatch):
    """Real Alpaca acknowledges a market close as accepted/new and fills it a
    moment later. The protective exit must open the re-entry window at that
    point, not only when the close fills inside the submit response, and the
    window must survive the following ledger syncs once the fill lands."""
    import json as _json

    import requests

    from alphalive import main as M
    from alphalive.broker.alpaca_broker import AlpacaBroker
    from alphalive.execution.order_manager import OrderManager
    from alphalive.execution.risk_manager import GlobalRiskManager, RiskManager
    from alphalive.state import BotState
    from tests.broker_sim.alpaca_sim import AlpacaSim
    from tests.test_execution_lifecycle_sim import CONFIG

    monkeypatch.setenv("ALPHALIVE_BROKER_READ_ONLY", "false")
    monkeypatch.setenv("ALPACA_HTTP_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("TRADING_PAUSED", "false")

    cfg = _json.loads(_json.dumps(CONFIG))
    cfg["strategy"]["parameters"] = {
        "fast_period": 10,
        "slow_period": 20,
        "reentry_after_protective_exit": True,
    }
    sc = StrategySchema(**cfg)

    class Notifier:
        def __init__(self):
            self.msgs = []

        def send_message(self, m, parse_mode=None):
            self.msgs.append(m)

        send_alert = send_error_alert = send_message

        def send_trade_notification(self, **k):
            self.msgs.append(str(k))

    class AppCfg:
        dry_run = False

    class MarketData:
        price = 100.0

        def get_current_price(self, ticker):
            return self.price

    with AlpacaSim() as sim:
        monkeypatch.setenv("ALPACA_DATA_URL", sim.url)

        def ctl(path, **body):
            r = requests.post(sim.url + "/sim/" + path, json=body)
            assert r.status_code == 200, r.text

        ctl("price", symbol="AAPL", price=100.0)
        broker = AlpacaBroker("SIMKEY", "SIMSECRET", paper=True, base_url=sim.url)
        assert broker.connect()
        state = BotState(str(tmp_path / "state.json"))
        g = GlobalRiskManager(bot_state=state)
        rm = RiskManager(
            sc.risk, sc.execution, sc.strategy.name, sc.safety_limits, global_risk=g
        )
        rm.attach_state(state, "AAPL")
        g.register_strategy("AAPL", rm)
        n = Notifier()
        om = OrderManager(broker, rm, sc, notifier=n, dry_run=False, state=state)
        engines = {"AAPL": SignalEngine(sc)}
        oms = {"AAPL": om}

        r = om.execute_signal(
            "AAPL", {"signal": "BUY", "reason": "x"}, 100.0, 100_000.0, 0, 0
        )
        ctl(
            "fill",
            client_order_id=sim.client_ids()[-1],
            qty=r["requested_qty"],
            price=100.0,
        )
        om.sync_open_intents()
        M._sync_engines_with_ledger(engines, state)
        assert engines["AAPL"]._in_position

        md = MarketData()
        md.price = 95.0  # through the 2% stop
        ctl("price", symbol="AAPL", price=95.0)
        M._run_exit_checks(broker, md, oms, state, AppCfg, n, g, engines)
        close_coid = sim.client_ids()[-1]
        assert sim.order(close_coid)["status"] == "accepted"  # not filled yet
        assert engines["AAPL"]._protective_exit_ts is not None

        ctl("fill", client_order_id=close_coid, qty=r["requested_qty"], price=95.0)
        for _ in range(2):
            om.sync_open_intents()
            M._sync_engines_with_ledger(engines, state)
            M._run_exit_checks(broker, md, oms, state, AppCfg, n, g, engines)
        assert broker.get_all_positions() == []
        assert engines["AAPL"]._protective_exit_ts is not None
        saved = state.state.get("engine_state", {}).get("AAPL", {})
        assert saved.get("protective_exit_ts") == engines["AAPL"]._protective_exit_ts


@pytest.mark.skipif(
    not _MA_HAS_REENTRY, reason="AlphaLab sibling checkout with re-entry not available"
)
@pytest.mark.parametrize("cooldown", [0, 5, 20])
def test_reentry_day_matches_alphalab_with_risk_cooldown(
    sample_strategy_dict, cooldown
):
    """Same as test_reentry_day_matches_alphalab, but the re-entry BUY also has
    to pass AlphaLive's RiskManager cooldown (execution.cooldown_bars, exported
    from cooldown_days), counted from the original entry's decision bar."""
    from alphalive.execution.risk_manager import RiskManager

    close = STEADY_RISE
    lab = pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1e6},
        index=pd.bdate_range("2021-01-04", periods=len(close)),
    )
    lab.attrs["ticker"] = "TEST"
    res = BacktestEngine().run_backtest(
        MovingAverageCrossover(
            {
                "short_window": 5,
                "long_window": 20,
                "volume_confirmation": False,
                "cooldown_days": cooldown,
                "reentry_after_protective_exit": True,
                "reentry_wait_bars": 1,
            }
        ),
        lab,
        risk_settings={
            "stop_loss_pct": 50.0,
            "take_profit_pct": 10.0,
            "max_position_size_pct": 100.0,
        },
        drawdown_halt_enabled=False,
    )
    days = [str(d)[:10] for d in lab.index]
    entry_decision = days.index(str(res.trades[0]["timestamp"])[:10]) - 1
    sell = next(t for t in res.trades if t["side"] == "sell")
    t_exit = days.index(str(sell["timestamp"])[:10])  # intrabar exit
    lab_reentry_day = days.index(
        str(
            next(t for t in res.trades if t["reason"].startswith("Re-entry"))[
                "timestamp"
            ]
        )[:10]
    )

    d = copy.deepcopy(sample_strategy_dict)
    d["strategy"]["name"] = "ma_crossover"
    d["strategy"]["parameters"] = {
        "fast_period": 5,
        "slow_period": 20,
        "cooldown_days": cooldown,
        "reentry_after_protective_exit": True,
        "reentry_wait_bars": 1,
    }
    d["execution"]["cooldown_bars"] = cooldown  # what AlphaLab's export sends
    sc = StrategySchema(**d)
    eng = SignalEngine(sc)
    rm = RiskManager(sc.risk, sc.execution, sc.strategy.name, sc.safety_limits)
    bars = _bars(close)
    rm.last_trade_bar_ts["TEST"] = bars.index[entry_decision].isoformat()
    eng.mark_protective_exit(bars.index[t_exit] + pd.Timedelta(hours=12))
    live_day = next(
        day
        for day in range(t_exit + 1, len(close))
        if eng.generate_signal(bars.iloc[:day])["signal"] == "BUY"
        and rm.check_cooldown("TEST", bars.index[:day])
    )
    assert live_day == lab_reentry_day


def test_low_volume_death_cross_still_exits(sample_strategy_dict):
    """Mirror of AlphaLab's test: volume_confirmation screens entries only."""
    close = np.r_[
        np.full(40, 100.0), np.linspace(100, 120, 20), np.linspace(119, 95, 20)
    ]
    bars = _bars(close)
    eng = _engine(sample_strategy_dict, volume_confirmation=True, volume_avg_period=20)
    death = next(
        k
        for k in range(41, len(close) + 1)
        if eng.generate_signal(bars.iloc[:k])["signal"] == "SELL"
    )
    quiet = bars.copy()
    quiet.iloc[death - 1, quiet.columns.get_loc("volume")] = 1_000.0
    eng2 = _engine(sample_strategy_dict, volume_confirmation=True, volume_avg_period=20)
    assert eng2.generate_signal(quiet.iloc[:death])["signal"] == "SELL"
