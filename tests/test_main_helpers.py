"""
Tests for alphalive.main helper functions.

main.py's main() function is a 24/7 orchestration loop that can't be run
end-to-end in a test. Instead this covers the extracted, individually
testable units: signal-check gating, EOD stat computation, startup
messaging/warmup, per-strategy signal checking, exit-condition checks,
and position reconciliation - all with the broker/notifier/order-manager
mocked out. No real network or Alpaca calls are made.
"""

import time
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from alphalive import main as main_module
from alphalive.data.market_data import DataStaleError

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# should_run_signal_check()
# ---------------------------------------------------------------------------


def test_should_run_signal_check_1day_always_false():
    assert main_module.should_run_signal_check("1Day", 0) is False


@patch("alphalive.main.datetime")
def test_should_run_signal_check_15min_at_boundary_and_elapsed(mock_dt):
    mock_dt.now.return_value = datetime(2024, 1, 2, 10, 15, tzinfo=ET)
    last_check = time.time() - (16 * 60)  # 16 minutes ago
    assert main_module.should_run_signal_check("15Min", last_check) is True


@patch("alphalive.main.datetime")
def test_should_run_signal_check_15min_not_at_boundary(mock_dt):
    mock_dt.now.return_value = datetime(2024, 1, 2, 10, 17, tzinfo=ET)
    last_check = time.time() - (16 * 60)
    assert main_module.should_run_signal_check("15Min", last_check) is False


@patch("alphalive.main.datetime")
def test_should_run_signal_check_15min_not_enough_time_elapsed(mock_dt):
    mock_dt.now.return_value = datetime(2024, 1, 2, 10, 15, tzinfo=ET)
    last_check = time.time() - 60  # only 1 minute ago
    assert main_module.should_run_signal_check("15Min", last_check) is False


# ---------------------------------------------------------------------------
# _compute_daily_stats()
# ---------------------------------------------------------------------------


def test_compute_daily_stats_pnl_from_equity_change():
    stats = main_module._compute_daily_stats([], start_equity=100000.0, end_equity=101500.0)
    assert stats["pnl"] == 1500.0
    assert stats["win_rate"] == 0.0  # No round trips


def test_compute_daily_stats_win_rate_fifo_matching():
    orders = [
        {"ticker": "AAPL", "side": "BUY", "qty": 10, "price": 100.0, "timestamp": "2024-01-02T09:35:00"},
        {"ticker": "AAPL", "side": "SELL", "qty": 10, "price": 110.0, "timestamp": "2024-01-02T10:00:00"},
        {"ticker": "AAPL", "side": "BUY", "qty": 5, "price": 120.0, "timestamp": "2024-01-02T11:00:00"},
        {"ticker": "AAPL", "side": "SELL", "qty": 5, "price": 115.0, "timestamp": "2024-01-02T12:00:00"},
    ]
    stats = main_module._compute_daily_stats(orders, start_equity=100000.0, end_equity=100050.0)
    # 1 win (110 > 100), 1 loss (115 < 120) => 50% win rate
    assert stats["win_rate"] == 50.0
    assert stats["pnl"] == 50.0


def test_compute_daily_stats_sell_without_matching_buy_ignored():
    orders = [
        {"ticker": "AAPL", "side": "SELL", "qty": 10, "price": 110.0, "timestamp": "2024-01-02T10:00:00"},
    ]
    stats = main_module._compute_daily_stats(orders, start_equity=100000.0, end_equity=100000.0)
    assert stats["win_rate"] == 0.0


def test_compute_daily_stats_multi_ticker_independent_queues():
    orders = [
        {"ticker": "AAPL", "side": "BUY", "qty": 10, "price": 100.0, "timestamp": "2024-01-02T09:35:00"},
        {"ticker": "MSFT", "side": "BUY", "qty": 10, "price": 200.0, "timestamp": "2024-01-02T09:36:00"},
        {"ticker": "AAPL", "side": "SELL", "qty": 10, "price": 90.0, "timestamp": "2024-01-02T10:00:00"},
        {"ticker": "MSFT", "side": "SELL", "qty": 10, "price": 210.0, "timestamp": "2024-01-02T10:01:00"},
    ]
    stats = main_module._compute_daily_stats(orders, start_equity=100000.0, end_equity=100000.0)
    # AAPL loses (90<100), MSFT wins (210>200) -> 50%
    assert stats["win_rate"] == 50.0


# ---------------------------------------------------------------------------
# _send_eod_summary()
# ---------------------------------------------------------------------------


def test_send_eod_summary_aggregates_orders_and_notifies():
    order_manager_map = {
        "AAPL": Mock(get_order_history=Mock(return_value=[
            {"ticker": "AAPL", "side": "BUY", "qty": 10, "price": 100.0, "timestamp": "2024-01-02T09:35:00"},
        ])),
    }
    broker = Mock()
    broker.get_account.return_value = Mock(equity=101000.0)
    notifier = Mock()

    main_module._send_eod_summary(order_manager_map, broker, morning_equity=100000.0, notifier=notifier)

    notifier.send_daily_summary.assert_called_once()
    call_args = notifier.send_daily_summary.call_args[0][0]
    assert call_args["trades"] == 1
    assert call_args["pnl"] == 1000.0
    assert call_args["start_equity"] == 100000.0
    assert call_args["end_equity"] == 101000.0


# ---------------------------------------------------------------------------
# _build_startup_message()
# ---------------------------------------------------------------------------


def test_build_startup_message_single_strategy(sample_strategy_config):
    msg = main_module._build_startup_message([sample_strategy_config], "PAPER")
    assert "AlphaLive Started" in msg
    assert sample_strategy_config.strategy.name in msg
    assert sample_strategy_config.ticker in msg
    assert "PAPER" in msg


def test_build_startup_message_multi_strategy(sample_strategy_config):
    cfg2 = sample_strategy_config.model_copy(deep=True)
    cfg2.ticker = "MSFT"
    msg = main_module._build_startup_message([sample_strategy_config, cfg2], "DRY RUN")
    assert "Multi-Strategy" in msg
    assert "AAPL" in msg
    assert "MSFT" in msg


# ---------------------------------------------------------------------------
# _run_startup_warmup()
# ---------------------------------------------------------------------------


