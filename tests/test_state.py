"""
Test State Persistence

Tests for alphalive/state.py — state persistence across Railway restarts.
"""

import os
import json
import tempfile
import pytest
from unittest.mock import Mock, patch

from alphalive.state import (
    BotState,
    reconstruct_daily_pnl,
    check_trailing_stop_requirements
)


@pytest.fixture
def temp_state_file():
    """Create a temporary state file."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        temp_path = f.name

    yield temp_path

    # Cleanup
    try:
        os.unlink(temp_path)
    except FileNotFoundError:
        pass


def test_state_survives_restart(temp_state_file):
    """Test that state survives restart (load from same file)."""
    # Create BotState and set values
    state1 = BotState(state_file=temp_state_file)
    state1.mark_morning_check_done("2024-03-11")
    state1.set_position_high("AAPL", 155.0)

    # Create NEW BotState from same file (simulates restart)
    state2 = BotState(state_file=temp_state_file)

    # Verify values were loaded
    assert state2.already_ran_morning_check("2024-03-11")
    assert state2.get_position_high("AAPL") == 155.0


def test_morning_check_not_duplicated(temp_state_file):
    """Test that morning check is not duplicated after restart."""
    # Mark morning check done for today
    state1 = BotState(state_file=temp_state_file)
    today = "2024-03-11"
    state1.mark_morning_check_done(today)

    # Create new BotState (simulates restart)
    state2 = BotState(state_file=temp_state_file)

    # Verify already_ran_morning_check returns True
    assert state2.already_ran_morning_check(today) is True

    # Different day should return False
    assert state2.already_ran_morning_check("2024-03-12") is False


def test_eod_not_duplicated(temp_state_file):
    """Test that EOD summary is not duplicated after restart."""
    # Mark EOD sent for today
    state1 = BotState(state_file=temp_state_file)
    today = "2024-03-11"
    state1.mark_eod_sent(today)

    # Create new BotState (simulates restart)
    state2 = BotState(state_file=temp_state_file)

    # Verify already_sent_eod returns True
    assert state2.already_sent_eod(today) is True

    # Different day should return False
    assert state2.already_sent_eod("2024-03-12") is False


def test_position_high_persists(temp_state_file):
    """Test that position high persists across restart."""
    # Set position high for AAPL
    state1 = BotState(state_file=temp_state_file)
    state1.set_position_high("AAPL", 155.0)

    # Create new BotState (simulates restart)
    state2 = BotState(state_file=temp_state_file)

    # Verify position high loaded
    assert state2.get_position_high("AAPL") == 155.0


def test_position_high_cleared(temp_state_file):
    """Test that position high can be cleared."""
    # Set position high
    state1 = BotState(state_file=temp_state_file)
    state1.set_position_high("AAPL", 155.0)

    # Clear it
    state1.clear_position_high("AAPL")

    # Create new BotState (simulates restart)
    state2 = BotState(state_file=temp_state_file)

    # Verify position high is None
    assert state2.get_position_high("AAPL") is None


def test_corrupted_state_file_returns_defaults(temp_state_file):
    """Test that corrupted state file returns defaults (no crash)."""
    # Write invalid JSON to state file
    with open(temp_state_file, 'w') as f:
        f.write("not valid json {[")

    # Create BotState (should not crash)
    state = BotState(state_file=temp_state_file)

    # Verify defaults loaded
    assert state.state["last_morning_check_date"] is None
    assert state.state["last_eod_summary_date"] is None
    assert state.state["daily_pnl"] == 0.0
    assert state.state["trades_today"] == []
    assert state.state["position_highs"] == {}


def test_daily_pnl_reconstruction_success():
    """Test successful daily P&L reconstruction from broker fills."""
    # Mock broker
    broker = Mock()
    broker.get_todays_fills.return_value = [
        {"pnl": 120.0},
        {"pnl": -45.0},
        {"pnl": 80.0}
    ]

    # Mock risk manager
    risk_manager = Mock()
    risk_manager.daily_pnl = 0.0

    # Reconstruct daily P&L
    daily_pnl = reconstruct_daily_pnl(broker, risk_manager)

    # Verify correct sum
    assert daily_pnl == 155.0
    assert risk_manager.daily_pnl == 155.0


def test_daily_pnl_reconstruction_failure(caplog):
    """Test daily P&L reconstruction failure (defaults to 0.0 with WARNING)."""
    # Mock broker that throws exception
    broker = Mock()
    broker.get_todays_fills.side_effect = ConnectionError("Network error")

    # Mock risk manager
    risk_manager = Mock()
    risk_manager.daily_pnl = 0.0

    # Reconstruct daily P&L (should not crash)
    daily_pnl = reconstruct_daily_pnl(broker, risk_manager)

    # Verify defaults to 0.0
    assert daily_pnl == 0.0
    assert risk_manager.daily_pnl == 0.0

    # Verify WARNING was logged
    assert any("reconstruction failed" in record.message.lower() for record in caplog.records)
    assert any("WARNING" in record.levelname for record in caplog.records)


def test_state_defaults_on_fresh_start(temp_state_file):
    """Test that fresh start (no state file) returns clean defaults."""
    # Make sure file doesn't exist
    try:
        os.unlink(temp_state_file)
    except FileNotFoundError:
        pass

    # Create BotState
    state = BotState(state_file=temp_state_file)

    # Verify defaults
    assert state.state["last_morning_check_date"] is None
    assert state.state["last_eod_summary_date"] is None
    assert state.state["daily_pnl"] == 0.0
    assert state.state["trades_today"] == []
    assert state.state["position_highs"] == {}
    assert state.state["version"] == "1.0"


def test_position_high_only_increases(temp_state_file):
    """Test that position high only increases (never decreases)."""
    state = BotState(state_file=temp_state_file)

    # Set initial high
    state.set_position_high("AAPL", 155.0)
    assert state.get_position_high("AAPL") == 155.0

    # Try to set lower value (should not update)
    state.set_position_high("AAPL", 150.0)
    assert state.get_position_high("AAPL") == 155.0

    # Set higher value (should update)
    state.set_position_high("AAPL", 160.0)
    assert state.get_position_high("AAPL") == 160.0


def test_multiple_position_highs(temp_state_file):
    """Test tracking multiple position highs simultaneously."""
    state = BotState(state_file=temp_state_file)

    # Set highs for multiple tickers
    state.set_position_high("AAPL", 155.0)
    state.set_position_high("TSLA", 200.0)
    state.set_position_high("MSFT", 350.0)

    # Create new state (simulates restart)
    state2 = BotState(state_file=temp_state_file)

    # Verify all persist
    assert state2.get_position_high("AAPL") == 155.0
    assert state2.get_position_high("TSLA") == 200.0
    assert state2.get_position_high("MSFT") == 350.0

    # Clear one
    state2.clear_position_high("TSLA")

    # Verify others still exist
    assert state2.get_position_high("AAPL") == 155.0
    assert state2.get_position_high("TSLA") is None
    assert state2.get_position_high("MSFT") == 350.0


def test_reset_daily(temp_state_file):
    """Test daily reset clears counters."""
    state = BotState(state_file=temp_state_file)

    # Set some state
    state.mark_morning_check_done("2024-03-11")
    state.mark_eod_sent("2024-03-11")
    state.state["daily_pnl"] = 450.0
    state.state["trades_today"] = [{"ticker": "AAPL", "pnl": 450.0}]
    state.save()

    # Reset for new day
    state.reset_daily("2024-03-12")

    # Verify counters reset
    assert state.state["daily_pnl"] == 0.0
    assert state.state["trades_today"] == []
    assert state.state["last_morning_check_date"] is None
    assert state.state["last_eod_summary_date"] is None

    # Position highs should NOT be reset (persist across days)
    state.set_position_high("AAPL", 155.0)
    state.reset_daily("2024-03-13")
    assert state.get_position_high("AAPL") == 155.0


def test_trailing_stop_enforcement_blocks_startup(sample_strategy_dict):
    """Test that trailing stops without persistent storage blocks startup."""
    from alphalive.strategy_schema import StrategySchema

    # Enable trailing stops
    sample_strategy_dict["risk"]["trailing_stop_enabled"] = True
    config = StrategySchema(**sample_strategy_dict)

    # Mock notifier
    notifier = Mock()

    # Set PERSISTENT_STORAGE to false
    with patch.dict(os.environ, {"PERSISTENT_STORAGE": "false"}, clear=False):
        # Should exit with code 1
        with pytest.raises(SystemExit) as exc_info:
            check_trailing_stop_requirements(config, notifier)

        assert exc_info.value.code == 1

        # Verify Telegram alert sent
        assert notifier.send_error_alert.called


def test_trailing_stop_enforcement_allows_with_persistent_storage(sample_strategy_dict):
    """Test that trailing stops with persistent storage allows startup."""
    from alphalive.strategy_schema import StrategySchema

    # Enable trailing stops
    sample_strategy_dict["risk"]["trailing_stop_enabled"] = True
    config = StrategySchema(**sample_strategy_dict)

    # Set PERSISTENT_STORAGE to true
    with patch.dict(os.environ, {"PERSISTENT_STORAGE": "true"}, clear=False):
        # Should not raise
        check_trailing_stop_requirements(config, notifier=None)


def test_trailing_stop_enforcement_allows_without_trailing_stops(sample_strategy_dict):
    """Test that startup works without trailing stops (regardless of persistent storage)."""
    from alphalive.strategy_schema import StrategySchema

    # Disable trailing stops
    sample_strategy_dict["risk"]["trailing_stop_enabled"] = False
    config = StrategySchema(**sample_strategy_dict)

    # PERSISTENT_STORAGE not set (defaults to false)
    with patch.dict(os.environ, {}, clear=False):
        # Should not raise
        check_trailing_stop_requirements(config, notifier=None)


# ============================================================================
# Health Endpoint Tests
# ============================================================================

def test_health_returns_200_with_correct_secret(sample_strategy_dict):
    """Test health endpoint returns 200 with correct secret."""
    from alphalive.health import HealthServer
    from alphalive.strategy_schema import StrategySchema
    import requests
    import time

    config = StrategySchema(**sample_strategy_dict)

    # Set HEALTH_SECRET
    with patch.dict(os.environ, {"HEALTH_SECRET": "test_secret_123"}, clear=False):
        # Create health server
        health = HealthServer(
            port=8081,  # Use different port to avoid conflicts
            health_data={
                "warmup_complete": True,
                "bars_loaded": 252,
                "trading_paused": False,
                "dry_run": False,
                "paper": True
            }
        )
        health.start()

        # Wait for server to start
        time.sleep(0.1)

        try:
            # Make request with correct secret
            response = requests.get(
                "http://localhost:8081/",
                headers={"X-Health-Secret": "test_secret_123"},
                timeout=2
            )

            # Verify response
            assert response.status_code == 200

            data = response.json()
            assert data["status"] == "ok"
            assert "uptime" in data
            assert "last_check" in data
            assert data["warmup_complete"] is True
            assert data["bars_loaded"] == 252
            assert data["trading_paused"] is False
            assert data["dry_run"] is False
            assert data["paper"] is True

        finally:
            health.stop()


def test_health_returns_401_with_wrong_secret(sample_strategy_dict):
    """Test health endpoint returns 401 with wrong secret."""
    from alphalive.health import HealthServer
    from alphalive.strategy_schema import StrategySchema
    import requests
    import time

    config = StrategySchema(**sample_strategy_dict)

    # Set HEALTH_SECRET
    with patch.dict(os.environ, {"HEALTH_SECRET": "correct_secret"}, clear=False):
        # Create health server
        health = HealthServer(port=8082, health_data={})
        health.start()

        # Wait for server to start
        time.sleep(0.1)

        try:
            # Make request with WRONG secret
            response = requests.get(
                "http://localhost:8082/",
                headers={"X-Health-Secret": "wrong_secret"},
                timeout=2
            )

            # Verify 401 unauthorized
            assert response.status_code == 401

            data = response.json()
            assert "error" in data
            assert data["error"] == "Unauthorized"

        finally:
            health.stop()


def test_health_returns_503_when_secret_not_configured(sample_strategy_dict, caplog):
    """Test health endpoint returns 503 when HEALTH_SECRET not configured."""
    from alphalive.health import HealthServer
    from alphalive.strategy_schema import StrategySchema
    import requests
    import time

    config = StrategySchema(**sample_strategy_dict)

    # Make sure HEALTH_SECRET is NOT set
    env_without_secret = {k: v for k, v in os.environ.items() if k != "HEALTH_SECRET"}

    with patch.dict(os.environ, env_without_secret, clear=True):
        # Create health server (should be disabled)
        health = HealthServer(port=8083, health_data={})
        health.start()

        # Wait for server to start
        time.sleep(0.1)

        try:
            # Make request (no secret header needed, endpoint is disabled)
            response = requests.get(
                "http://localhost:8083/",
                timeout=2
            )

            # Verify 503 service unavailable
            assert response.status_code == 503

            data = response.json()
            assert "error" in data
            assert data["error"] == "Health endpoint disabled"

            # Verify warning was logged
            assert any(
                "Health endpoint disabled" in record.message
                for record in caplog.records
            )

        finally:
            health.stop()
