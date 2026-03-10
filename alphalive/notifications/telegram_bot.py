"""
Telegram Notifications

Sends trading alerts via Telegram Bot API using httpx.
No external Telegram library needed — direct API calls only.

IMPORTANT: Does NOT use python-telegram-bot (any version).
Calls Telegram Bot API directly via HTTPS POST.
"""

import time
import logging
from typing import Optional, Dict, Any

import httpx

logger = logging.getLogger(__name__)


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
    """

    def __init__(
        self,
        bot_token: Optional[str],
        chat_id: Optional[str],
        enabled: bool = True
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

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """
        Send a message via Telegram Bot API.

        Max 3 retries with exponential backoff (1s, 2s, 4s).
        If all retries fail, log error but DON'T crash.

        GRACEFUL DEGRADATION:
        - If 3 consecutive sends fail:
          * Set self.telegram_offline = True
          * Log CRITICAL: "Telegram offline — trading continues but alerts lost"
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
        if not self.enabled:
            logger.debug("Telegram disabled, skipping message")
            return False

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

        # Try sending with retries
        max_retries = 3
        backoff = 1  # Start with 1 second

        for attempt in range(1, max_retries + 1):
            try:
                payload = {
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode
                }

                response = httpx.post(
                    self.api_url,
                    json=payload,
                    timeout=10.0
                )

                if response.status_code == 200:
                    # Success
                    if self.telegram_offline:
                        logger.info("✅ Telegram connection restored")
                        self.telegram_offline = False

                    self.consecutive_failures = 0
                    logger.debug("Telegram message sent successfully")
                    return True
                else:
                    logger.warning(
                        f"Telegram API error (attempt {attempt}/{max_retries}): "
                        f"{response.status_code} - {response.text[:100]}"
                    )

                    if attempt < max_retries:
                        time.sleep(backoff)
                        backoff *= 2  # Exponential backoff: 1s, 2s, 4s
                    else:
                        # All retries exhausted
                        self.consecutive_failures += 1

            except Exception as e:
                logger.error(
                    f"Telegram send failed (attempt {attempt}/{max_retries}): {e}"
                )

                if attempt < max_retries:
                    time.sleep(backoff)
                    backoff *= 2
                else:
                    # All retries exhausted
                    self.consecutive_failures += 1

        # All retries failed
        if self.consecutive_failures >= 3 and not self.telegram_offline:
            self.telegram_offline = True
            logger.critical(
                "🚨 Telegram offline — trading continues but alerts lost"
            )

        return False

    def send_startup_notification(self, strategy_name: str, ticker: str, config: Dict[str, Any]):
        """
        Send bot startup notification.

        Args:
            strategy_name: Strategy name
            ticker: Trading ticker
            config: Strategy configuration dict
        """
        text = (
            f"🚀 <b>AlphaLive Started</b>\n\n"
            f"<b>Strategy:</b> {strategy_name}\n"
            f"<b>Ticker:</b> {ticker}\n"
            f"<b>Timeframe:</b> {config.get('timeframe', 'N/A')}\n"
            f"<b>Stop Loss:</b> {config.get('stop_loss_pct', 'N/A')}%\n"
            f"<b>Take Profit:</b> {config.get('take_profit_pct', 'N/A')}%\n"
            f"<b>Max Position Size:</b> {config.get('max_position_size_pct', 'N/A')}%\n"
            f"<b>Max Daily Loss:</b> {config.get('max_daily_loss_pct', 'N/A')}%\n\n"
            f"Bot is now monitoring the market 24/7."
        )

        self.send_message(text)

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
        self,
        ticker: str,
        side: str,
        qty: float,
        price: float,
        reason: str
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
        reason: str
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
        pnl = stats.get('pnl', 0.0)
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
