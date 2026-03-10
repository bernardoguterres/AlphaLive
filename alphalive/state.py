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
import json
import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


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
        self.state_file = state_file or os.environ.get(
            "STATE_FILE", "/tmp/alphalive_state.json"
        )
        self.state = self._load()
        logger.info(f"BotState initialized from {self.state_file}")

    def _load(self) -> dict:
        """
        Load state from file.

        Returns:
            Dictionary with state data. If file doesn't exist or is corrupted,
            returns default state.
        """
        try:
            with open(self.state_file, 'r') as f:
                state = json.load(f)
                logger.info(f"State loaded from {self.state_file}")
                return state
        except FileNotFoundError:
            logger.info(f"State file not found at {self.state_file}, using defaults")
            return self._default_state()
        except json.JSONDecodeError as e:
            logger.warning(
                f"State file {self.state_file} is corrupted (invalid JSON): {e}. "
                f"Using defaults."
            )
            return self._default_state()
        except Exception as e:
            logger.error(f"Failed to load state from {self.state_file}: {e}")
            return self._default_state()

    def _default_state(self) -> dict:
        """
        Return default state.

        Returns:
            Dictionary with default state values
        """
        return {
            "last_morning_check_date": None,
            "last_eod_summary_date": None,
            "daily_pnl": 0.0,
            "trades_today": [],
            "position_highs": {},
            "last_startup": None,
            "version": "1.0"
        }

    def save(self):
        """
        Save state to file.

        Adds timestamp and writes to file atomically (via temp file + rename).
        """
        try:
            # Add timestamp
            self.state["last_saved"] = datetime.now(ET).isoformat()

            # Write to temp file first (atomic write)
            temp_file = f"{self.state_file}.tmp"
            with open(temp_file, 'w') as f:
                json.dump(self.state, f, indent=2)

            # Rename to actual file (atomic on POSIX)
            os.replace(temp_file, self.state_file)

            logger.debug(f"State saved to {self.state_file}")
        except Exception as e:
            logger.error(f"Failed to save state to {self.state_file}: {e}")

    def already_ran_morning_check(self, today: str) -> bool:
        """
        Check if morning signal check already ran today.

        Args:
            today: Date string (YYYY-MM-DD)

        Returns:
            True if morning check already ran today
        """
        return self.state.get("last_morning_check_date") == today

    def mark_morning_check_done(self, today: str):
        """
        Mark morning check as done for today.

        Args:
            today: Date string (YYYY-MM-DD)
        """
        self.state["last_morning_check_date"] = today
        self.save()
        logger.info(f"Morning check marked done for {today}")

    def already_sent_eod(self, today: str) -> bool:
        """
        Check if EOD summary already sent today.

        Args:
            today: Date string (YYYY-MM-DD)

        Returns:
            True if EOD summary already sent today
        """
        return self.state.get("last_eod_summary_date") == today

    def mark_eod_sent(self, today: str):
        """
        Mark EOD summary as sent for today.

        Args:
            today: Date string (YYYY-MM-DD)
        """
        self.state["last_eod_summary_date"] = today
        self.save()
        logger.info(f"EOD summary marked sent for {today}")

    def get_position_high(self, ticker: str) -> Optional[float]:
        """
        Get highest price seen for position (for trailing stops).

        Args:
            ticker: Stock ticker

        Returns:
            Highest price seen, or None if not tracking
        """
        return self.state["position_highs"].get(ticker)

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

    def mark_startup(self):
        """Mark bot startup time."""
        self.state["last_startup"] = datetime.now(ET).isoformat()
        self.save()

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


def reconstruct_daily_pnl(broker, risk_manager) -> float:
    """
    Reconstruct daily P&L from broker's today's fills.

    This is called on startup to restore daily_pnl after a restart.
    If it fails (API error, no fills yet), defaults to 0.0 with WARNING.

    Args:
        broker: Broker instance
        risk_manager: RiskManager instance

    Returns:
        Reconstructed daily P&L
    """
    try:
        # Get today's fills from broker
        fills = broker.get_todays_fills()

        # Sum P&L from all fills
        daily_pnl = sum(fill.get("pnl", 0.0) for fill in fills)

        # Update risk manager
        risk_manager.daily_pnl = daily_pnl

        logger.info(
            f"Daily P&L reconstructed from broker: ${daily_pnl:.2f} "
            f"({len(fills)} fills today)"
        )

        return daily_pnl

    except Exception as e:
        logger.warning(
            f"Daily P&L reconstruction failed: {e}. "
            f"Defaulting to 0.0. Circuit breaker reset. "
            f"Monitor manually today."
        )

        # Default to 0.0 (acceptable risk)
        risk_manager.daily_pnl = 0.0
        return 0.0


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
        persistent_storage = os.environ.get("PERSISTENT_STORAGE", "false").lower()

        if persistent_storage != "true":
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