def test_run_startup_warmup_success_single_strategy(sample_strategy_config):
    market_data = Mock()
    df = pd.DataFrame({"close": [1, 2, 3]})
    market_data.get_latest_bars.return_value = df

    signal_engine = Mock()
    signal_engine.generate_signal.return_value = {
        "warmup_complete": True,
        "signal": "HOLD",
        "confidence": 0.5,
    }
    signal_engine_map = {sample_strategy_config.ticker: signal_engine}
    notifier = Mock()

    main_module._run_startup_warmup(
        [sample_strategy_config], market_data, signal_engine_map, notifier
    )

    notifier.send_message.assert_called_once()
    notifier.send_alert.assert_not_called()
    notifier.send_error_alert.assert_not_called()


def test_run_startup_warmup_incomplete_sends_alert(sample_strategy_config):
    market_data = Mock()
    market_data.get_latest_bars.return_value = pd.DataFrame({"close": [1]})

    signal_engine = Mock()
    signal_engine.generate_signal.return_value = {"warmup_complete": False}
    signal_engine_map = {sample_strategy_config.ticker: signal_engine}
    notifier = Mock()

    main_module._run_startup_warmup(
        [sample_strategy_config], market_data, signal_engine_map, notifier
    )

    notifier.send_alert.assert_called_once()
    assert "warmup incomplete" in notifier.send_alert.call_args[0][0].lower()


def test_run_startup_warmup_data_stale_exits(sample_strategy_config):
    market_data = Mock()
    market_data.get_latest_bars.side_effect = DataStaleError("stale data")
    signal_engine_map = {sample_strategy_config.ticker: Mock()}
    notifier = Mock()

    with pytest.raises(SystemExit) as exc_info:
        main_module._run_startup_warmup(
            [sample_strategy_config], market_data, signal_engine_map, notifier
        )

    assert exc_info.value.code == 1
    notifier.send_error_alert.assert_called_once()


def test_run_startup_warmup_generic_error_continues(sample_strategy_config):
    market_data = Mock()
    market_data.get_latest_bars.side_effect = RuntimeError("boom")
    signal_engine_map = {sample_strategy_config.ticker: Mock()}
    notifier = Mock()

    # Should not raise - errors other than DataStaleError are logged & alerted.
    main_module._run_startup_warmup(
        [sample_strategy_config], market_data, signal_engine_map, notifier
    )

    notifier.send_error_alert.assert_called_once()


def test_run_startup_warmup_multi_strategy_sends_combined_message(sample_strategy_config):
    cfg2 = sample_strategy_config.model_copy(deep=True)
    cfg2.ticker = "MSFT"

    market_data = Mock()
    market_data.get_latest_bars.return_value = pd.DataFrame({"close": [1, 2, 3]})

    signal_engine = Mock()
    signal_engine.generate_signal.return_value = {
        "warmup_complete": True,
        "signal": "HOLD",
        "confidence": 0.5,
    }
    signal_engine_map = {
        sample_strategy_config.ticker: signal_engine,
        "MSFT": signal_engine,
    }
    notifier = Mock()

    main_module._run_startup_warmup(
        [sample_strategy_config, cfg2], market_data, signal_engine_map, notifier
    )

    # One combined "multi-strategy warmup complete" message (no per-strategy message)
    notifier.send_message.assert_called_once()
    assert "Multi-strategy warmup complete" in notifier.send_message.call_args[0][0]


# ---------------------------------------------------------------------------
# _check_signal_for_strategy()
# ---------------------------------------------------------------------------


def _make_ohlcv_df(n=5, base=100.0):
    rows = []
    for i in range(n):
        price = base + i
        rows.append({"open": price, "high": price + 1, "low": price - 1, "close": price + 0.5, "volume": 1000})
    return pd.DataFrame(rows)


def test_check_signal_for_strategy_1day_skips_outside_window(sample_strategy_config):
    now_et = datetime(2024, 1, 2, 9, 0, tzinfo=ET)  # before 9:35
    morning_checks_done = set()
    last_signal_check_map = {}
    market_data = Mock()

    main_module._check_signal_for_strategy(
        sample_strategy_config, now_et, morning_checks_done, last_signal_check_map,
        market_data, {}, {}, None, Mock(), Mock(), main_module.GlobalRiskManager(), Mock(),
    )

    market_data.get_latest_bars.assert_not_called()
    assert sample_strategy_config.ticker not in morning_checks_done


def test_check_signal_for_strategy_1day_skips_if_already_done(sample_strategy_config):
    now_et = datetime(2024, 1, 2, 9, 40, tzinfo=ET)
    morning_checks_done = {sample_strategy_config.ticker}
    market_data = Mock()

    main_module._check_signal_for_strategy(
        sample_strategy_config, now_et, morning_checks_done, {},
        market_data, {}, {}, None, Mock(), Mock(), main_module.GlobalRiskManager(), Mock(),
    )

    market_data.get_latest_bars.assert_not_called()


def test_check_signal_for_strategy_hold_signal_no_order(sample_strategy_config):
    now_et = datetime(2024, 1, 2, 9, 40, tzinfo=ET)
    market_data = Mock()
    market_data.get_latest_bars.return_value = _make_ohlcv_df()

    signal_engine = Mock()
    signal_engine.generate_signal.return_value = {
        "signal": "HOLD", "confidence": 0.3, "reason": "no edge",
    }
    signal_engine_map = {sample_strategy_config.ticker: signal_engine}
    order_manager = Mock()
    order_manager_map = {sample_strategy_config.ticker: order_manager}
    morning_checks_done = set()

    main_module._check_signal_for_strategy(
        sample_strategy_config, now_et, morning_checks_done, {},
        market_data, signal_engine_map, order_manager_map, None, Mock(), Mock(),
        main_module.GlobalRiskManager(), Mock(),
    )

    order_manager.execute_signal.assert_not_called()
    assert sample_strategy_config.ticker in morning_checks_done


