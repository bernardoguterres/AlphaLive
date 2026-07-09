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
        market_data, {}, {}, None, None, Mock(), Mock(), main_module.GlobalRiskManager(),
    )

    market_data.get_latest_bars.assert_not_called()
    assert sample_strategy_config.ticker not in morning_checks_done


def test_check_signal_for_strategy_1day_skips_if_already_done(sample_strategy_config):
    now_et = datetime(2024, 1, 2, 9, 40, tzinfo=ET)
    morning_checks_done = {sample_strategy_config.ticker}
    market_data = Mock()

    main_module._check_signal_for_strategy(
        sample_strategy_config, now_et, morning_checks_done, {},
        market_data, {}, {}, None, None, Mock(), Mock(), main_module.GlobalRiskManager(),
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
        market_data, signal_engine_map, order_manager_map, None, None, Mock(), Mock(),
        main_module.GlobalRiskManager(),
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
        market_data, signal_engine_map, order_manager_map, None, None, broker, notifier,
        main_module.GlobalRiskManager(),
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
        market_data, signal_engine_map, order_manager_map, None, None, broker, notifier,
        main_module.GlobalRiskManager(),
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
        market_data, signal_engine_map, order_manager_map, None, None, broker, notifier,
        main_module.GlobalRiskManager(),
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
        market_data, signal_engine_map, {}, None, None, Mock(), notifier,
        main_module.GlobalRiskManager(),
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
        market_data, {}, {}, None, None, Mock(), notifier,
        main_module.GlobalRiskManager(),
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
        market_data, {}, {}, None, None, Mock(), notifier,
        main_module.GlobalRiskManager(),
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
            market_data, {}, {}, None, None, Mock(), Mock(),
            main_module.GlobalRiskManager(),
        )
        mock_should_run.assert_called_once()
    market_data.get_latest_bars.assert_not_called()


def test_check_signal_for_strategy_with_pre_execution_checks_blocked(sample_strategy_config):
    """When DeepLOB/AlphaSignal clients are wired in and block execution, no order is placed."""
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

    deeplob_client = Mock()
    alphasignal_client = Mock()

    with patch(
        "alphalive.main.run_pre_execution_checks",
        return_value=(False, {"pred": 1}, True, {}),
    ):
        main_module._check_signal_for_strategy(
            sample_strategy_config, now_et, set(), {},
            market_data, signal_engine_map, order_manager_map,
            deeplob_client, alphasignal_client, Mock(), Mock(),
            main_module.GlobalRiskManager(),
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
# _run_position_reconciliation()
# ---------------------------------------------------------------------------


def test_run_position_reconciliation_no_drift():
    pos = _mock_position()
    broker = Mock()
    broker.get_all_positions.return_value = [pos]

    order_manager = Mock()
    order_manager.get_order_history.return_value = [
        {"ticker": "AAPL", "status": "filled"}
    ]
    order_manager_map = {"AAPL": order_manager}
    app_config = Mock(trading_paused=False)
    notifier = Mock()

    with patch.dict("os.environ", {}, clear=False):
        main_module._run_position_reconciliation(broker, order_manager_map, app_config, notifier)

    notifier.send_alert.assert_not_called()
    assert app_config.trading_paused is False


def test_run_position_reconciliation_alpaca_only_drift_pauses_trading():
    """Broker has a position the bot never recorded - the first drift branch."""
    pos = _mock_position()
    broker = Mock()
    broker.get_all_positions.return_value = [pos]

    order_manager_map = {"AAPL": Mock(get_order_history=Mock(return_value=[]))}
    app_config = Mock(trading_paused=False)
    notifier = Mock()

    with patch.dict("os.environ", {}, clear=False):
        main_module._run_position_reconciliation(broker, order_manager_map, app_config, notifier)

    assert app_config.trading_paused is True
    assert notifier.send_alert.call_count == 2  # per-ticker alert + halt alert


def test_run_position_reconciliation_internal_only_drift_pauses_trading():
    """Bot recorded a fill the broker no longer shows - the second drift branch."""
    broker = Mock()
    broker.get_all_positions.return_value = []

    order_manager = Mock()
    order_manager.get_order_history.return_value = [{"ticker": "AAPL", "status": "filled"}]
    order_manager_map = {"AAPL": order_manager}
    app_config = Mock(trading_paused=False)
    notifier = Mock()

    with patch.dict("os.environ", {}, clear=False):
        main_module._run_position_reconciliation(broker, order_manager_map, app_config, notifier)

    assert app_config.trading_paused is True


def test_run_position_reconciliation_broker_error_caught():
    broker = Mock()
    broker.get_all_positions.side_effect = RuntimeError("boom")
    app_config = Mock(trading_paused=False)
    notifier = Mock()

    # Should not raise
    main_module._run_position_reconciliation(broker, {}, app_config, notifier)

    notifier.send_alert.assert_not_called()
