"""
Telegram Notifications

Sends trading alerts via Telegram Bot API using httpx.
No external Telegram library needed - direct API calls only.

IMPORTANT: Does NOT use python-telegram-bot (any version).
Calls Telegram Bot API directly via HTTPS POST.
"""

import queue
import threading
import time
import logging
from typing import Optional, Dict, Any

import httpx

from alphalive.utils.retry import RetryDecision, RetryOutcome, retry_with_backoff

logger = logging.getLogger(__name__)


class _TelegramAPIError(Exception):
    """Internal: raised by _post_once() when Telegram returns a non-200
    status, so the shared retry_with_backoff() loop can drive retries.
    Never escapes _send_now() - callers only see the True/False return."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"{status_code} - {body}")


class TelegramNotifier:
    """
    Send notifications via Telegram Bot API.

    Uses httpx to call the Telegram sendMessage endpoint directly.
    No python-telegram-bot library required.

    Features:
    - Retry logic with exponential backoff (1s, 2s, 4s)
    - Graceful degradation if Telegram offline
    - Background retry every 10 minutes
    - Never crashes trading loop
    - send_message() never blocks the calling thread (fixed 2026-07-14,
      audit bug 2.8) - it enqueues onto a dedicated background worker
      thread, which does the actual HTTP call/retries/backoff. A slow or
      down Telegram can no longer delay signal checks or order execution.
    """

    def __init__(
        self, bot_token: Optional[str], chat_id: Optional[str], enabled: bool = True
    ):
        """
        Initialize Telegram notifier.

        Args:
            bot_token: Telegram bot token (from @BotFather)
            chat_id: Chat ID to send messages to
            enabled: Enable notifications (default True)
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled and bot_token is not None and chat_id is not None

        # Graceful degradation tracking
        self.consecutive_failures = 0
        self.telegram_offline = False
        self.last_retry_attempt = 0.0  # Timestamp of last background retry

        if not self.enabled:
            logger.warning(
                "Telegram notifications disabled (missing bot_token or chat_id)"
            )
        else:
            logger.info(f"Telegram notifications enabled | Chat ID: {chat_id}")

        # Telegram API URL
        if bot_token:
            self.api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        else:
            self.api_url = None

        # Audit bug 2.8: send_message() used to make the actual blocking
        # HTTP call (up to 3 retries x 10s timeout + 1s/2s/4s backoff =
        # ~37s worst case) directly on the caller's thread - and it was
        # called inline from the main trading loop, so a slow/down
        # Telegram measurably blocked trading (measured: up to ~33s with a
        # hanging fake server). The real send now happens on a dedicated
        # background worker thread; send_message() just enqueues the
        # message and returns immediately. All existing retry/backoff/
        # graceful-degradation behavior is unchanged - it just runs off the
        # trading loop's thread now (see _send_now / _worker_loop below).
        self._queue: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=100)
        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="telegram-notifier", daemon=True
        )
        self._worker_thread.start()

    def _worker_loop(self):
        """Background thread: pulls queued messages and sends them via
        _send_now(), one at a time, off the caller's thread."""
        while True:
            text, parse_mode = self._queue.get()
            try:
                self._send_now(text, parse_mode)
            except Exception:
                logger.exception("Unexpected error in Telegram background worker")
            finally:
                self._queue.task_done()

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """
        Queue a message to be sent via the Telegram Bot API in the
        background - never blocks the calling thread on the actual HTTP
        call, retries, or backoff (see _send_now for that logic, which now
        runs on a dedicated worker thread).

        Args:
            text: Message text
            parse_mode: "HTML" or "Markdown" (default HTML)

        Returns:
            True if the message was queued (NOT a delivery guarantee -
            actual send/retry/failure happens asynchronously; no caller in
            this codebase inspects this return value for delivery status).
            False if disabled, or the queue is full (Telegram has been down
            long enough that a backlog of retries hasn't drained - the
            message is dropped rather than blocking the caller further).
        """
        if not self.enabled:
            logger.debug("Telegram disabled, skipping message")
            return False

        try:
            self._queue.put_nowait((text, parse_mode))
            return True
        except queue.Full:
            logger.error(
                "Telegram message queue full (Telegram likely down for a "
                "while) - dropping message rather than blocking the caller"
            )
            return False

    def _send_now(self, text: str, parse_mode: str = "HTML") -> bool:
        """
        Actually send a message via the Telegram Bot API (blocking - only
        ever called from the background worker thread, never directly by
        trading-loop code).

        Max 3 retries with exponential backoff (1s, 2s, 4s).
        If all retries fail, log error but DON'T crash.

        GRACEFUL DEGRADATION:
        - If 3 consecutive sends fail:
          * Set self.telegram_offline = True
          * Log CRITICAL: "Telegram offline - trading continues but alerts lost"
          * Continue returning False (don't crash trading loop)
        - Background retry: every 10 minutes, attempt one send
        - If background retry succeeds:
          * Set self.telegram_offline = False
          * Set self.consecutive_failures = 0
          * Log INFO: "Telegram connection restored"

        Args:
            text: Message text
            parse_mode: "HTML" or "Markdown" (default HTML)

        Returns:
            True if sent successfully, False otherwise
        """
        # Check if we should attempt a background retry
        if self.telegram_offline:
            current_time = time.time()
            if current_time - self.last_retry_attempt < 600:  # 10 minutes
                # Too soon for background retry
                return False
            else:
                # Attempt background retry
                logger.info("Attempting background Telegram retry (10min elapsed)")
                self.last_retry_attempt = current_time

        def _post_once() -> httpx.Response:
            payload = {"chat_id": self.chat_id, "text": text, "parse_mode": parse_mode}
            response = httpx.post(self.api_url, json=payload, timeout=10.0)
            if response.status_code != 200:
                # Not a raise-for-status HTTP client error - raise our own
                # exception so the shared retry loop treats a non-200
                # response as retryable, same as a transport exception.
                raise _TelegramAPIError(response.status_code, response.text[:100])
            return response

        def _classify(e: Exception) -> RetryOutcome:
            if isinstance(e, _TelegramAPIError):
                return RetryOutcome(
                    RetryDecision.RETRY,
                    log_message=f"Telegram API error: {e.status_code} - {e.body}",
                )
            return RetryOutcome(
                RetryDecision.RETRY, log_message=f"Telegram send failed: {e}"
            )

        try:
            # Max 3 retries with exponential backoff (1s, 2s, 4s).
            retry_with_backoff(
                _post_once, classify=_classify, max_retries=3, base_delay=1.0
            )
        except Exception:
            # All retries failed
            self.consecutive_failures += 1
            if self.consecutive_failures >= 3 and not self.telegram_offline:
                self.telegram_offline = True
                logger.critical(
                    "🚨 Telegram offline - trading continues but alerts lost"
                )
            return False

        # Success
        if self.telegram_offline:
            logger.info("✅ Telegram connection restored")
            self.telegram_offline = False

        self.consecutive_failures = 0
        logger.debug("Telegram message sent successfully")
        return True

    def send_shutdown_notification(self, daily_stats: Dict[str, Any]):
        """
        Send bot shutdown notification with daily stats.

        Args:
            daily_stats: Daily trading statistics
        """
        text = (
            f"🛑 <b>AlphaLive Stopped</b>\n\n"
            f"<b>Trades Today:</b> {daily_stats.get('trades', 0)}\n"
            f"<b>P&L:</b> ${daily_stats.get('pnl', 0.0):.2f}\n"
            f"<b>Win Rate:</b> {daily_stats.get('win_rate', 0):.1f}%\n\n"
            f"Bot has been shut down."
        )

        self.send_message(text)

    def send_trade_notification(
        self, ticker: str, side: str, qty: float, price: float, reason: str
    ):
        """
        Send trade execution notification.

        Args:
            ticker: Ticker symbol
            side: "BUY" or "SELL"
            qty: Quantity
            price: Execution price
            reason: Signal reason
        """
        emoji = "🟢" if side.upper() == "BUY" else "🔴"

        text = (
            f"{emoji} <b>{side.upper()} Signal Executed</b>\n\n"
            f"<b>Ticker:</b> {ticker}\n"
            f"<b>Qty:</b> {qty}\n"
            f"<b>Price:</b> ${price:.2f}\n"
            f"<b>Total:</b> ${qty * price:.2f}\n"
            f"<b>Reason:</b> {reason}"
        )

        self.send_message(text)

    def send_position_closed_notification(
        self,
        ticker: str,
        qty: float,
        entry_price: float,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        reason: str,
    ):
        """
        Send position closed notification.

        Args:
            ticker: Ticker symbol
            qty: Quantity
            entry_price: Entry price
            exit_price: Exit price
            pnl: Profit/Loss in dollars
            pnl_pct: Profit/Loss percentage
            reason: Closure reason (e.g., "Stop loss hit")
        """
        emoji = "💰" if pnl > 0 else "⚠️"

        text = (
            f"{emoji} <b>Position Closed</b>\n\n"
            f"<b>Ticker:</b> {ticker}\n"
            f"<b>Qty:</b> {qty}\n"
            f"<b>Entry:</b> ${entry_price:.2f}\n"
            f"<b>Exit:</b> ${exit_price:.2f}\n"
            f"<b>P&L:</b> ${pnl:.2f} ({pnl_pct:+.2f}%)\n"
            f"<b>Reason:</b> {reason}"
        )

        self.send_message(text)

    def send_error_alert(self, error_msg: str):
        """
        Send error alert.

        Args:
            error_msg: Error message
        """
        text = (
            f"⚠️ <b>AlphaLive Error</b>\n\n"
            f"<code>{error_msg}</code>\n\n"
            f"Check logs for details."
        )

        self.send_message(text)

    def send_alert(self, message: str):
        """
        Send generic alert message.

        Args:
            message: Alert message
        """
        text = f"🔔 <b>Alert</b>\n\n{message}"
        self.send_message(text)

    def send_daily_summary(self, stats: Dict[str, Any]):
        """
        Send daily trading summary.

        Args:
            stats: Daily statistics dict with keys:
                   trades, pnl, win_rate, start_equity, end_equity
        """
        pnl = stats.get("pnl", 0.0)
        emoji = "📈" if pnl > 0 else "📉" if pnl < 0 else "➖"

        text = (
            f"{emoji} <b>Daily Summary</b>\n\n"
            f"<b>Trades:</b> {stats.get('trades', 0)}\n"
            f"<b>P&L:</b> ${pnl:.2f}\n"
            f"<b>Win Rate:</b> {stats.get('win_rate', 0):.1f}%\n"
            f"<b>Start Equity:</b> ${stats.get('start_equity', 0):.2f}\n"
            f"<b>End Equity:</b> ${stats.get('end_equity', 0):.2f}"
        )

        self.send_message(text)

    def is_offline(self) -> bool:
        """
        Check if Telegram is currently offline.

        Returns:
            True if Telegram is offline (3+ consecutive failures)
        """
        return self.telegram_offline