def test_check_signal_for_strategy_buy_signal_executes_order(sample_strategy_config):
    now_et = datetime(2024, 1, 2, 9, 40, tzinfo=ET)
    market_data = Mock()
    market_data.get_latest_bars.return_value = _make_ohlcv_df()
    market_data.get_current_price.return_value = 150.0

    signal_engine = Mock()
    signal_engine.generate_signal.return_value = {
        "signal": "BUY", "confidence": 0.8, "reason": "ma cross",
    }
    signal_engine_map = {sample_strategy_config.ticker: signal_engine}

    order_manager = Mock()
    order_manager.execute_signal.return_value = {
        "status": "success", "order_id": "o1", "filled_qty": 10, "filled_price": 150.0,
    }
    order_manager_map = {sample_strategy_config.ticker: order_manager}

    broker = Mock()
    broker.get_account.return_value = Mock(equity=100000.0)
    broker.get_all_positions.return_value = []

    notifier = Mock()
    morning_checks_done = set()

    main_module._check_signal_for_strategy(
        sample_strategy_config, now_et, morning_checks_done, {},
        market_data, signal_engine_map, order_manager_map, None, broker, notifier,
        main_module.GlobalRiskManager(), Mock(),
    )

    order_manager.execute_signal.assert_called_once()
    notifier.send_trade_notification.assert_called_once()


def test_check_signal_for_strategy_buy_signal_blocked(sample_strategy_config):
    now_et = datetime(2024, 1, 2, 9, 40, tzinfo=ET)
    market_data = Mock()
    market_data.get_latest_bars.return_value = _make_ohlcv_df()
    market_data.get_current_price.return_value = 150.0

    signal_engine = Mock()
    signal_engine.generate_signal.return_value = {
        "signal": "BUY", "confidence": 0.8, "reason": "ma cross",
    }
    signal_engine_map = {sample_strategy_config.ticker: signal_engine}

    order_manager = Mock()
    order_manager.execute_signal.return_value = {"status": "blocked", "reason": "risk limit"}
    order_manager_map = {sample_strategy_config.ticker: order_manager}

    broker = Mock()
    broker.get_account.return_value = Mock(equity=100000.0)
    broker.get_all_positions.return_value = []
    notifier = Mock()

    main_module._check_signal_for_strategy(
        sample_strategy_config, now_et, set(), {},
        market_data, signal_engine_map, order_manager_map, None, broker, notifier,
        main_module.GlobalRiskManager(), Mock(),
    )

    notifier.send_trade_notification.assert_not_called()


def test_check_signal_for_strategy_buy_signal_error_status(sample_strategy_config):
    now_et = datetime(2024, 1, 2, 9, 40, tzinfo=ET)
    market_data = Mock()
    market_data.get_latest_bars.return_value = _make_ohlcv_df()
    market_data.get_current_price.return_value = 150.0

    signal_engine = Mock()
    signal_engine.generate_signal.return_value = {
        "signal": "BUY", "confidence": 0.8, "reason": "ma cross",
    }
    signal_engine_map = {sample_strategy_config.ticker: signal_engine}

    order_manager = Mock()
    order_manager.execute_signal.return_value = {"status": "error", "reason": "broker down"}
    order_manager_map = {sample_strategy_config.ticker: order_manager}

    broker = Mock()
    broker.get_account.return_value = Mock(equity=100000.0)
    broker.get_all_positions.return_value = []
    notifier = Mock()

    # Should not raise
    main_module._check_signal_for_strategy(
        sample_strategy_config, now_et, set(), {},
        market_data, signal_engine_map, order_manager_map, None, broker, notifier,
        main_module.GlobalRiskManager(), Mock(),
    )


def test_check_signal_for_strategy_corporate_action_detected(sample_strategy_config):
    now_et = datetime(2024, 1, 2, 9, 40, tzinfo=ET)
    df = pd.DataFrame({
        "open": [100.0, 130.0],
        "high": [101.0, 131.0],
        "low": [99.0, 129.0],
        "close": [100.5, 130.5],
        "volume": [1000, 1000],
    })
    market_data = Mock()
    market_data.get_latest_bars.return_value = df
    signal_engine_map = {sample_strategy_config.ticker: Mock()}
    notifier = Mock()
    morning_checks_done = set()

    main_module._check_signal_for_strategy(
        sample_strategy_config, now_et, morning_checks_done, {},
        market_data, signal_engine_map, {}, None, Mock(), notifier,
        main_module.GlobalRiskManager(), Mock(),
    )

    notifier.send_alert.assert_called_once()
    assert "CORPORATE ACTION" in notifier.send_alert.call_args[0][0]
    assert sample_strategy_config.ticker in morning_checks_done
    signal_engine_map[sample_strategy_config.ticker].generate_signal.assert_not_called()


def test_check_signal_for_strategy_data_stale_error(sample_strategy_config):
    now_et = datetime(2024, 1, 2, 9, 40, tzinfo=ET)
    market_data = Mock()
    market_data.get_latest_bars.side_effect = DataStaleError("stale")
    notifier = Mock()
    morning_checks_done = set()

    main_module._check_signal_for_strategy(
        sample_strategy_config, now_et, morning_checks_done, {},
        market_data, {}, {}, None, Mock(), notifier,
        main_module.GlobalRiskManager(), Mock(),
    )

    notifier.send_error_alert.assert_called_once()
    assert sample_strategy_config.ticker in morning_checks_done


def test_check_signal_for_strategy_generic_exception(sample_strategy_config):
    now_et = datetime(2024, 1, 2, 9, 40, tzinfo=ET)
    market_data = Mock()
    market_data.get_latest_bars.side_effect = RuntimeError("boom")
    notifier = Mock()
    morning_checks_done = set()

    main_module._check_signal_for_strategy(
        sample_strategy_config, now_et, morning_checks_done, {},
        market_data, {}, {}, None, Mock(), notifier,
        main_module.GlobalRiskManager(), Mock(),
    )

    notifier.send_error_alert.assert_called_once()
    assert sample_strategy_config.ticker in morning_checks_done


def test_check_signal_for_strategy_intraday_uses_should_run_check(sample_strategy_config):
    intraday_cfg = sample_strategy_config.model_copy(deep=True)
    intraday_cfg.timeframe = "15Min"
    now_et = datetime(2024, 1, 2, 10, 0, tzinfo=ET)
    market_data = Mock()

    with patch("alphalive.main.should_run_signal_check", return_value=False) as mock_should_run:
        main_module._check_signal_for_strategy(
            intraday_cfg, now_et, set(), {},
            market_data, {}, {}, None, Mock(), Mock(),
            main_module.GlobalRiskManager(), Mock(),
        )
        mock_should_run.assert_called_once()
    market_data.get_latest_bars.assert_not_called()


