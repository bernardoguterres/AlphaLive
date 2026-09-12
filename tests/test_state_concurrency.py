"""
Tests for shared-state concurrency (2026-09-11 pass 2, objective 5):
BotState is mutated concurrently by the main loop thread and the Telegram
listener's background polling thread within one process. These tests prove
the @_synchronized locking added to state.py prevents corruption/crashes
under real concurrent access, and that unrelated mutations don't clobber
each other.

All state files are tmp_path-backed; no real threads talk to Telegram or
Alpaca - only BotState itself is exercised concurrently.
"""

import json
import threading
import time

import pytest

from alphalive.state import BotState, INTENT_FILLED, INTENT_UNCERTAIN


@pytest.fixture
def state(tmp_path):
    return BotState(state_file=str(tmp_path / "state.json"))


def _run_concurrently(funcs, timeout=10):
    threads = [threading.Thread(target=f) for f in funcs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
        assert not t.is_alive(), "thread did not finish in time"


# ---------------------------------------------------------------------------
# Concurrent mutations cannot corrupt or overwrite each other
# ---------------------------------------------------------------------------


def test_concurrent_intent_creation_never_crashes_or_loses_entries(state):
    """Many threads creating submission intents for different tickers at
    once must not raise (e.g. "dictionary changed size during iteration"
    from an unguarded save()) and every intent must end up persisted."""
    errors = []

    def _create(n):
        try:
            state.create_submission_intent(
                ticker=f"T{n}",
                side="BUY",
                qty=1,
                order_type="market",
                strategy_name="x",
            )
        except Exception as e:
            errors.append(e)

    funcs = [(lambda n=i: _create(n)) for i in range(40)]
    _run_concurrently(funcs)

    assert errors == []
    assert len(state.list_all_intents()) == 40


def test_concurrent_saves_from_unrelated_mutations_do_not_corrupt_file(state):
    """One thread repeatedly updates submission intents while another
    repeatedly toggles the telegram pause flag - both trigger save() on
    the same BotState. The file must always be valid JSON when reloaded,
    never partially written or corrupted by an interleaved write."""
    errors = []

    def _intents():
        try:
            for i in range(30):
                intent = state.create_submission_intent(
                    ticker="AAPL",
                    side="BUY",
                    qty=1,
                    order_type="market",
                    strategy_name="x",
                )
                state.update_submission_intent(
                    intent["intent_id"], status=INTENT_FILLED
                )
        except Exception as e:
            errors.append(e)

    def _pause_toggle():
        try:
            for i in range(30):
                state.set_telegram_pause(i % 2 == 0)
        except Exception as e:
            errors.append(e)

    _run_concurrently([_intents, _pause_toggle])

    assert errors == []
    # File must be valid, loadable JSON.
    with open(state.state_file) as f:
        reloaded = json.load(f)
    assert isinstance(reloaded, dict)
    assert "submission_intents" in reloaded


def test_one_strategys_intent_creation_does_not_erase_anothers(state):
    """Simulates the main loop (strategy A, a BUY intent) and the Telegram
    thread (strategy B position close, a CLOSE intent) mutating
    submission_intents concurrently - neither must clobber the other."""

    def _buy_a():
        for _ in range(20):
            state.create_submission_intent(
                ticker="AAPL",
                side="BUY",
                qty=5,
                order_type="market",
                strategy_name="s1",
            )

    def _close_b():
        for _ in range(20):
            state.create_submission_intent(
                ticker="MSFT",
                side="CLOSE",
                qty=0,
                order_type="market",
                strategy_name="s2",
            )

    _run_concurrently([_buy_a, _close_b])

    all_intents = state.list_all_intents()
    aapl_intents = [i for i in all_intents if i["ticker"] == "AAPL"]
    msft_intents = [i for i in all_intents if i["ticker"] == "MSFT"]
    assert len(aapl_intents) == 20
    assert len(msft_intents) == 20


def test_telegram_pause_does_not_erase_engine_or_ledger_state(state):
    """Toggling the Telegram pause flag concurrently with engine-state and
    position-ledger updates must not lose either kind of data - they live
    in different top-level keys of the same self.state dict."""
    state.save_engine_state("AAPL", {"in_position": True, "entry_price": 100.0})
    state.record_position_open("AAPL", 10, 100.0)

    def _pause_toggle():
        for i in range(25):
            state.set_telegram_pause(i % 2 == 0)

    def _engine_updates():
        for i in range(25):
            state.save_engine_state(
                "AAPL", {"in_position": True, "entry_price": 100.0 + i}
            )

    _run_concurrently([_pause_toggle, _engine_updates])

    assert state.get_engine_state("AAPL") is not None
    assert state.get_open_positions().get("AAPL") is not None


# ---------------------------------------------------------------------------
# Backup / corruption interplay
# ---------------------------------------------------------------------------


def test_backup_never_replaced_by_a_known_corrupt_primary(state, monkeypatch):
    """save()'s backup refresh only happens after the primary write
    succeeds - if the primary write itself fails, the .bak must retain the
    last genuinely valid content, never be overwritten with something
    corrupt."""
    state.set_position_high("AAPL", 100.0)
    with open(f"{state.state_file}.bak") as f:
        good_backup = json.load(f)

    # Force the primary save to fail before it reaches the backup refresh.
    original_replace = None
    import os as os_module

    call_count = {"n": 0}
    real_replace = os_module.replace

    def _flaky_replace(src, dst):
        call_count["n"] += 1
        if call_count["n"] == 1 and dst == state.state_file:
            raise OSError("simulated disk failure")
        return real_replace(src, dst)

    monkeypatch.setattr(os_module, "replace", _flaky_replace)
    state.set_position_high("AAPL", 999.0)  # save() will fail silently (logged)
    monkeypatch.undo()

    with open(f"{state.state_file}.bak") as f:
        backup_after = json.load(f)
    assert (
        backup_after["position_highs"]["AAPL"] == good_backup["position_highs"]["AAPL"]
    )


def test_valid_backup_restores_correctly(tmp_path):
    path = tmp_path / "state.json"
    state = BotState(state_file=str(path))
    state.set_position_high("AAPL", 42.0)
    state.record_entry("AAPL")

    with open(path, "w") as f:
        f.write("{corrupt")

    recovered = BotState(state_file=str(path))
    assert recovered.get_position_high("AAPL") == 42.0
    assert recovered.is_min_hold_met("AAPL", 0) is True  # entry timestamp survived


# ---------------------------------------------------------------------------
# Clean-first-startup / hosted-execution deliberateness (re-verification -
# already covered in test_state_durability.py; kept here as a smoke check
# specific to the concurrency-touched code paths)
# ---------------------------------------------------------------------------


def test_clean_first_startup_with_persistent_storage_true_is_deliberate(
    tmp_path, monkeypatch
):
    """PERSISTENT_STORAGE=true on a brand-new (never-written) state file is
    a deliberate first boot, not a corruption case - must start cleanly,
    not raise StateCorruptionError (that only fires for an UNREADABLE
    existing file, not a simply-absent one)."""
    monkeypatch.setenv("PERSISTENT_STORAGE", "true")
    path = tmp_path / "state.json"
    state = BotState(state_file=str(path))
    assert state.state["submission_intents"] == {}


def test_schema_migration_preserves_existing_risk_and_strategy_fields(tmp_path):
    """Migrating an older state file must not drop pre-existing
    strategy/risk-relevant fields (position_highs, entry_timestamps,
    open_positions, daily_pnl) while backfilling new ones."""
    path = tmp_path / "state.json"
    legacy = {
        "position_highs": {"AAPL": 155.0},
        "entry_timestamps": {"AAPL": "2026-01-01T09:30:00-05:00"},
        "open_positions": {"AAPL": {"qty": 10, "entry_price": 150.0, "opened_at": "x"}},
        "daily_pnl": 42.5,
        "version": "1.0",
    }
    with open(path, "w") as f:
        json.dump(legacy, f)

    state = BotState(state_file=str(path))
    assert state.state["position_highs"] == {"AAPL": 155.0}
    assert state.state["entry_timestamps"] == {"AAPL": "2026-01-01T09:30:00-05:00"}
    assert state.state["open_positions"]["AAPL"]["qty"] == 10
    assert state.state["daily_pnl"] == 42.5
    # New keys backfilled without disturbing the old ones.
    assert state.state["submission_intents"] == {}
    assert state.state["weekly_eval_keys"] == {}
    assert state.state["telegram_paused"] is False
