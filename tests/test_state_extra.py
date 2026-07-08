"""
Extended BotState coverage: minimum-hold entry-timestamp tracking,
dashboard pause flag, startup marking, and load/save error handling.
tests/test_state.py already covers morning-check/EOD/position-high
tracking and trailing-stop startup enforcement - this fills in the
entry-timestamp and dashboard-pause methods it doesn't reach.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from alphalive.state import BotState

ET = ZoneInfo("America/New_York")


@pytest.fixture
def temp_state_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        temp_path = f.name
    yield temp_path
    try:
        os.unlink(temp_path)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Entry timestamp / minimum hold enforcement
# ---------------------------------------------------------------------------


def test_record_and_clear_entry_timestamp(temp_state_file):
    state = BotState(temp_state_file)
    state.record_entry("AAPL")

    assert "AAPL" in state.state["entry_timestamps"]

    state.clear_entry_timestamp("AAPL")
    assert "AAPL" not in state.state["entry_timestamps"]


def test_clear_entry_timestamp_missing_ticker_noop(temp_state_file):
    state = BotState(temp_state_file)
    # Should not raise even though AAPL was never recorded
    state.clear_entry_timestamp("AAPL")


def test_is_min_hold_met_no_record_allows_exit(temp_state_file):
    state = BotState(temp_state_file)
    assert state.is_min_hold_met("AAPL", min_hold_weeks=52) is True


def test_is_min_hold_met_recent_entry_blocks_exit(temp_state_file):
    state = BotState(temp_state_file)
    state.state["entry_timestamps"]["AAPL"] = datetime.now(ET).isoformat()

    assert state.is_min_hold_met("AAPL", min_hold_weeks=52) is False


def test_is_min_hold_met_old_entry_allows_exit(temp_state_file):
    state = BotState(temp_state_file)
    old_entry = datetime.now(ET) - timedelta(weeks=60)
    state.state["entry_timestamps"]["AAPL"] = old_entry.isoformat()

    assert state.is_min_hold_met("AAPL", min_hold_weeks=52) is True


def test_is_min_hold_met_corrupt_timestamp_fails_safe(temp_state_file):
    state = BotState(temp_state_file)
    state.state["entry_timestamps"]["AAPL"] = "not-a-timestamp"

    assert state.is_min_hold_met("AAPL", min_hold_weeks=52) is True


# ---------------------------------------------------------------------------
# Dashboard pause flag
# ---------------------------------------------------------------------------


def test_dashboard_pause_set_and_read(temp_state_file):
    state = BotState(temp_state_file)
    assert state.is_dashboard_paused() is False

    state.set_dashboard_pause(True)
    assert state.is_dashboard_paused() is True


def test_check_dashboard_paused_reads_from_disk(temp_state_file):
    writer = BotState(temp_state_file)
    writer.set_dashboard_pause(True)

    # A second BotState instance re-reading the file should see the flag
    reader = BotState(temp_state_file)
    assert reader.check_dashboard_paused() is True


# ---------------------------------------------------------------------------
# mark_startup()
# ---------------------------------------------------------------------------


def test_mark_startup_sets_timestamp(temp_state_file):
    state = BotState(temp_state_file)
    state.mark_startup()

    assert state.state["last_startup"] is not None


# ---------------------------------------------------------------------------
# _load() / save() error handling
# ---------------------------------------------------------------------------


def test_load_generic_exception_returns_defaults(temp_state_file):
    with patch("builtins.open", side_effect=PermissionError("denied")):
        state = BotState(temp_state_file)

    assert state.state["daily_pnl"] == 0.0
    assert state.state["position_highs"] == {}


def test_save_failure_logged_not_raised(temp_state_file):
    state = BotState(temp_state_file)

    with patch("os.replace", side_effect=OSError("disk full")):
        # Should not raise even though the underlying rename fails
        state.set_position_high("AAPL", 150.0)

    assert state.state["position_highs"]["AAPL"] == 150.0