def test_check_signal_for_strategy_with_pre_execution_checks_blocked(sample_strategy_config):
    """When the sentiment filter blocks execution, no order is placed."""
    now_et = datetime(2024, 1, 2, 9, 40, tzinfo=ET)
    market_data = Mock()
    market_data.get_latest_bars.return_value = _make_ohlcv_df()

    signal_engine = Mock()
    signal_engine.generate_signal.return_value = {
        "signal": "BUY", "confidence": 0.8, "reason": "ma cross",
    }
    signal_engine_map = {sample_strategy_config.ticker: signal_engine}
    order_manager = Mock()
    order_manager_map = {sample_strategy_config.ticker: order_manager}

    alphasignal_client = Mock()

    with patch(
        "alphalive.main.run_pre_execution_checks",
        return_value=(False, {"sentiment_score": -0.9}),
    ):
        main_module._check_signal_for_strategy(
            sample_strategy_config, now_et, set(), {},
            market_data, signal_engine_map, order_manager_map,
            alphasignal_client, Mock(), Mock(),
            main_module.GlobalRiskManager(), Mock(),
        )

    order_manager.execute_signal.assert_not_called()


# ---------------------------------------------------------------------------
# _run_exit_checks()
# ---------------------------------------------------------------------------


def _mock_position(symbol="AAPL", qty=10.0, side="long", avg_entry=100.0, current=105.0):
    pos = Mock()
    pos.symbol = symbol
    pos.qty = qty
    pos.side = side
    pos.avg_entry_price = avg_entry
    pos.current_price = current
    return pos


def test_run_exit_checks_no_positions_noop():
    broker = Mock()
    broker.get_all_positions.return_value = []
    market_data = Mock()
    bot_state = Mock()
    app_config = Mock(dry_run=False)
    notifier = Mock()

    main_module._run_exit_checks(broker, market_data, {}, bot_state, app_config, notifier, main_module.GlobalRiskManager())

    bot_state.set_position_high.assert_not_called()


def test_run_exit_checks_no_order_manager_for_ticker_skips():
    broker = Mock()
    broker.get_all_positions.return_value = [_mock_position()]
    market_data = Mock()
    market_data.get_current_price.return_value = 105.0
    bot_state = Mock()
    app_config = Mock(dry_run=False)
    notifier = Mock()

    main_module._run_exit_checks(broker, market_data, {}, bot_state, app_config, notifier, main_module.GlobalRiskManager())
    # No order manager for AAPL registered -> loop should skip without error


def test_run_exit_checks_exit_fires_and_closes_position():
    pos = _mock_position()
    broker = Mock()
    broker.get_all_positions.return_value = [pos]
    market_data = Mock()
    market_data.get_current_price.return_value = 105.0

    order_manager = Mock()
    order_manager.config.risk.commission_per_trade = 0.0
    order_manager.check_exits.return_value = [
        {"ticker": "AAPL", "reason": "stop_loss", "current_price": 105.0}
    ]
    order_manager.close_position.return_value = {"status": "success"}
    order_manager_map = {"AAPL": order_manager}

    bot_state = Mock()
    bot_state.get_position_high.return_value = 110.0
    app_config = Mock(dry_run=False)
    notifier = Mock()

    main_module._run_exit_checks(broker, market_data, order_manager_map, bot_state, app_config, notifier, main_module.GlobalRiskManager())

    order_manager.close_position.assert_called_once_with(ticker="AAPL", reason="stop_loss")
    bot_state.clear_position_high.assert_called_once_with("AAPL")
    notifier.send_position_closed_notification.assert_called_once()


def test_run_exit_checks_dry_run_skips_close():
    pos = _mock_position()
    broker = Mock()
    broker.get_all_positions.return_value = [pos]
    market_data = Mock()
    market_data.get_current_price.return_value = 105.0

    order_manager = Mock()
    order_manager.check_exits.return_value = [
        {"ticker": "AAPL", "reason": "stop_loss", "current_price": 105.0}
    ]
    order_manager_map = {"AAPL": order_manager}

    bot_state = Mock()
    bot_state.get_position_high.return_value = 110.0
    app_config = Mock(dry_run=True)
    notifier = Mock()

    main_module._run_exit_checks(broker, market_data, order_manager_map, bot_state, app_config, notifier, main_module.GlobalRiskManager())

    order_manager.close_position.assert_not_called()


def test_run_exit_checks_price_fetch_failure_falls_back_to_position_price():
    pos = _mock_position()
    broker = Mock()
    broker.get_all_positions.return_value = [pos]
    market_data = Mock()
    market_data.get_current_price.side_effect = RuntimeError("no data")

    order_manager = Mock()
    order_manager.check_exits.return_value = []
    order_manager_map = {"AAPL": order_manager}

    bot_state = Mock()
    bot_state.get_position_high.return_value = None
    app_config = Mock(dry_run=False)
    notifier = Mock()

    main_module._run_exit_checks(broker, market_data, order_manager_map, bot_state, app_config, notifier, main_module.GlobalRiskManager())

    # check_exits still called with the fallback price map
    order_manager.check_exits.assert_called_once()
    _, current_prices = order_manager.check_exits.call_args[0]
    assert current_prices["AAPL"] == pos.current_price


def test_run_exit_checks_broker_error_caught_and_alerted():
    broker = Mock()
    broker.get_all_positions.side_effect = RuntimeError("broker down")
    market_data = Mock()
    bot_state = Mock()
    app_config = Mock(dry_run=False)
    notifier = Mock()

    # Should not raise
    main_module._run_exit_checks(broker, market_data, {}, bot_state, app_config, notifier, main_module.GlobalRiskManager())

    notifier.send_error_alert.assert_called_once()


# ---------------------------------------------------------------------------
# _record_broker_call()
# ---------------------------------------------------------------------------


def test_record_broker_call_success_resets_and_exits_degraded_mode():
    order_manager_map = {"AAPL": Mock(), "MSFT": Mock()}

    main_module._record_broker_call(order_manager_map, success=True)

    for om in order_manager_map.values():
        om.risk.record_broker_success.assert_called_once()
        om.risk.exit_degraded_mode.assert_called_once()
        om.risk.record_broker_failure.assert_not_called()


