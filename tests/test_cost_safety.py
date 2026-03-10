"""
Test Cost Safety Limits

Tests for B17 cost safety features:
- Max trades per day auto-pause
- API call limits and hourly reset
- Broker degraded mode
- Signal generation timeout
"""

import os
import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from alphalive.execution.risk_manager import RiskManager
from alphalive.strategy_schema import Risk, Execution, SafetyLimits


ET = ZoneInfo("America/New_York")


@pytest.fixture
def risk_config():
    """Standard risk config."""
    return Risk(
        stop_loss_pct=2.0,
        take_profit_pct=5.0,
        max_position_size_pct=10.0,
        max_daily_loss_pct=3.0,
        max_open_positions=5,
        portfolio_max_positions=10,
        trailing_stop_enabled=False,
        trailing_stop_pct=None,
        commission_per_trade=0.0
    )


@pytest.fixture
def execution_config():
    """Standard execution config."""
    return Execution(
        order_type="market",
        limit_offset_pct=0.1,
        cooldown_bars=1
    )


@pytest.fixture
def safety_limits_default():
    """Default safety limits."""
    return SafetyLimits()  # Uses defaults


@pytest.fixture
def safety_limits_low():
    """Low limits for testing."""
    return SafetyLimits(
        max_trades_per_day=10,
        max_api_calls_per_hour=100,
        signal_generation_timeout_seconds=5.0,
        broker_degraded_mode_threshold_failures=3
    )


@pytest.fixture
def notifier_mock():
    """Mock notifier."""
    mock = Mock()
    mock.send_alert = Mock(return_value=True)
    return mock


def test_max_trades_per_day_auto_pauses(risk_config, execution_config, safety_limits_low, notifier_mock):
    """Verify hitting trade limit auto-pauses trading."""
    rm = RiskManager(
        risk_config=risk_config,
        execution_config=execution_config,
        strategy_name="test_strategy",
        safety_limits=safety_limits_low,
        notifier=notifier_mock
    )

    # Place 10 trades (should succeed)
    for i in range(10):
        can_trade, reason = rm.can_trade(
            ticker="AAPL",
            signal="BUY",
            account_equity=100000.0,
            current_positions_count=0,
            total_portfolio_positions=0
        )
        assert can_trade, f"Trade {i+1} should be allowed"
        rm.record_trade("AAPL", 100.0)  # Record each trade

    # Verify 10 trades recorded
    assert rm.trades_today == 10

    # 11th trade should be blocked and auto-pause should trigger
    can_trade, reason = rm.can_trade(
        ticker="AAPL",
        signal="BUY",
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0
    )

    assert not can_trade
    assert "Max trades/day" in reason
    assert rm.trading_paused_manual  # Auto-paused
    assert notifier_mock.send_alert.called


def test_api_call_limit_auto_pauses(risk_config, execution_config, safety_limits_low, notifier_mock):
    """Verify API call limit triggers auto-pause."""
    rm = RiskManager(
        risk_config=risk_config,
        execution_config=execution_config,
        strategy_name="test_strategy",
        safety_limits=safety_limits_low,
        notifier=notifier_mock
    )

    # Make 100 API calls
    for i in range(100):
        rm.record_api_call("get_latest_bars")

    assert rm.api_calls_this_hour == 100

    # Next can_trade() should auto-pause
    can_trade, reason = rm.can_trade(
        ticker="AAPL",
        signal="BUY",
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0
    )

    assert not can_trade
    assert "API call limit" in reason
    assert rm.trading_paused_manual  # Auto-paused
    assert notifier_mock.send_alert.called


def test_api_counter_resets_hourly(risk_config, execution_config, safety_limits_default, notifier_mock):
    """Verify API call counter resets at top of hour."""
    rm = RiskManager(
        risk_config=risk_config,
        execution_config=execution_config,
        strategy_name="test_strategy",
        safety_limits=safety_limits_default,
        notifier=notifier_mock
    )

    # Set counter to 95 and set last reset to 1 hour ago
    rm.api_calls_this_hour = 95
    rm.last_hour_reset = datetime.now(ET) - timedelta(hours=1)

    # Record new API call (should trigger reset)
    rm.record_api_call("get_account")

    # Counter should be reset to 1 (current call)
    assert rm.api_calls_this_hour == 1


