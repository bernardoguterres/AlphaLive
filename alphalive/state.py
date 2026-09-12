"""
State Persistence for AlphaLive

Lightweight state persistence to handle Railway restarts gracefully.
Stores state in JSON file to persist across restarts.

State is stored in STATE_FILE (env var):
- Default: /tmp/alphalive_state.json (ephemeral, lost on Railway redeploy)
- For trailing stops: /mnt/data/alphalive_state.json (Railway Volume, persistent)

CRITICAL: If trailing stops are enabled, PERSISTENT_STORAGE must be "true"
to prevent position_highs from being reset on redeploy (real money risk).
"""

import os
import sys
import json
import logging
import threading
import uuid
from datetime import datetime
from functools import wraps
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from alphalive.utils.env_bool import read_bool_env

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Bump when the on-disk state shape changes in a way `_migrate()` needs to
# handle. Legacy files with no "schema_version" key are treated as version 1.
CURRENT_SCHEMA_VERSION = 2

# Submission-intent lifecycle states (order-intent durability, see
# OrderManager._prepare_intent / _reconcile_intent). "uncertain" is the
# quarantine state: the intent is neither known-successful nor
# known-failed, and no new order for that (ticker, side) decision may be
# placed until it resolves to a terminal or recovered state.
INTENT_PREPARED = "prepared"
INTENT_SUBMITTING = "submitting"
INTENT_SUBMITTED = "submitted"
INTENT_PARTIALLY_FILLED = "partially_filled"
INTENT_FILLED = "filled"
INTENT_REJECTED = "rejected"
INTENT_CANCELLED = "cancelled"
INTENT_EXPIRED = "expired"
INTENT_UNCERTAIN = "uncertain"
INTENT_RECONCILED = "reconciled"

# Non-terminal statuses: an intent in one of these blocks a new order for
# the same (ticker, side) decision until it is reconciled. "reconciled" is
# terminal even though it can follow "uncertain" - it means the quarantine
# was deliberately resolved (by a definite broker outcome or operator
# action), not that the outcome no longer matters.
OPEN_INTENT_STATUSES = {
    INTENT_PREPARED,
    INTENT_SUBMITTING,
    INTENT_SUBMITTED,
    INTENT_PARTIALLY_FILLED,
    INTENT_UNCERTAIN,
}
TERMINAL_INTENT_STATUSES = {
    INTENT_FILLED,
    INTENT_REJECTED,
    INTENT_CANCELLED,
    INTENT_EXPIRED,
    INTENT_RECONCILED,
}


class StateSchemaError(RuntimeError):
    """Raised when a state file's schema_version is newer than this build
    supports. Never silently downgraded/ignored - see BotState._load()."""


class StateCorruptionError(RuntimeError):
    """Raised when the state file is unreadable, no valid backup exists,
    and PERSISTENT_STORAGE=true means durable state was expected. Refusing
    to silently start trading from empty defaults in that situation."""


def _pause_file_path(state_file: str) -> str:
    """Derive the dashboard kill switch's dedicated pause-file path from the
    main state file path. The dashboard is the only writer of this file;
    the bot only ever reads it (see BotState.set_dashboard_pause /
    check_dashboard_paused, audit bug 2.5)."""
    return f"{state_file}.pause.json"


def is_hosted_execution() -> bool:
    """Heuristic: are we running inside a Railway (or similar container
    hosting) deploy, as opposed to an ordinary local run?

    Railway auto-injects RAILWAY_ENVIRONMENT_NAME/RAILWAY_PROJECT_ID on
    every deploy; none of these are set for a local `python run.py`. Used
    only to decide whether an ephemeral STATE_FILE deserves a loud startup
    warning (see config.py) - never to change trading behavior itself.
    """
    return bool(
        os.environ.get("RAILWAY_ENVIRONMENT_NAME")
        or os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("RAILWAY_PROJECT_ID")
    )