def test_record_broker_call_failure_records_on_every_risk_manager():
    order_manager_map = {"AAPL": Mock(), "MSFT": Mock()}
    error = RuntimeError("broker down")

    main_module._record_broker_call(order_manager_map, success=False, error=error)

    for om in order_manager_map.values():
        om.risk.record_broker_failure.assert_called_once_with(error)
        om.risk.record_broker_success.assert_not_called()


def test_run_exit_checks_broker_failure_recorded_on_risk_manager():
    broker = Mock()
    broker.get_all_positions.side_effect = RuntimeError("broker down")
    order_manager = Mock()
    order_manager_map = {"AAPL": order_manager}
    market_data = Mock()
    bot_state = Mock()
    app_config = Mock(dry_run=False)
    notifier = Mock()

    main_module._run_exit_checks(
        broker, market_data, order_manager_map, bot_state, app_config, notifier,
        main_module.GlobalRiskManager(),
    )

    order_manager.risk.record_broker_failure.assert_called_once()


def test_run_exit_checks_broker_success_recorded_on_risk_manager():
    broker = Mock()
    broker.get_all_positions.return_value = []
    order_manager = Mock()
    order_manager_map = {"AAPL": order_manager}
    market_data = Mock()
    bot_state = Mock()
    app_config = Mock(dry_run=False)
    notifier = Mock()

    main_module._run_exit_checks(
        broker, market_data, order_manager_map, bot_state, app_config, notifier,
        main_module.GlobalRiskManager(),
    )

    order_manager.risk.record_broker_success.assert_called_once()
    order_manager.risk.exit_degraded_mode.assert_called_once()


# ---------------------------------------------------------------------------
# _run_position_reconciliation()
# ---------------------------------------------------------------------------


def _mock_bot_state(ledger=None):
    bot_state = Mock()
    bot_state.get_open_positions.return_value = ledger or {}
    return bot_state


def test_run_position_reconciliation_no_drift():
    pos = _mock_position()
    broker = Mock()
    broker.get_all_positions.return_value = [pos]

    bot_state = _mock_bot_state({"AAPL": {"qty": 10, "entry_price": 150.0}})
    order_manager_map = {"AAPL": Mock()}
    app_config = Mock(trading_paused=False)
    notifier = Mock()

    with patch.dict("os.environ", {}, clear=False):
        main_module._run_position_reconciliation(
            broker, order_manager_map, app_config, notifier, bot_state
        )

    notifier.send_alert.assert_not_called()
    assert app_config.trading_paused is False


def test_run_position_reconciliation_multi_day_hold_no_drift():
    """A position opened on a previous day (empty order history today) must NOT
    look like drift - the ledger, not the daily order log, is the source of truth."""
    pos = _mock_position()
    broker = Mock()
    broker.get_all_positions.return_value = [pos]

    bot_state = _mock_bot_state({"AAPL": {"qty": 10, "entry_price": 150.0}})
    # Simulates the morning after: reset_daily() wiped today's order history
    order_manager_map = {"AAPL": Mock(get_order_history=Mock(return_value=[]))}
    app_config = Mock(trading_paused=False)
    notifier = Mock()

    with patch.dict("os.environ", {}, clear=False):
        main_module._run_position_reconciliation(
            broker, order_manager_map, app_config, notifier, bot_state
        )

    notifier.send_alert.assert_not_called()
    assert app_config.trading_paused is False


def test_run_position_reconciliation_alpaca_only_drift_pauses_trading():
    """Broker has a position the ledger never recorded - the first drift branch."""
    pos = _mock_position()
    broker = Mock()
    broker.get_all_positions.return_value = [pos]

    bot_state = _mock_bot_state({})
    order_manager_map = {"AAPL": Mock()}
    app_config = Mock(trading_paused=False)
    notifier = Mock()

    with patch.dict("os.environ", {}, clear=False):
        main_module._run_position_reconciliation(
            broker, order_manager_map, app_config, notifier, bot_state
        )

    assert app_config.trading_paused is True
    assert notifier.send_alert.call_count == 2  # per-ticker alert + halt alert


def test_run_position_reconciliation_internal_only_drift_pauses_trading():
    """Ledger tracks a position the broker no longer shows - the second drift branch."""
    broker = Mock()
    broker.get_all_positions.return_value = []

    bot_state = _mock_bot_state({"AAPL": {"qty": 10, "entry_price": 150.0}})
    order_manager_map = {"AAPL": Mock()}
    app_config = Mock(trading_paused=False)
    notifier = Mock()

    with patch.dict("os.environ", {}, clear=False):
        main_module._run_position_reconciliation(
            broker, order_manager_map, app_config, notifier, bot_state
        )

    assert app_config.trading_paused is True


def test_run_position_reconciliation_broker_error_caught():
    broker = Mock()
    broker.get_all_positions.side_effect = RuntimeError("boom")
    app_config = Mock(trading_paused=False)
    notifier = Mock()

    # Should not raise
    main_module._run_position_reconciliation(
        broker, {}, app_config, notifier, _mock_bot_state()
    )

    notifier.send_alert.assert_not_called()


# ---------------------------------------------------------------------------
# _sync_position_ledger()
# ---------------------------------------------------------------------------


def test_sync_position_ledger_adopts_untracked_broker_position():
    """Broker position missing from the ledger at startup is adopted, not halted."""
    pos = _mock_position()
    broker = Mock()
    broker.get_all_positions.return_value = [pos]
    bot_state = _mock_bot_state({})
    notifier = Mock()

    main_module._sync_position_ledger(broker, bot_state, notifier)

    bot_state.record_position_open.assert_called_once_with(
        "AAPL", pos.qty, pos.avg_entry_price
    )
    notifier.send_alert.assert_called_once()


def test_sync_position_ledger_removes_stale_ledger_entry():
    """Ledger entry with no broker position at startup is removed, not halted."""
    broker = Mock()
    broker.get_all_positions.return_value = []
    bot_state = _mock_bot_state({"MSFT": {"qty": 5, "entry_price": 400.0}})
    notifier = Mock()

    main_module._sync_position_ledger(broker, bot_state, notifier)

    bot_state.record_position_close.assert_called_once_with("MSFT")
    notifier.send_alert.assert_called_once()