def test_broker_failure_threshold_triggers_degraded_mode(risk_config, execution_config, safety_limits_low, notifier_mock):
    """Verify consecutive failures trigger degraded mode."""
    rm = RiskManager(
        risk_config=risk_config,
        execution_config=execution_config,
        strategy_name="test_strategy",
        safety_limits=safety_limits_low,
        notifier=notifier_mock
    )

    # First failure
    rm.record_broker_failure(Exception("Connection timeout"))
    assert not rm.degraded_mode  # 1 failure
    assert rm.broker_consecutive_failures == 1

    # Second failure
    rm.record_broker_failure(Exception("Connection timeout"))
    assert not rm.degraded_mode  # 2 failures
    assert rm.broker_consecutive_failures == 2

    # Third failure → should trigger degraded mode
    rm.record_broker_failure(Exception("Connection timeout"))
    assert rm.degraded_mode  # 3 failures → degraded
    assert rm.broker_consecutive_failures == 3
    assert notifier_mock.send_alert.called


def test_broker_success_resets_failure_counter(risk_config, execution_config, safety_limits_default, notifier_mock):
    """Verify successful broker call resets failure counter."""
    rm = RiskManager(
        risk_config=risk_config,
        execution_config=execution_config,
        strategy_name="test_strategy",
        safety_limits=safety_limits_default,
        notifier=notifier_mock
    )

    # Simulate 2 failures
    rm.broker_consecutive_failures = 2

    # Successful call should reset
    rm.record_broker_success()

    assert rm.broker_consecutive_failures == 0


def test_degraded_mode_blocks_new_entries(risk_config, execution_config, safety_limits_default, notifier_mock):
    """Verify degraded mode blocks new entries."""
    rm = RiskManager(
        risk_config=risk_config,
        execution_config=execution_config,
        strategy_name="test_strategy",
        safety_limits=safety_limits_default,
        notifier=notifier_mock
    )

    # Enter degraded mode
    rm.enter_degraded_mode("Test broker failure")

    # Try to trade
    can_trade, reason = rm.can_trade(
        ticker="AAPL",
        signal="BUY",
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0
    )

    assert not can_trade
    assert "Degraded mode" in reason


def test_daily_counter_reset(risk_config, execution_config, safety_limits_default, notifier_mock):
    """Verify trades_today resets at market open."""
    rm = RiskManager(
        risk_config=risk_config,
        execution_config=execution_config,
        strategy_name="test_strategy",
        safety_limits=safety_limits_default,
        notifier=notifier_mock
    )

    # Simulate 15 trades
    rm.trades_today = 15

    # Reset counters
    rm.reset_daily()

    assert rm.trades_today == 0


def test_api_soft_limit_warning(risk_config, execution_config, safety_limits_low, notifier_mock, caplog):
    """Verify 80% API budget triggers warning (not halt)."""
    import logging
    caplog.set_level(logging.WARNING)

    rm = RiskManager(
        risk_config=risk_config,
        execution_config=execution_config,
        strategy_name="test_strategy",
        safety_limits=safety_limits_low,  # max=100
        notifier=notifier_mock
    )

    # Make 80 API calls (80% of 100)
    for i in range(80):
        rm.record_api_call("get_bars")

    # Try to trade (should succeed with warning)
    can_trade, reason = rm.can_trade(
        ticker="AAPL",
        signal="BUY",
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0
    )

    assert can_trade  # Still allowed
    assert "API call budget 80% used" in caplog.text  # Warning logged


def test_get_safety_stats(risk_config, execution_config, safety_limits_default, notifier_mock):
    """Verify get_safety_stats returns current statistics."""
    rm = RiskManager(
        risk_config=risk_config,
        execution_config=execution_config,
        strategy_name="test_strategy",
        safety_limits=safety_limits_default,
        notifier=notifier_mock
    )

    # Simulate some state
    rm.trades_today = 8
    rm.api_calls_this_hour = 67
    rm.degraded_mode = False
    rm.broker_consecutive_failures = 1

    stats = rm.get_safety_stats()

    assert stats["trades_today"] == 8
    assert stats["max_trades_per_day"] == 20
    assert stats["api_calls_this_hour"] == 67
    assert stats["max_api_calls_per_hour"] == 500
    assert stats["degraded_mode"] is False
    assert stats["broker_consecutive_failures"] == 1
    assert stats["broker_failure_threshold"] == 3
