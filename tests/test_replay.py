"""
Tests for alphalive.replay.ReplaySimulator.

Runs the day-by-day replay loop against a small synthetic OHLCV DataFrame
with a mocked broker (get_historical_bars), signal engine, and risk
manager - no real network or Alpaca calls.
"""

from unittest.mock import Mock

import pandas as pd
import pytest
from zoneinfo import ZoneInfo

from alphalive.replay import ReplaySimulator

ET = ZoneInfo("America/New_York")


def _make_history(n=60, base=100.0):
    dates = pd.date_range("2023-01-02", periods=n, freq="B", tz=ET)
    rows = []
    for i, d in enumerate(dates):
        price = base + i * 0.1
        rows.append(
            {
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price + 0.5,
                "volume": 1000,
            }
        )
    return pd.DataFrame(rows, index=dates)


@pytest.fixture
def broker():
    b = Mock()
    b.get_historical_bars.return_value = _make_history()
    return b


@pytest.fixture
def simulator(broker):
    return ReplaySimulator(
        broker=broker,
        start_date="2023-01-01",
        end_date="2023-03-31",
        tickers=["AAPL"],
        speed_multiplier=0,
        starting_equity=100000.0,
    )


@pytest.fixture
def strategy_config():
    cfg = Mock()
    cfg.ticker = "AAPL"
    return cfg


@pytest.fixture
def risk_manager():
    rm = Mock()
    rm.calculate_position_size.return_value = 10
    rm.can_trade.return_value = (True, "OK")
    rm.check_stop_loss.return_value = False
    rm.check_take_profit.return_value = False
    rm.check_trailing_stop.return_value = False
    return rm


# ---------------------------------------------------------------------------
# _load_historical_data() / _get_bars_up_to_date()
# ---------------------------------------------------------------------------


def test_load_historical_data_populates_trading_days(simulator):
    simulator._load_historical_data()

    assert "AAPL" in simulator.historical_data
    assert len(simulator.trading_days) == 60


def test_load_historical_data_raises_on_empty_data(broker, simulator):
    broker.get_historical_bars.return_value = pd.DataFrame()

    with pytest.raises(ValueError, match="No historical data"):
        simulator._load_historical_data()


def test_get_bars_up_to_date_excludes_current_day(simulator):
    simulator._load_historical_data()
    cutoff = simulator.trading_days[30]

    bars = simulator._get_bars_up_to_date("AAPL", cutoff, lookback_bars=200)

    assert all(bars.index < cutoff)
    assert len(bars) == 30


# ---------------------------------------------------------------------------
# _execute_entry() / _close_position() / _check_exit()
# ---------------------------------------------------------------------------


def test_execute_entry_opens_position_and_records_trade(
    simulator, strategy_config, risk_manager
):
    simulator._load_historical_data()
    notifier = Mock()
    current_date = simulator.trading_days[40]
    signal = {"signal": "BUY", "reason": "ma cross"}

    simulator._execute_entry(
        ticker="AAPL",
        signal=signal,
        current_date=current_date,
        current_price=110.0,
        config=strategy_config,
        risk_manager=risk_manager,
        notifier=notifier,
    )

    assert "AAPL" in simulator.positions
    assert simulator.results["total_trades"] == 1
    notifier.send_message.assert_called_once()


def test_execute_entry_zero_shares_skips(simulator, strategy_config, risk_manager):
    risk_manager.calculate_position_size.return_value = 0
    notifier = Mock()

    simulator._execute_entry(
        ticker="AAPL",
        signal={"signal": "BUY", "reason": "x"},
        current_date=pd.Timestamp("2023-01-05", tz=ET),
        current_price=100.0,
        config=strategy_config,
        risk_manager=risk_manager,
        notifier=notifier,
    )

    assert "AAPL" not in simulator.positions
    notifier.send_message.assert_not_called()


def test_execute_entry_blocked_by_risk_skips(simulator, strategy_config, risk_manager):
    risk_manager.can_trade.return_value = (False, "max positions")
    notifier = Mock()

    simulator._execute_entry(
        ticker="AAPL",
        signal={"signal": "BUY", "reason": "x"},
        current_date=pd.Timestamp("2023-01-05", tz=ET),
        current_price=100.0,
        config=strategy_config,
        risk_manager=risk_manager,
        notifier=notifier,
    )

    assert "AAPL" not in simulator.positions


def test_close_position_records_pnl_and_removes_position(simulator):
    simulator.positions["AAPL"] = {
        "qty": 10,
        "entry_price": 100.0,
        "entry_date": pd.Timestamp("2023-01-05", tz=ET),
        "side": "BUY",
    }
    notifier = Mock()

    simulator._close_position(
        ticker="AAPL",
        current_date=pd.Timestamp("2023-01-10", tz=ET),
        exit_price=110.0,
        qty=10,
        entry_price=100.0,
        reason="Take Profit",
        notifier=notifier,
    )

    assert "AAPL" not in simulator.positions
    assert simulator.results["total_pnl"] == pytest.approx(100.0)
    assert simulator.results["wins"] == 1
    notifier.send_message.assert_called_once()