def test_sync_position_ledger_in_sync_is_silent():
    pos = _mock_position()
    broker = Mock()
    broker.get_all_positions.return_value = [pos]
    bot_state = _mock_bot_state({"AAPL": {"qty": 10, "entry_price": 150.0}})
    notifier = Mock()

    main_module._sync_position_ledger(broker, bot_state, notifier)

    bot_state.record_position_open.assert_not_called()
    bot_state.record_position_close.assert_not_called()
    notifier.send_alert.assert_not_called()


def test_sync_position_ledger_broker_error_skips_quietly():
    broker = Mock()
    broker.get_all_positions.side_effect = RuntimeError("boom")
    bot_state = _mock_bot_state()
    notifier = Mock()

    # Should not raise, should not touch the ledger
    main_module._sync_position_ledger(broker, bot_state, notifier)

    bot_state.record_position_open.assert_not_called()
    bot_state.record_position_close.assert_not_called()


# ---------------------------------------------------------------------------
# Ledger updates from the signal path
# ---------------------------------------------------------------------------


def _run_signal_check_with_result(sample_strategy_config, signal, exec_result, dry_run=False):
    now_et = datetime(2024, 1, 2, 9, 40, tzinfo=ET)
    market_data = Mock()
    market_data.get_latest_bars.return_value = _make_ohlcv_df()
    market_data.get_current_price.return_value = 150.0

    signal_engine = Mock()
    signal_engine.generate_signal.return_value = {
        "signal": signal, "confidence": 0.8, "reason": "test",
    }
    order_manager = Mock()
    order_manager.dry_run = dry_run
    order_manager.execute_signal.return_value = exec_result

    broker = Mock()
    broker.get_account.return_value = Mock(equity=100000.0)
    broker.get_all_positions.return_value = []
    bot_state = Mock()

    main_module._check_signal_for_strategy(
        sample_strategy_config, now_et, set(), {},
        market_data,
        {sample_strategy_config.ticker: signal_engine},
        {sample_strategy_config.ticker: order_manager},
        None, broker, Mock(),
        main_module.GlobalRiskManager(), bot_state,
    )
    return bot_state


def test_buy_success_records_position_in_ledger(sample_strategy_config):
    bot_state = _run_signal_check_with_result(
        sample_strategy_config, "BUY",
        {"status": "success", "order_id": "o1", "filled_qty": 10, "filled_price": 150.0},
    )
    bot_state.record_position_open.assert_called_once_with(
        sample_strategy_config.ticker, 10, 150.0
    )
    bot_state.record_entry.assert_called_once_with(sample_strategy_config.ticker)


def test_sell_success_clears_ledger_and_tracking(sample_strategy_config):
    bot_state = _run_signal_check_with_result(
        sample_strategy_config, "SELL",
        {"status": "success", "order_id": "o2", "filled_qty": 10, "filled_price": 160.0},
    )
    bot_state.record_position_close.assert_called_once_with(sample_strategy_config.ticker)
    bot_state.clear_position_high.assert_called_once_with(sample_strategy_config.ticker)
    bot_state.clear_entry_timestamp.assert_called_once_with(sample_strategy_config.ticker)


def test_dry_run_buy_does_not_touch_ledger(sample_strategy_config):
    """Dry-run fills place no real order - recording them in the ledger would
    make the next reconciliation see phantom drift and auto-halt."""
    bot_state = _run_signal_check_with_result(
        sample_strategy_config, "BUY",
        {"status": "success", "order_id": "DRY_RUN_x", "filled_qty": 10, "filled_price": 150.0},
        dry_run=True,
    )
    bot_state.record_position_open.assert_not_called()


def test_blocked_trade_does_not_touch_ledger(sample_strategy_config):
    bot_state = _run_signal_check_with_result(
        sample_strategy_config, "BUY",
        {"status": "blocked", "reason": "risk limit"},
    )
    bot_state.record_position_open.assert_not_called()


# ---------------------------------------------------------------------------
# _restore_engine_states()
# ---------------------------------------------------------------------------


def _restore_setup(sample_strategy_config, ledger=None, saved=None, position_high=None):
    engine = Mock()
    engine.get_state.return_value = {"in_position": True, "entry_price": 1.0, "peak_price": 1.0}
    bot_state = Mock()
    bot_state.get_open_positions.return_value = ledger or {}
    bot_state.get_engine_state.return_value = saved
    bot_state.get_position_high.return_value = position_high
    main_module._restore_engine_states(
        [sample_strategy_config], {sample_strategy_config.ticker: engine}, bot_state
    )
    return engine, bot_state


def test_restore_engine_states_no_ledger_position_forces_flat(sample_strategy_config):
    """Saved in-position state with no ledger position (closed while bot was
    down) must be sanitized to flat - but position-free bookkeeping (vwap
    cooldown counters) survives the restart."""
    engine, bot_state = _restore_setup(
        sample_strategy_config,
        ledger={},
        saved={"in_position": True, "entry_price": 150.0, "peak_price": 160.0,
               "vwap_position": 0, "vwap_bars_since_signal": 2},
    )
    restored = engine.restore_state.call_args[0][0]
    assert restored["in_position"] is False
    assert restored["entry_price"] == 0.0
    assert restored["vwap_bars_since_signal"] == 2  # bookkeeping preserved
    bot_state.save_engine_state.assert_called_once()


def test_restore_engine_states_vwap_long_without_ledger_reset(sample_strategy_config):
    """vwap_position=1 implies a long the broker doesn't have - reset it;
    a short bookkeeping state (-1) implies no position and is kept."""
    engine, bot_state = _restore_setup(
        sample_strategy_config,
        ledger={},
        saved={"in_position": False, "vwap_position": 1, "vwap_bars_since_signal": 5},
    )
    restored = engine.restore_state.call_args[0][0]
    assert restored["vwap_position"] == 0

    engine2, _ = _restore_setup(
        sample_strategy_config,
        ledger={},
        saved={"in_position": False, "vwap_position": -1, "vwap_bars_since_signal": 5},
    )
    restored2 = engine2.restore_state.call_args[0][0]
    assert restored2["vwap_position"] == -1  # short bookkeeping survives


