"""ReplaySimulator must evaluate stop-loss / take-profit / trailing-stop exits.

Regression: the replay passed side.lower() ("buy") to RiskManager, which only
accepts "long"/"short", so every exit check logged "Invalid side" and returned
False - risk exits never fired in replay. Offline only (no broker, no network).
"""

from unittest.mock import MagicMock

import pandas as pd
import pytest

from alphalive.execution.risk_manager import RiskManager
from alphalive.replay import ET, ReplaySimulator
from alphalive.strategy_schema import Execution, Risk
from tests.conftest import sample_strategy_config  # noqa: F401  (fixture)


@pytest.fixture
def rm(sample_strategy_config):  # noqa: F811
    risk = Risk(
        stop_loss_pct=2.0,
        take_profit_pct=5.0,
        max_position_size_pct=10.0,
        max_daily_loss_pct=3.0,
        max_open_positions=5,
        portfolio_max_positions=10,
        trailing_stop_enabled=False,
    )
    return RiskManager(
        risk_config=risk,
        execution_config=Execution(order_type="market", cooldown_bars=1),
        strategy_name="replay_test",
        safety_limits=sample_strategy_config.safety_limits,
    )


def _sim(entry_side, entry_price=100.0, qty=10):
    sim = ReplaySimulator(
        broker=MagicMock(),
        start_date="2024-01-01",
        end_date="2024-01-31",
        tickers=["AAPL"],
    )
    days = pd.date_range("2024-01-02", periods=3, freq="D", tz=ET)
    sim.historical_data["AAPL"] = pd.DataFrame(
        {
            "high": [entry_price] * 3,
            "low": [entry_price] * 3,
            "close": [entry_price] * 3,
        },
        index=days,
    )
    sim.positions["AAPL"] = {
        "qty": qty,
        "entry_price": entry_price,
        "entry_date": days[0],
        "side": entry_side,
    }
    return sim, days[2]


@pytest.mark.parametrize(
    "price,reason,wins,losses",
    [(97.0, "Stop Loss", 0, 1), (106.0, "Take Profit", 1, 0)],
)
def test_long_position_exits_on_stop_and_target(rm, price, reason, wins, losses):
    sim, day = _sim("BUY")
    sim._check_exit("AAPL", day, price, rm, MagicMock())
    assert "AAPL" not in sim.positions, f"{reason} did not close the position"
    assert sim.results["trades"][-1]["action"] == "EXIT"
    assert (sim.results["wins"], sim.results["losses"]) == (wins, losses)


def test_long_position_holds_inside_the_bands(rm, caplog):
    sim, day = _sim("BUY")
    sim._check_exit("AAPL", day, 101.0, rm, MagicMock())
    assert "AAPL" in sim.positions
    assert "Invalid side" not in caplog.text


def test_short_position_uses_inverted_bands(rm):
    sim, day = _sim("SELL")
    sim._check_exit("AAPL", day, 103.0, rm, MagicMock())  # price up 3% on a short: stop
    assert "AAPL" not in sim.positions