def _synchronized(method):
    """Serialize this BotState method's entire body against every other
    @_synchronized method (including save()) via the instance's RLock.

    Concurrency reality (objective: shared-state concurrency, 2026-09-11
    pass 2): a single AlphaLive process runs the main loop on one thread
    and TelegramCommandListener's polling loop on a background daemon
    thread. Both can mutate the SAME BotState instance concurrently - the
    main loop via execute_signal's BUY/SELL submission intents, engine
    state, position ledger, weekly-eval keys; the Telegram thread via
    /pause-/resume (GlobalRiskManager.set_manual_pause ->
    BotState.set_telegram_pause) and /close_all (OrderManager.close_position
    -> CLOSE submission intents). Without a lock, save()'s json.dump()
    walks the entire nested self.state structure while another thread
    could be inserting/removing a key in a nested dict (e.g. a new
    submission_intents entry) at the same instant - a genuine
    "dictionary changed size during iteration" hazard, not a
    hypothetical one, plus plain lost-update races on compound
    read-modify-write fields. RLock (not Lock) because some of these
    methods are simple enough to stay non-reentrant today, but decorating
    liberally is only safe long-term with a reentrant lock.

    This does NOT make cross-process access safe (the dashboard, a
    separate process, only ever touches the state file directly, never
    this in-process lock) - atomic file replacement (save()'s temp-file +
    os.replace) remains the cross-process safety mechanism, unchanged by
    this lock; this lock only serializes concurrent threads within one
    process.
    """

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def default_state_file_path() -> str:
    """OS-appropriate persistent application-data path, used only as the
    fallback when STATE_FILE is unset.

    Previously defaulted to /tmp/alphalive_state.json, which most OSes
    clear on reboot and which Railway never persists across a redeploy.
    Hosted execution should always set STATE_FILE explicitly to a mounted
    Volume path (see is_hosted_execution() / the startup warning this
    enables in config.py) - this function exists for ordinary local/dev
    use where "an established durable repository convention" beats an
    unqualified temp path.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(
            os.path.join("~", "AppData", "Local")
        )
        return os.path.join(base, "AlphaLive", "alphalive_state.json")
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
        return os.path.join(base, "AlphaLive", "alphalive_state.json")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "alphalive", "alphalive_state.json")


class BotState:
    """
    Persistent state for AlphaLive bot.

    Handles:
    - Morning check tracking (prevent duplicate signal checks on restart)
    - EOD summary tracking (prevent duplicate summaries)
    - Position highs for trailing stops
    - Daily P&L tracking
    - Trades today tracking

    State survives:
    - Process restarts (if using Railway Volume)
    - Code deploys (if using Railway Volume)
    - Crashes (state saved after each update)

    State does NOT survive:
    - Railway redeployments (if using /tmp/... ephemeral storage)
    - File deletion
    """

    def __init__(self, state_file: Optional[str] = None):
        """
        Initialize BotState.

        Args:
            state_file: Path to state file. If None, uses STATE_FILE env var.
        """
        # Guards every @_synchronized method below against concurrent
        # mutation from the main loop thread and the Telegram listener's
        # background polling thread within this one process - see
        # _synchronized's docstring for the concrete hazard this closes.
        self._lock = threading.RLock()
        # In-memory-only claims for try_begin_weekly_eval/end_weekly_eval -
        # deliberately not part of self.state (never persisted, never
        # migrated); see try_begin_weekly_eval's docstring.
        self._weekly_eval_in_flight = set()
        self.state_file = state_file or os.environ.get(
            "STATE_FILE", default_state_file_path()
        )
        try:
            os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        except Exception as e:
            logger.debug(f"Could not pre-create state directory: {e}")
        self.state = self._load()
        logger.info(f"BotState initialized from {self.state_file}")

    def _load(self) -> dict:
        """
        Load state from file.

        Returns:
            Dictionary with state data. If the file doesn't exist, returns
            default state. If it's corrupted, attempts recovery from the
            rolling backup (see save()) before falling back to defaults -
            and refuses to fall back silently when PERSISTENT_STORAGE=true
            signals that durable state was expected (see
            _handle_unreadable_state / StateCorruptionError).

        Raises:
            StateSchemaError: the file's schema_version is newer than this
                build understands.
            StateCorruptionError: the file (and its backup) are unreadable
                and PERSISTENT_STORAGE=true.
        """
        try:
            with open(self.state_file, "r") as f:
                raw = f.read()
        except FileNotFoundError:
            logger.info(f"State file not found at {self.state_file}, using defaults")
            return self._default_state()
        except Exception as e:
            logger.error(f"Failed to read state from {self.state_file}: {e}")
            return self._handle_unreadable_state(f"unreadable ({e})")

        try:
            state = json.loads(raw)
            if not isinstance(state, dict):
                raise ValueError("state file does not contain a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            return self._handle_unreadable_state(f"corrupted (invalid JSON): {e}")

        schema_version = state.get("schema_version", 1)
        if schema_version > CURRENT_SCHEMA_VERSION:
            raise StateSchemaError(
                f"State file {self.state_file} has schema_version={schema_version}, "
                f"newer than this build supports (max {CURRENT_SCHEMA_VERSION}). "
                f"Refusing to start with state this build cannot safely interpret - "
                f"upgrade AlphaLive or restore a compatible state file."
            )

        logger.info(f"State loaded from {self.state_file}")
        return self._migrate(state, schema_version)

    def _handle_unreadable_state(self, detail: str) -> dict:
        """Recover from an unreadable/corrupt state file: try the rolling
        backup first, then fall back to defaults - unless PERSISTENT_STORAGE
        signals durable state was expected, in which case fail loud instead
        of silently trading from an empty ledger/engine-state/intent set."""
        backup_path = f"{self.state_file}.bak"
        try:
            with open(backup_path, "r") as f:
                backup = json.load(f)
            if isinstance(backup, dict):
                schema_version = backup.get("schema_version", 1)
                if schema_version <= CURRENT_SCHEMA_VERSION:
                    logger.warning(
                        f"State file {self.state_file} is {detail}. Restored last "
                        f"valid backup from {backup_path}."
                    )
                    return self._migrate(backup, schema_version)
        except Exception:
            pass

        try:
            persistent_storage = read_bool_env("PERSISTENT_STORAGE", default=False)
        except ValueError:
            persistent_storage = False

        if persistent_storage:
            raise StateCorruptionError(
                f"State file {self.state_file} is {detail} and no valid backup "
                f"exists. PERSISTENT_STORAGE=true means durable state (position "
                f"ledger, engine state, submission intents) was expected - refusing "
                f"to silently start trading from empty defaults. Restore a valid "
                f"state file, or unset PERSISTENT_STORAGE to intentionally start "
                f"fresh."
            )

        logger.warning(
            f"State file {self.state_file} is {detail}. No backup available and "
            f"PERSISTENT_STORAGE is not set - starting from defaults."
        )
        return self._default_state()

    def _migrate(self, state: dict, from_version: int) -> dict:
        """Apply forward migrations and backfill any keys missing from an
        older or hand-edited state file. Idempotent - safe to call on an
        already-current state."""
        if from_version < 2:
            logger.info(
                f"Migrating state file from schema_version={from_version} to "
                f"{CURRENT_SCHEMA_VERSION} (adding submission_intents/telegram_paused)"
            )
        defaults = self._default_state()
        for key, value in defaults.items():
            state.setdefault(key, value)
        state["schema_version"] = CURRENT_SCHEMA_VERSION
        return state

    def _default_state(self) -> dict:
        """Return the initial empty state dict."""
        return {
            "last_morning_check_date": None,
            "last_eod_summary_date": None,
            "daily_pnl": 0.0,
            "trades_today": [],
            "position_highs": {},
            "entry_timestamps": {},  # {ticker: ISO timestamp} for minimum hold enforcement
            "open_positions": {},  # {ticker: {qty, entry_price, opened_at}} - persisted ledger
            "engine_state": {},  # {ticker: {in_position, entry_price, peak_price}} - SignalEngine state
            "submission_intents": {},  # {intent_id: {...}} - order submission-intent lifecycle
            "weekly_eval_keys": {},  # {ticker: "YYYY-Www"} - last evaluated ISO week per 1Week strategy
            "telegram_paused": False,  # global Telegram /pause flag, survives restarts
            "last_startup": None,
            "version": "1.0",  # legacy informational field, kept for backward compat
            "schema_version": CURRENT_SCHEMA_VERSION,
            # dashboard_paused deliberately does NOT live in this dict (see
            # audit bug 2.5 / _pause_file_path below) - it has its own
            # single-writer file so the bot's own save() of this dict can
            # never clobber a pause the dashboard just wrote.
        }

    @_synchronized
    def save(self):
        """
        Save state to file.

        Adds timestamp and writes to file atomically (via temp file + rename),
        then best-effort refreshes a rolling `.bak` copy used by
        _handle_unreadable_state() to recover from a corrupted primary file.
        Both writes go through a temp-file + os.replace so a crash mid-write
        never leaves either the primary file or the backup partially written -
        the previous valid contents survive untouched until the replace
        (atomic on POSIX) actually happens.

        Returns:
            True if the primary write succeeded, False otherwise. Most
            callers ignore this (in-memory self.state is already updated
            regardless, which is what protects same-process duplicate
            action even on a failed write - see individual mutators'
            docstrings) - mark_weekly_eval_done() is the one caller that
            treats a False here as serious enough to halt further
            automatic action.
        """
        try:
            # Add timestamp
            self.state["last_saved"] = datetime.now(ET).isoformat()
            self.state["schema_version"] = CURRENT_SCHEMA_VERSION

            # Write to temp file first (atomic write)
            temp_file = f"{self.state_file}.tmp"
            with open(temp_file, "w") as f:
                json.dump(self.state, f, indent=2)

            # Rename to actual file (atomic on POSIX)
            os.replace(temp_file, self.state_file)

            logger.debug(f"State saved to {self.state_file}")
        except Exception as e:
            logger.error(f"Failed to save state to {self.state_file}: {e}")
            return False

        try:
            backup_temp = f"{self.state_file}.bak.tmp"
            with open(backup_temp, "w") as f:
                json.dump(self.state, f, indent=2)
            os.replace(backup_temp, f"{self.state_file}.bak")
        except Exception as e:
            logger.debug(f"Failed to refresh state backup: {e}")

        return True

    @_synchronized
    def already_ran_morning_check(self, today: str) -> bool:
        """
        Check if morning signal check already ran today.

        Args:
            today: Date string (YYYY-MM-DD)

        Returns:
            True if morning check already ran today
        """
        return self.state.get("last_morning_check_date") == today

    @_synchronized
    def mark_morning_check_done(self, today: str):
        """
        Mark morning check as done for today.

        Args:
            today: Date string (YYYY-MM-DD)
        """
        self.state["last_morning_check_date"] = today
        self.save()
        logger.info(f"Morning check marked done for {today}")

    @_synchronized
    def already_sent_eod(self, today: str) -> bool:
        """
        Check if EOD summary already sent today.

        Args:
            today: Date string (YYYY-MM-DD)

        Returns:
            True if EOD summary already sent today
        """
        return self.state.get("last_eod_summary_date") == today

    @_synchronized
    def mark_eod_sent(self, today: str):
        """
        Mark EOD summary as sent for today.

        Args:
            today: Date string (YYYY-MM-DD)
        """
        self.state["last_eod_summary_date"] = today
        self.save()
        logger.info(f"EOD summary marked sent for {today}")

    @_synchronized
    def get_position_high(self, ticker: str) -> Optional[float]:
        """
        Get highest price seen for position (for trailing stops).

        Args:
            ticker: Stock ticker

        Returns:
            Highest price seen, or None if not tracking
        """
        return self.state["position_highs"].get(ticker)

    @_synchronized
    def set_position_high(self, ticker: str, price: float):
        """
        Set/update highest price seen for position.

        Args:
            ticker: Stock ticker
            price: New high price
        """
        current_high = self.state["position_highs"].get(ticker)

        if current_high is None or price > current_high:
            self.state["position_highs"][ticker] = price
            self.save()
            logger.debug(f"Position high for {ticker} updated to ${price:.2f}")

    @_synchronized
    def clear_position_high(self, ticker: str):
        """
        Clear position high tracking (position closed).

        Args:
            ticker: Stock ticker
        """
        if ticker in self.state["position_highs"]:
            del self.state["position_highs"][ticker]
            self.save()
            logger.debug(f"Position high cleared for {ticker}")

    @_synchronized
    def record_entry(self, ticker: str):
        """Record the timestamp when a position was entered (for minimum hold enforcement)."""
        if "entry_timestamps" not in self.state:
            self.state["entry_timestamps"] = {}
        self.state["entry_timestamps"][ticker] = datetime.now(ET).isoformat()
        self.save()
        logger.info(f"Entry timestamp recorded for {ticker}")

    @_synchronized
    def clear_entry_timestamp(self, ticker: str):
        """Clear entry timestamp when position is closed."""
        entry_timestamps = self.state.get("entry_timestamps", {})
        if ticker in entry_timestamps:
            del entry_timestamps[ticker]
            self.state["entry_timestamps"] = entry_timestamps
            self.save()

    @_synchronized
    def is_min_hold_met(self, ticker: str, min_hold_weeks: int) -> bool:
        """Return True if the position has been held for at least min_hold_weeks.

        Returns True if no entry timestamp is found (fail-safe: allow exit).
        """
        entry_timestamps = self.state.get("entry_timestamps", {})
        ts_str = entry_timestamps.get(ticker)
        if ts_str is None:
            return True  # No record - allow exit

        try:
            entry_dt = datetime.fromisoformat(ts_str)
            now = datetime.now(ET)
            weeks_held = (now - entry_dt).days / 7
            met = weeks_held >= min_hold_weeks
            logger.debug(
                f"{ticker}: {weeks_held:.1f} weeks held, "
                f"min_hold={min_hold_weeks} weeks → {'met' if met else 'NOT met'}"
            )
            return met
        except Exception as exc:
            logger.warning(f"Could not parse entry timestamp for {ticker}: {exc}")
            return True  # Fail-safe: allow exit

    @_synchronized
    def record_position_open(self, ticker: str, qty: float, entry_price: float):
        """Record an opened position in the persisted ledger.

        The ledger is the source of truth for position reconciliation -
        unlike OrderManager's order history, it survives restarts and is
        not reset daily, so multi-day holds don't trigger false drift.
        """
        ledger = self.state.setdefault("open_positions", {})
        ledger[ticker] = {
            "qty": qty,
            "entry_price": entry_price,
            "opened_at": datetime.now(ET).isoformat(),
        }
        self.save()
        logger.info(f"Position ledger: opened {ticker} ({qty} @ ${entry_price:.2f})")

    @_synchronized
    def record_position_close(self, ticker: str):
        """Remove a position from the persisted ledger (position fully closed)."""
        ledger = self.state.setdefault("open_positions", {})
        if ticker in ledger:
            del ledger[ticker]
            self.save()
            logger.info(f"Position ledger: closed {ticker}")

    @_synchronized
    def get_open_positions(self) -> dict:
        """Return the persisted open-position ledger: {ticker: {qty, entry_price, opened_at}}."""
        return dict(self.state.get("open_positions", {}))

    @_synchronized
    def set_morning_equity(self, today: str, equity: float):
        """Persist the equity captured at market open for the given day.

        Without this, a restart after 4 PM leaves morning_equity at 0.0 and
        the EOD summary reports the entire account equity as the day's P&L.
        """
        self.state["morning_equity"] = {"date": today, "value": equity}
        self.save()

    @_synchronized
    def get_morning_equity(self, today: str) -> Optional[float]:
        """Return the persisted morning equity for the given day, or None."""
        entry = self.state.get("morning_equity")
        if entry and entry.get("date") == today:
            return entry.get("value")
        return None

    @_synchronized
    def get_last_screener_month(self) -> Optional[str]:
        """Return the YYYY-MM month the monthly screener last ran, or None."""
        return self.state.get("last_screener_month")

    @_synchronized
    def set_last_screener_month(self, month: str):
        """Record that the monthly screener ran for the given YYYY-MM month."""
        self.state["last_screener_month"] = month
        self.save()

    @_synchronized
    def save_engine_state(self, ticker: str, engine_state: dict):
        """Persist a SignalEngine's stateful fields (in_position/entry/peak).

        Stateful strategies must survive Railway restarts mid-position -
        otherwise they think they're flat, can double-buy, and never emit
        their exit. Saved after every signal check.
        """
        states = self.state.setdefault("engine_state", {})
        states[ticker] = engine_state
        self.save()

    @_synchronized
    def get_engine_state(self, ticker: str) -> Optional[dict]:
        """Return the persisted SignalEngine state for a ticker, or None."""
        return self.state.get("engine_state", {}).get(ticker)

    @_synchronized
    def clear_engine_state(self, ticker: str):
        """Drop persisted engine state (position closed outside the engine)."""
        states = self.state.get("engine_state", {})
        if ticker in states:
            del states[ticker]
            self.save()

    # ------------------------------------------------------------------
    # Weekly scheduling (objective: 1Week strategies evaluate once per
    # ISO calendar week, 2026-09-11 hardening pass). The in-memory
    # `morning_checks_done` set main.py already uses for 1Day is lost on
    # restart, which is fine for a once-per-day gate (a restart later the
    # same day is a narrow, documented, accepted window - see README). A
    # once-per-CALENDAR-WEEK gate needs a real restart guarantee: without
    # this, a restart on, say, Wednesday after Monday's weekly evaluation
    # would re-evaluate against a fresh in-memory set. `mark_weekly_eval_done`
    # mutates self.state (and therefore what get_weekly_eval_key sees) BEFORE
    # calling save() - so even if the disk write itself fails, a caller in
    # the SAME process still observes the update and won't re-evaluate; only
    # a failed write immediately followed by a restart can lose it, which is
    # the same irreducible boundary every other persisted flag here has.
    # ------------------------------------------------------------------

    @_synchronized
    def get_weekly_eval_key(self, ticker: str) -> Optional[str]:
        """Return the ISO calendar-week key (e.g. "2026-W03") this ticker's
        weekly strategy last successfully evaluated for, or None."""
        return self.state.get("weekly_eval_keys", {}).get(ticker)

    @_synchronized
    def mark_weekly_eval_done(self, ticker: str, week_key: str) -> bool:
        """Record that this ticker's weekly strategy has evaluated for the
        given ISO calendar-week key - never evaluate the same key twice.

        Returns whether the write to disk actually succeeded (save()'s own
        result) - unlike every other BotState mutator, the caller
        (main.py's _mark_periodic_check_done) treats a failed write here as
        serious enough to halt further automatic evaluation rather than
        silently continue on in-memory-only protection: a weekly decision
        that silently fails to persist and is then lost to a restart is a
        duplicate-evaluation risk this gate exists specifically to prevent.
        """
        keys = self.state.setdefault("weekly_eval_keys", {})
        keys[ticker] = week_key
        return self.save()

    @_synchronized
    def try_begin_weekly_eval(self, ticker: str, week_key: str) -> bool:
        """Atomically claim the right to evaluate `ticker` for `week_key`
        this pass. Returns False (do not evaluate) if the week is already
        marked done, OR another in-process evaluation for this exact
        (ticker, week_key) is currently in flight - closing the race a
        plain "get_weekly_eval_key, evaluate, then mark_weekly_eval_done"
        sequence would otherwise leave open between concurrent scheduler
        ticks (this codebase's main loop is single-threaded today, but the
        primitive is correct regardless of caller concurrency). Returns
        True and claims it otherwise.

        The claim is in-memory only (not persisted) - deliberately so:
        completion (mark_weekly_eval_done) is what earns durability, and a
        failed/aborted evaluation must not durably block a later retry.
        Callers MUST pair a successful claim with end_weekly_eval() in a
        finally-equivalent, on every exit path (success or failure).
        """
        if self.state.get("weekly_eval_keys", {}).get(ticker) == week_key:
            return False
        key = (ticker, week_key)
        if key in self._weekly_eval_in_flight:
            return False
        self._weekly_eval_in_flight.add(key)
        return True

    @_synchronized
    def end_weekly_eval(self, ticker: str, week_key: str) -> None:
        """Release a claim made by try_begin_weekly_eval(), regardless of
        whether the evaluation completed or failed."""
        self._weekly_eval_in_flight.discard((ticker, week_key))

    def set_dashboard_pause(self, paused: bool):
        """Set the dashboard kill switch.

        Writes ONLY to the dedicated pause file (see _pause_file_path) -
        never touches self.state or the main state file. Audit bug 2.5: the
        pause flag used to live in self.state["dashboard_paused"], so the
        bot's own next unrelated save() (e.g. a trailing-stop position_high
        update) would write its own stale in-memory copy of the whole state
        dict back to disk, silently overwriting a pause the dashboard had
        just written - the bot process never refreshed dashboard_paused
        from disk except via the separate check_dashboard_paused() call.
        Giving the pause flag its own single-writer file (the dashboard is
        the only writer; the bot only ever reads it) eliminates that
        shared-mutable-state race entirely rather than papering over it.
        """
        pause_file = _pause_file_path(self.state_file)
        try:
            payload = {
                "dashboard_paused": bool(paused),
                "updated_at": datetime.now(ET).isoformat(),
            }
            temp_file = f"{pause_file}.tmp"
            with open(temp_file, "w") as f:
                json.dump(payload, f, indent=2)
            os.replace(temp_file, pause_file)
            logger.info(f"Dashboard kill switch {'activated' if paused else 'cleared'}")
        except Exception as e:
            logger.error(f"Failed to write dashboard pause file {pause_file}: {e}")

    def is_dashboard_paused(self) -> bool:
        """Return the dashboard kill switch state. Always a fresh read from
        the dedicated pause file - there is no in-memory cache to go stale,
        which is the whole point of this flag having its own file (see
        set_dashboard_pause). Alias of check_dashboard_paused()."""
        return self.check_dashboard_paused()

    def check_dashboard_paused(self) -> bool:
        """Read the dashboard kill switch fresh from its dedicated pause
        file. Used by the main loop for its ~30s poll."""
        pause_file = _pause_file_path(self.state_file)
        try:
            with open(pause_file, "r") as f:
                return bool(json.load(f).get("dashboard_paused", False))
        except FileNotFoundError:
            # No pause file yet = dashboard has never paused - the correct,
            # safe interpretation of "never configured" for this flag.
            return False
        except Exception as e:
            # Any other read failure (corrupted JSON, permission error) is
            # NOT the same as "never configured" - we genuinely don't know
            # the real state, so fail toward the safe direction (paused)
            # rather than silently letting trading continue.
            logger.error(
                f"Failed to read dashboard pause file {pause_file}: {e}. Failing safe (paused)."
            )
            return True

    @_synchronized
    def mark_startup(self):
        """Mark bot startup time."""
        self.state["last_startup"] = datetime.now(ET).isoformat()
        self.save()

    @_synchronized
    def add_daily_pnl(self, pnl: float):
        """
        Add a realized trade P&L to the running daily total and persist it.

        Args:
            pnl: Realized profit/loss in dollars (negative for losses)
        """
        self.state["daily_pnl"] = self.state.get("daily_pnl", 0.0) + pnl
        self.save()

    @_synchronized
    def reset_daily(self, today: str):
        """
        Reset daily counters (call at start of new trading day).

        Args:
            today: Date string (YYYY-MM-DD)
        """
        # Only reset if it's actually a new day
        if self.state.get("last_morning_check_date") != today:
            self.state["daily_pnl"] = 0.0
            self.state["trades_today"] = []
            self.state["last_morning_check_date"] = None
            self.state["last_eod_summary_date"] = None
            self.save()
            logger.info(f"Daily counters reset for {today}")

    # ------------------------------------------------------------------
    # Global Telegram pause (objective 3): a single persisted flag shared
    # by every strategy's RiskManager via GlobalRiskManager, instead of an
    # in-memory per-RiskManager attribute that only the first configured
    # strategy's Telegram listener could reach. Persisted (unlike the old
    # in-memory-only flag) so an operator-issued halt survives a restart -
    # the same durability expectation as the dashboard/env kill switches.
    # ------------------------------------------------------------------

    @_synchronized
    def set_telegram_pause(self, paused: bool, reason: Optional[str] = None):
        """Persist the global Telegram /pause flag."""
        self.state["telegram_paused"] = bool(paused)
        self.state["telegram_pause_reason"] = reason if paused else None
        self.save()

    @_synchronized
    def get_telegram_pause(self) -> bool:
        """Return the persisted global Telegram pause flag."""
        return bool(self.state.get("telegram_paused", False))

    @_synchronized
    def get_telegram_pause_reason(self) -> Optional[str]:
        return self.state.get("telegram_pause_reason")

    # ------------------------------------------------------------------
    # Submission-intent lifecycle (objective 1): a durable record of each
    # logical order decision, created and persisted BEFORE any broker call,
    # so a crash/restart can recover the same client_order_id instead of
    # generating a fresh one and risking a duplicate order. See
    # OrderManager._get_or_create_intent / _reconcile_intent for the
    # decision logic that consumes this state; this class only owns
    # storage + simple queries.
    # ------------------------------------------------------------------

    @_synchronized
    def create_submission_intent(
        self,
        ticker: str,
        side: str,
        qty: float,
        order_type: str,
        strategy_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create and durably persist a new submission intent BEFORE any
        broker call is made. Returns the intent dict (includes intent_id
        and a stable client_order_id).

        The client_order_id is derived from a fresh uuid4, not just
        ticker+side+timestamp - two distinct legitimate decisions to trade
        the same ticker/side (e.g. two signals on different days) must not
        collide, and a collision would either get spuriously rejected by
        Alpaca's own client_order_id uniqueness constraint or (worse) get
        misidentified as "the same logical order" during reconciliation.
        """
        intent_id = uuid.uuid4().hex
        # Alpaca's client_order_id has a generous length limit; keeping the
        # ticker/side in it is purely for human log readability, not identity -
        # identity comes entirely from the uuid suffix.
        client_order_id = f"{ticker}_{side.lower()}_{intent_id}"[:128]
        now = datetime.now(ET).isoformat()
        intent = {
            "intent_id": intent_id,
            "client_order_id": client_order_id,
            "ticker": ticker,
            "side": side.upper(),
            "qty": qty,
            "order_type": order_type,
            "strategy_name": strategy_name,
            "status": INTENT_PREPARED,
            "broker_order_id": None,
            "created_at": now,
            "updated_at": now,
            "last_error": None,
        }
        intents = self.state.setdefault("submission_intents", {})
        intents[intent_id] = intent
        self.save()
        logger.info(
            f"Submission intent created: {intent_id} ({ticker} {side} qty={qty}, "
            f"client_order_id={client_order_id})"
        )
        return dict(intent)

    @_synchronized
    def update_submission_intent(self, intent_id: str, **fields) -> None:
        """Merge-update an existing intent's fields and persist."""
        intents = self.state.setdefault("submission_intents", {})
        intent = intents.get(intent_id)
        if intent is None:
            logger.error(f"update_submission_intent: unknown intent_id {intent_id}")
            return
        intent.update(fields)
        intent["updated_at"] = datetime.now(ET).isoformat()
        self.save()

    @_synchronized
    def get_submission_intent(self, intent_id: str) -> Optional[Dict[str, Any]]:
        intent = self.state.get("submission_intents", {}).get(intent_id)
        return dict(intent) if intent is not None else None

    @_synchronized
    def get_open_intent(self, ticker: str, side: str) -> Optional[Dict[str, Any]]:
        """Return the most recent non-terminal intent for (ticker, side), if
        any. OrderManager consults this BEFORE creating a new intent so a
        retry/restart reuses the same client_order_id instead of risking a
        second broker order for the same logical decision."""
        side = side.upper()
        candidates = [
            intent
            for intent in self.state.get("submission_intents", {}).values()
            if intent["ticker"] == ticker
            and intent["side"] == side
            and intent["status"] in OPEN_INTENT_STATUSES
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda i: i["created_at"])
        return dict(candidates[-1])

    @_synchronized
    def list_open_intents(self) -> List[Dict[str, Any]]:
        """Return every non-terminal (quarantined or in-flight) intent,
        across all tickers - used by startup reconciliation."""
        return [
            dict(intent)
            for intent in self.state.get("submission_intents", {}).values()
            if intent["status"] in OPEN_INTENT_STATUSES
        ]

    @_synchronized
    def list_all_intents(self) -> List[Dict[str, Any]]:
        return [dict(i) for i in self.state.get("submission_intents", {}).values()]