def test_restore_engine_states_ledger_and_saved_state_restores(sample_strategy_config):
    ticker = sample_strategy_config.ticker
    saved = {"in_position": True, "entry_price": 150.0, "peak_price": 172.0}
    engine, bot_state = _restore_setup(
        sample_strategy_config,
        ledger={ticker: {"qty": 10, "entry_price": 150.0}},
        saved=saved,
    )
    engine.restore_state.assert_called_once_with(saved)
    bot_state.save_engine_state.assert_called_once()


def test_restore_engine_states_ledger_without_saved_state_rebuilds(sample_strategy_config):
    """Position in ledger but no saved engine state (pre-persistence state
    file): rebuild in-position state from ledger entry + tracked high."""
    ticker = sample_strategy_config.ticker
    engine, bot_state = _restore_setup(
        sample_strategy_config,
        ledger={ticker: {"qty": 10, "entry_price": 150.0}},
        saved=None,
        position_high=165.0,
    )
    engine.restore_state.assert_called_once_with(
        {"in_position": True, "entry_price": 150.0, "peak_price": 165.0}
    )


def test_restore_engine_states_flat_everywhere_is_noop(sample_strategy_config):
    engine, bot_state = _restore_setup(sample_strategy_config, ledger={}, saved=None)
    engine.restore_state.assert_not_called()
    bot_state.clear_engine_state.assert_not_called()


# ---------------------------------------------------------------------------
# Warmup must not consume stateful entries; exits must reset engine state
# ---------------------------------------------------------------------------


def test_warmup_snapshots_and_restores_engine_state(sample_strategy_config):
    """generate_signal() mutates stateful engines - the warmup throwaway call
    must not leave that mutation behind (it would consume the real entry)."""
    market_data = Mock()
    market_data.get_latest_bars.return_value = pd.DataFrame({"close": [1, 2, 3]})

    engine = Mock()
    pre_state = {"in_position": False, "entry_price": 0.0, "peak_price": 0.0}
    engine.get_state.return_value = pre_state
    engine.generate_signal.return_value = {
        "warmup_complete": True, "signal": "BUY", "confidence": 0.8,
    }

    main_module._run_startup_warmup(
        [sample_strategy_config], market_data,
        {sample_strategy_config.ticker: engine}, Mock(),
    )

    engine.restore_state.assert_called_once_with(pre_state)


def test_exit_close_resets_engine_state():
    """A SL/TP close happens outside the signal engine - the engine must be
    reset to flat and the reset persisted, or it never re-enters."""
    pos = _mock_position()
    broker = Mock()
    broker.get_all_positions.return_value = [pos]
    market_data = Mock()
    market_data.get_current_price.return_value = 90.0

    order_manager = Mock()
    order_manager.check_exits.return_value = [
        {"ticker": "AAPL", "reason": "Stop loss hit", "current_price": 90.0}
    ]
    order_manager.close_position.return_value = {"status": "success", "order_id": "o9"}
    order_manager.config.risk.commission_per_trade = 0.0

    engine = Mock()
    bot_state = Mock()
    app_config = Mock(dry_run=False)

    main_module._run_exit_checks(
        broker, market_data, {"AAPL": order_manager}, bot_state, app_config,
        Mock(), main_module.GlobalRiskManager(), {"AAPL": engine},
    )

    engine.restore_state.assert_called_once_with(
        {"in_position": False, "entry_price": 0.0, "peak_price": 0.0}
    )
    bot_state.save_engine_state.assert_called()


def test_signal_check_persists_engine_state(sample_strategy_config):
    bot_state = _run_signal_check_with_result(
        sample_strategy_config, "HOLD",
        {"status": "blocked", "reason": "non-actionable"},
    )
    bot_state.save_engine_state.assert_called_once()


# ---------------------------------------------------------------------------
# _wire_min_hold_checkers()
# ---------------------------------------------------------------------------


def test_wire_min_hold_checkers_greenblatt_only(sample_strategy_config):
    """Only greenblatt_weekly engines get the min-hold gate; the checker must
    call BotState.is_min_hold_met with the config's ticker and min_hold_bars."""
    gb_cfg = sample_strategy_config.model_copy(deep=True)
    gb_cfg.strategy.name = "greenblatt_weekly"
    gb_cfg.strategy.parameters = {"min_hold_bars": 26}
    gb_cfg.ticker = "META"

    gb_engine = Mock(min_hold_checker=None)
    other_engine = Mock(min_hold_checker=None)
    engine_map = {"META": gb_engine, sample_strategy_config.ticker: other_engine}

    bot_state = Mock()
    bot_state.is_min_hold_met.return_value = False

    main_module._wire_min_hold_checkers(
        [gb_cfg, sample_strategy_config], engine_map, bot_state
    )

    assert other_engine.min_hold_checker is None
    assert gb_engine.min_hold_checker is not None
    assert gb_engine.min_hold_checker() is False
    bot_state.is_min_hold_met.assert_called_once_with("META", 26)


def test_wire_min_hold_checkers_defaults_to_52_weeks(sample_strategy_config):
    gb_cfg = sample_strategy_config.model_copy(deep=True)
    gb_cfg.strategy.name = "greenblatt_weekly"
    gb_cfg.strategy.parameters = {}

    engine = Mock(min_hold_checker=None)
    bot_state = Mock()
    bot_state.is_min_hold_met.return_value = True

    main_module._wire_min_hold_checkers(
        [gb_cfg], {gb_cfg.ticker: engine}, bot_state
    )

    assert engine.min_hold_checker() is True
    bot_state.is_min_hold_met.assert_called_once_with(gb_cfg.ticker, 52)


# ---------------------------------------------------------------------------
# _build_screener() / _run_monthly_screener()
# ---------------------------------------------------------------------------


def test_build_screener_disabled_without_universe(monkeypatch):
    monkeypatch.delenv("SCREENER_UNIVERSE", raising=False)
    assert main_module._build_screener() is None


def test_build_screener_parses_universe(monkeypatch):
    monkeypatch.setenv("SCREENER_UNIVERSE", "aapl, msft ,NVDA,")
    monkeypatch.setenv("SCREENER_TOP_N", "7")
    screener = main_module._build_screener()
    assert screener.universe == ["AAPL", "MSFT", "NVDA"]
    assert screener.top_n == 7


def _screener_candidate(ticker="AAPL"):
    c = Mock()
    c.ticker = ticker
    c.combined_rank = 3
    c.earnings_yield = 0.05
    c.return_on_equity = 0.30
    return c


