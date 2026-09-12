"""
Tests for state persistence durability (objective 2 of the 2026-09-11
hardening pass): schema versioning/migration, corruption recovery via the
rolling backup, the PERSISTENT_STORAGE fail-loud guard, and the OS-
appropriate default path / hosted-execution warning.

All tests use tmp_path - never a real Railway Volume or /tmp.
"""

import json
import os

import pytest

from alphalive.state import (
    BotState,
    CURRENT_SCHEMA_VERSION,
    StateCorruptionError,
    StateSchemaError,
    default_state_file_path,
    is_hosted_execution,
)


def _write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


# ---------------------------------------------------------------------------
# Clean first startup / legacy state / migration
# ---------------------------------------------------------------------------


def test_clean_first_startup_uses_defaults(tmp_path):
    state = BotState(state_file=str(tmp_path / "state.json"))
    assert state.state["schema_version"] == CURRENT_SCHEMA_VERSION
    assert state.state["submission_intents"] == {}
    assert state.state["telegram_paused"] is False


def test_legacy_state_without_schema_version_is_migrated(tmp_path):
    path = tmp_path / "state.json"
    _write_json(
        path,
        {
            "last_morning_check_date": "2026-01-01",
            "position_highs": {"AAPL": 150.0},
            "version": "1.0",
        },
    )
    state = BotState(state_file=str(path))
    assert state.state["schema_version"] == CURRENT_SCHEMA_VERSION
    # Old data preserved
    assert state.state["last_morning_check_date"] == "2026-01-01"
    assert state.state["position_highs"] == {"AAPL": 150.0}
    # New keys backfilled
    assert state.state["submission_intents"] == {}
    assert state.state["telegram_paused"] is False


def test_unsupported_future_schema_version_raises(tmp_path):
    path = tmp_path / "state.json"
    _write_json(path, {"schema_version": CURRENT_SCHEMA_VERSION + 1})
    with pytest.raises(StateSchemaError):
        BotState(state_file=str(path))


# ---------------------------------------------------------------------------
# Restart with durable state
# ---------------------------------------------------------------------------


def test_restart_with_durable_state_preserves_data(tmp_path):
    path = str(tmp_path / "state.json")
    state1 = BotState(state_file=path)
    state1.set_position_high("AAPL", 155.0)
    state1.record_entry("AAPL")
    intent = state1.create_submission_intent(
        ticker="AAPL",
        side="BUY",
        qty=5,
        order_type="market",
        strategy_name="ma_crossover",
    )

    state2 = BotState(state_file=path)  # simulates restart
    assert state2.get_position_high("AAPL") == 155.0
    assert state2.get_submission_intent(intent["intent_id"])["ticker"] == "AAPL"


# ---------------------------------------------------------------------------
# Interrupted write / corrupt file / backup recovery
# ---------------------------------------------------------------------------


def test_interrupted_write_leaves_previous_valid_state_untouched(tmp_path):
    """A crash mid-write only ever touches the .tmp file - os.replace is
    atomic, so the real file is either the old contents or the fully-new
    contents, never a partial write. Simulated here by writing a valid
    state, then leaving a stray/partial .tmp file around (as a real crash
    would) and confirming a fresh load still sees the last complete save."""
    path = tmp_path / "state.json"
    state = BotState(state_file=str(path))
    state.set_position_high("AAPL", 100.0)

    # Simulate a crash mid-write: a stray temp file with partial content.
    with open(f"{path}.tmp", "w") as f:
        f.write('{"position_highs": {"AAPL": 999.0}, "trunc')

    reloaded = BotState(state_file=str(path))
    assert reloaded.get_position_high("AAPL") == 100.0  # not the truncated value


def test_corrupt_state_file_recovers_from_backup(tmp_path):
    path = tmp_path / "state.json"
    state = BotState(state_file=str(path))
    state.set_position_high("AAPL", 123.0)
    assert os.path.exists(f"{path}.bak")

    # Corrupt the primary file directly.
    with open(path, "w") as f:
        f.write("{not valid json")

    recovered = BotState(state_file=str(path))
    assert recovered.get_position_high("AAPL") == 123.0


def test_corrupt_state_file_no_backup_no_persistent_storage_uses_defaults(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("PERSISTENT_STORAGE", raising=False)
    path = tmp_path / "state.json"
    with open(path, "w") as f:
        f.write("{not valid json")

    state = BotState(state_file=str(path))
    assert state.state["position_highs"] == {}


def test_corrupt_state_file_no_backup_persistent_storage_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSISTENT_STORAGE", "true")
    path = tmp_path / "state.json"
    with open(path, "w") as f:
        f.write("{not valid json")

    with pytest.raises(StateCorruptionError):
        BotState(state_file=str(path))


def test_state_file_not_a_json_object_is_treated_as_corrupt(tmp_path):
    path = tmp_path / "state.json"
    _write_json(path, [1, 2, 3])
    state = BotState(state_file=str(path))
    assert state.state["position_highs"] == {}


# ---------------------------------------------------------------------------
# Hosted execution / default path policy
# ---------------------------------------------------------------------------


def test_default_state_file_path_is_not_tmp():
    path = default_state_file_path()
    assert not path.startswith("/tmp")
    assert "alphalive" in path.lower()


def test_is_hosted_execution_false_locally(monkeypatch):
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("RAILWAY_PROJECT_ID", raising=False)
    assert is_hosted_execution() is False


def test_is_hosted_execution_true_on_railway(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    assert is_hosted_execution() is True


def test_config_warns_on_ephemeral_state_path_when_hosted(
    monkeypatch, caplog, tmp_path
):
    """config.py's _warn_if_ephemeral_state_path must fire a loud warning
    for a hosted deploy with an ephemeral STATE_FILE and no persistent
    storage configured - and stay silent otherwise."""
    from alphalive import config as config_module

    class _FakeAppConfig:
        state_file = "/tmp/alphalive_state.json"
        persistent_storage = False

    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    with caplog.at_level("WARNING"):
        config_module._warn_if_ephemeral_state_path(_FakeAppConfig())
    assert any("ephemeral" in r.message for r in caplog.records)

    caplog.clear()
    _FakeAppConfig.persistent_storage = True
    with caplog.at_level("WARNING"):
        config_module._warn_if_ephemeral_state_path(_FakeAppConfig())
    assert not any("ephemeral" in r.message for r in caplog.records)

    caplog.clear()
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)
    _FakeAppConfig.persistent_storage = False
    with caplog.at_level("WARNING"):
        config_module._warn_if_ephemeral_state_path(_FakeAppConfig())
    assert not any("ephemeral" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Tests always use temporary storage (meta-check, cheap sanity guard)
# ---------------------------------------------------------------------------


def test_this_suite_never_touches_the_real_default_path(tmp_path):
    # Every test above passes an explicit tmp_path-derived state_file; this
    # just documents/asserts the convention rather than testing behavior.
    default_path = default_state_file_path()
    assert str(tmp_path) not in default_path