def check_trailing_stop_requirements(strategy_config, notifier=None):
    """
    Check if trailing stops are properly configured.

    If trailing_stop_enabled=True but PERSISTENT_STORAGE != "true",
    refuse to start (real money risk from position_highs reset on redeploy).

    Args:
        strategy_config: Strategy configuration
        notifier: Telegram notifier (optional)

    Raises:
        SystemExit: If trailing stops enabled without persistent storage
    """
    if strategy_config.risk.trailing_stop_enabled:
        try:
            persistent_storage = read_bool_env("PERSISTENT_STORAGE", default=False)
        except ValueError:
            # Malformed value is exactly as unsafe as "not set" here - this
            # guard's whole purpose is to fail closed unless persistent
            # storage is unambiguously enabled.
            persistent_storage = False

        if not persistent_storage:
            error_msg = (
                "STARTUP ABORTED: trailing_stop_enabled=true requires persistent "
                "storage, but PERSISTENT_STORAGE is not set to true. A Railway "
                "redeploy mid-day will reset position_highs and miscalculate "
                "trailing stops, which is a real money risk. Either: "
                "(A) Set trailing_stop_enabled=false in your strategy config, or "
                "(B) Mount a Railway Volume, set STATE_FILE=/mnt/data/alphalive_state.json, "
                "and set PERSISTENT_STORAGE=true"
            )

            logger.critical(error_msg)

            # Send Telegram alert
            if notifier:
                notifier.send_error_alert(
                    "⛔ AlphaLive refused to start: trailing stops require "
                    "persistent storage. See Railway logs for fix instructions."
                )

            import sys

            sys.exit(1)

    logger.info("Trailing stop configuration check passed")