def test_close_position_loss_increments_losses(simulator):
    simulator.positions["AAPL"] = {
        "qty": 10,
        "entry_price": 100.0,
        "entry_date": pd.Timestamp("2023-01-05", tz=ET),
        "side": "BUY",
    }
    notifier = Mock()

    simulator._close_position(
        ticker="AAPL",
        current_date=pd.Timestamp("2023-01-10", tz=ET),
        exit_price=90.0,
        qty=10,
        entry_price=100.0,
        reason="Stop Loss",
        notifier=notifier,
    )

    assert simulator.results["losses"] == 1


def test_check_exit_stop_loss_triggers_close(simulator, risk_manager):
    simulator._load_historical_data()
    entry_date = simulator.trading_days[10]
    current_date = simulator.trading_days[20]
    simulator.positions["AAPL"] = {
        "qty": 10,
        "entry_price": 100.0,
        "entry_date": entry_date,
        "side": "BUY",
    }
    risk_manager.check_stop_loss.return_value = True
    notifier = Mock()

    simulator._check_exit(
        ticker="AAPL",
        current_date=current_date,
        current_price=90.0,
        risk_manager=risk_manager,
        notifier=notifier,
    )

    assert "AAPL" not in simulator.positions


def test_check_exit_take_profit_triggers_close(simulator, risk_manager):
    simulator._load_historical_data()
    entry_date = simulator.trading_days[10]
    current_date = simulator.trading_days[20]
    simulator.positions["AAPL"] = {
        "qty": 10,
        "entry_price": 100.0,
        "entry_date": entry_date,
        "side": "BUY",
    }
    risk_manager.check_take_profit.return_value = True
    notifier = Mock()

    simulator._check_exit(
        ticker="AAPL",
        current_date=current_date,
        current_price=120.0,
        risk_manager=risk_manager,
        notifier=notifier,
    )

    assert "AAPL" not in simulator.positions


def test_check_exit_trailing_stop_triggers_close(simulator, risk_manager):
    simulator._load_historical_data()
    entry_date = simulator.trading_days[10]
    current_date = simulator.trading_days[20]
    simulator.positions["AAPL"] = {
        "qty": 10,
        "entry_price": 100.0,
        "entry_date": entry_date,
        "side": "BUY",
    }
    risk_manager.check_trailing_stop.return_value = True
    notifier = Mock()

    simulator._check_exit(
        ticker="AAPL",
        current_date=current_date,
        current_price=105.0,
        risk_manager=risk_manager,
        notifier=notifier,
    )

    assert "AAPL" not in simulator.positions


def test_check_exit_no_condition_met_keeps_position(simulator, risk_manager):
    simulator._load_historical_data()
    entry_date = simulator.trading_days[10]
    current_date = simulator.trading_days[20]
    simulator.positions["AAPL"] = {
        "qty": 10,
        "entry_price": 100.0,
        "entry_date": entry_date,
        "side": "BUY",
    }
    notifier = Mock()

    simulator._check_exit(
        ticker="AAPL",
        current_date=current_date,
        current_price=101.0,
        risk_manager=risk_manager,
        notifier=notifier,
    )

    assert "AAPL" in simulator.positions
    notifier.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# _simulate_trading_day() / run() / _send_final_summary()
# ---------------------------------------------------------------------------


def test_simulate_trading_day_insufficient_data_skips(
    simulator, strategy_config, risk_manager
):
    simulator._load_historical_data()
    signal_engine = Mock()
    notifier = Mock()
    early_day = simulator.trading_days[5]  # fewer than 50 bars available

    simulator._simulate_trading_day(
        current_date=early_day,
        strategy_configs=[strategy_config],
        signal_engines={"AAPL": signal_engine},
        risk_managers={"AAPL": risk_manager},
        order_managers={"AAPL": Mock()},
        notifier=notifier,
    )

    signal_engine.generate_signal.assert_not_called()


def test_simulate_trading_day_warmup_incomplete_skips(
    simulator, strategy_config, risk_manager
):
    simulator._load_historical_data()
    signal_engine = Mock()
    signal_engine.generate_signal.return_value = {"warmup_complete": False}
    notifier = Mock()
    day = simulator.trading_days[55]

    simulator._simulate_trading_day(
        current_date=day,
        strategy_configs=[strategy_config],
        signal_engines={"AAPL": signal_engine},
        risk_managers={"AAPL": risk_manager},
        order_managers={"AAPL": Mock()},
        notifier=notifier,
    )

    assert "AAPL" not in simulator.positions