def test_run_monthly_screener_runs_once_per_month():
    screener = Mock()
    screener.run.return_value = [_screener_candidate()]
    bot_state = Mock()
    bot_state.get_last_screener_month.return_value = None
    notifier = Mock()
    now_et = datetime(2026, 7, 15, 10, 0, tzinfo=ET)  # mid-month: still runs

    main_module._run_monthly_screener(screener, bot_state, notifier, now_et)

    screener.run.assert_called_once()
    bot_state.set_last_screener_month.assert_called_once_with("2026-07")
    notifier.send_message.assert_called_once()


def test_run_monthly_screener_skips_same_month():
    screener = Mock()
    bot_state = Mock()
    bot_state.get_last_screener_month.return_value = "2026-07"
    now_et = datetime(2026, 7, 15, 10, 0, tzinfo=ET)

    main_module._run_monthly_screener(screener, bot_state, Mock(), now_et)

    screener.run.assert_not_called()


def test_run_monthly_screener_none_is_noop():
    # Disabled screener: must not raise or touch state
    bot_state = Mock()
    main_module._run_monthly_screener(None, bot_state, Mock(), datetime(2026, 7, 1, tzinfo=ET))
    bot_state.set_last_screener_month.assert_not_called()


def test_run_monthly_screener_failure_alerts_and_skips_month():
    """A failed run must alert and NOT retry every 30s for the rest of the
    month - the month is recorded before the attempt."""
    screener = Mock()
    screener.run.side_effect = RuntimeError("yfinance down")
    bot_state = Mock()
    bot_state.get_last_screener_month.return_value = None
    notifier = Mock()
    now_et = datetime(2026, 8, 1, 9, 0, tzinfo=ET)

    main_module._run_monthly_screener(screener, bot_state, notifier, now_et)  # no raise

    bot_state.set_last_screener_month.assert_called_once_with("2026-08")
    notifier.send_error_alert.assert_called_once()
    notifier.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# Small-fix regression tests: EOD restart guard, qty-aware FIFO, fill-price P&L
# ---------------------------------------------------------------------------


def test_compute_daily_stats_zero_start_equity_reports_zero_pnl():
    """Restart after 4 PM with no persisted morning equity: P&L must be 0,
    not the entire account equity."""
    stats = main_module._compute_daily_stats([], start_equity=0.0, end_equity=100000.0)
    assert stats["pnl"] == 0.0


def test_compute_daily_stats_qty_aware_fifo():
    """One sell consuming two buy lots: win/loss judged against the
    weighted-average cost of the matched shares, not lot-per-lot."""
    orders = [
        {"ticker": "AAPL", "side": "BUY", "qty": 10, "price": 100.0, "timestamp": "t1"},
        {"ticker": "AAPL", "side": "BUY", "qty": 10, "price": 120.0, "timestamp": "t2"},
        # Sells all 20 @ 111: avg cost = 110 -> win (old price-only FIFO would
        # have judged against the first lot only)
        {"ticker": "AAPL", "side": "SELL", "qty": 20, "price": 111.0, "timestamp": "t3"},
    ]
    stats = main_module._compute_daily_stats(orders, 100000.0, 100020.0)
    assert stats["win_rate"] == 100.0


def test_compute_daily_stats_partial_sell_leaves_remaining_lot():
    orders = [
        {"ticker": "AAPL", "side": "BUY", "qty": 10, "price": 100.0, "timestamp": "t1"},
        {"ticker": "AAPL", "side": "SELL", "qty": 4, "price": 90.0, "timestamp": "t2"},   # loss
        {"ticker": "AAPL", "side": "SELL", "qty": 6, "price": 110.0, "timestamp": "t3"},  # win
    ]
    stats = main_module._compute_daily_stats(orders, 100000.0, 100000.0)
    assert stats["win_rate"] == 50.0


def test_exit_check_pnl_uses_fill_price_when_available():
    """P&L and the Telegram notification must use the close order's actual
    fill price, not the pre-order price the exit was decided on."""
    pos = _mock_position(avg_entry=100.0, current=90.0)
    broker = Mock()
    broker.get_all_positions.return_value = [pos]
    market_data = Mock()
    market_data.get_current_price.return_value = 90.0

    order_manager = Mock()
    order_manager.check_exits.return_value = [
        {"ticker": "AAPL", "reason": "Stop loss hit", "current_price": 90.0}
    ]
    # Actual fill came back worse than the decision price (slippage)
    order_manager.close_position.return_value = {
        "status": "success", "order_id": "o9",
        "filled_price": 89.5, "filled_qty": 10.0,
    }
    order_manager.config.risk.commission_per_trade = 0.0

    notifier = Mock()
    main_module._run_exit_checks(
        broker, market_data, {"AAPL": order_manager}, Mock(), Mock(dry_run=False),
        notifier, main_module.GlobalRiskManager(), {"AAPL": Mock()},
    )

    kwargs = notifier.send_position_closed_notification.call_args.kwargs
    assert kwargs["exit_price"] == 89.5
    assert kwargs["pnl"] == pytest.approx((89.5 - 100.0) * 10.0)


def test_exit_check_pnl_falls_back_to_decision_price():
    """No fill details yet (market order in flight): fall back to the price
    the exit was decided on."""
    pos = _mock_position(avg_entry=100.0, current=90.0)
    broker = Mock()
    broker.get_all_positions.return_value = [pos]
    market_data = Mock()
    market_data.get_current_price.return_value = 90.0

    order_manager = Mock()
    order_manager.check_exits.return_value = [
        {"ticker": "AAPL", "reason": "Stop loss hit", "current_price": 90.0}
    ]
    order_manager.close_position.return_value = {
        "status": "success", "order_id": "o9",
        "filled_price": None, "filled_qty": None,
    }
    order_manager.config.risk.commission_per_trade = 0.0

    notifier = Mock()
    main_module._run_exit_checks(
        broker, market_data, {"AAPL": order_manager}, Mock(), Mock(dry_run=False),
        notifier, main_module.GlobalRiskManager(), {"AAPL": Mock()},
    )

    kwargs = notifier.send_position_closed_notification.call_args.kwargs
    assert kwargs["exit_price"] == 90.0