def test_simulate_trading_day_buy_signal_opens_position(
    simulator, strategy_config, risk_manager
):
    simulator._load_historical_data()
    signal_engine = Mock()
    signal_engine.generate_signal.return_value = {
        "warmup_complete": True,
        "signal": "BUY",
        "confidence": 0.8,
        "reason": "cross",
    }
    notifier = Mock()
    day = simulator.trading_days[55]

    simulator._simulate_trading_day(
        current_date=day,
        strategy_configs=[strategy_config],
        signal_engines={"AAPL": signal_engine},
        risk_managers={"AAPL": risk_manager},
        order_managers={"AAPL": Mock()},
        notifier=notifier,
    )

    assert "AAPL" in simulator.positions


def test_simulate_trading_day_sell_signal_closes_position(
    simulator, strategy_config, risk_manager
):
    simulator._load_historical_data()
    day = simulator.trading_days[55]
    simulator.positions["AAPL"] = {
        "qty": 10,
        "entry_price": 100.0,
        "entry_date": simulator.trading_days[50],
        "side": "BUY",
    }
    signal_engine = Mock()
    signal_engine.generate_signal.return_value = {
        "warmup_complete": True,
        "signal": "SELL",
        "confidence": 0.8,
        "reason": "cross down",
    }
    notifier = Mock()

    simulator._simulate_trading_day(
        current_date=day,
        strategy_configs=[strategy_config],
        signal_engines={"AAPL": signal_engine},
        risk_managers={"AAPL": risk_manager},
        order_managers={"AAPL": Mock()},
        notifier=notifier,
    )

    assert "AAPL" not in simulator.positions


def test_simulate_trading_day_sleeps_when_speed_multiplier_set(
    simulator, strategy_config, risk_manager
):
    simulator._load_historical_data()
    simulator.speed_multiplier = 1
    signal_engine = Mock()
    signal_engine.generate_signal.return_value = {
        "warmup_complete": True,
        "signal": "HOLD",
        "confidence": 0.5,
        "reason": "n/a",
    }
    day = simulator.trading_days[55]

    with pytest.MonkeyPatch.context() as mp:
        sleep_mock = Mock()
        mp.setattr("alphalive.replay.time.sleep", sleep_mock)
        simulator._simulate_trading_day(
            current_date=day,
            strategy_configs=[strategy_config],
            signal_engines={"AAPL": signal_engine},
            risk_managers={"AAPL": risk_manager},
            order_managers={"AAPL": Mock()},
            notifier=Mock(),
        )

    sleep_mock.assert_called_once_with(1)


def test_run_executes_full_loop_and_sends_summary(
    broker, strategy_config, risk_manager
):
    sim = ReplaySimulator(
        broker=broker,
        start_date="2023-01-01",
        end_date="2023-03-31",
        tickers=["AAPL"],
        speed_multiplier=0,
    )
    signal_engine = Mock()
    signal_engine.generate_signal.return_value = {
        "warmup_complete": True,
        "signal": "HOLD",
        "confidence": 0.5,
        "reason": "n/a",
    }
    notifier = Mock()

    sim.run(
        strategy_configs=[strategy_config],
        signal_engines={"AAPL": signal_engine},
        risk_managers={"AAPL": risk_manager},
        order_managers={"AAPL": Mock()},
        notifier=notifier,
    )

    # Startup message + final summary, at minimum
    assert notifier.send_message.call_count >= 2


def test_send_final_summary_with_trades_includes_trade_lines(simulator):
    simulator.results["total_trades"] = 1
    simulator.results["wins"] = 1
    simulator.results["total_pnl"] = 150.0
    simulator.results["trades"] = [
        {
            "date": "2023-01-05",
            "ticker": "AAPL",
            "action": "ENTRY",
            "side": "BUY",
            "qty": 10,
            "price": 100.0,
        },
        {
            "date": "2023-01-10",
            "ticker": "AAPL",
            "action": "EXIT",
            "side": "SELL",
            "qty": 10,
            "price": 110.0,
            "pnl": 100.0,
            "pnl_pct": 10.0,
            "reason": "Take Profit",
        },
    ]
    notifier = Mock()

    simulator._send_final_summary(notifier)

    notifier.send_message.assert_called_once()
    summary_text = notifier.send_message.call_args[0][0]
    assert "Replay Complete" in summary_text
    assert "AAPL" in summary_text


def test_send_final_summary_no_trades(simulator):
    notifier = Mock()
    simulator._send_final_summary(notifier)

    notifier.send_message.assert_called_once()
    assert "Win Rate:</b> 0.0%" in notifier.send_message.call_args[0][0]
