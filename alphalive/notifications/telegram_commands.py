"""
Telegram Command Listener

Polls Telegram for incoming commands on a background thread.
Uses the getUpdates API endpoint directly via httpx (NOT webhooks).

Security: Only responds to messages from the configured chat_id.
"""

import httpx
import threading
import time
import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo
from collections import defaultdict

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


class TelegramCommandListener:
    """
    Polls Telegram for incoming commands on a background thread.
    Uses the getUpdates API endpoint directly via httpx.

    Commands:
    - /status: Current bot state
    - /pause: Pause trading (no new entries)
    - /resume: Resume trading
    - /close_all: Close all positions (with confirmation)
    - /config: Strategy configuration
    - /performance: Performance stats
    - /help: List all commands
    """

    def __init__(self, bot_token: str, chat_id: str,
                 order_manager, risk_manager, broker, notifier, config):
        """
        Initialize command listener.

        Args:
            bot_token: Telegram bot token
            chat_id: Only respond to this chat (security)
            order_manager: OrderManager instance
            risk_manager: RiskManager instance
            broker: Broker instance
            notifier: TelegramNotifier instance
            config: Strategy configuration
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.order_manager = order_manager
        self.risk_manager = risk_manager
        self.broker = broker
        self.notifier = notifier
        self.config = config
        self.last_update_id = 0
        self._running = False
        self.thread = None
        self.start_time = datetime.now(ET)

        # Confirmation state for /close_all
        self._pending_close_all = False

        # Rate limiting (prevent command spam/abuse)
        self.command_timestamps = defaultdict(list)
        self.rate_limit_window = 60  # seconds
        self.rate_limit_max = 10  # commands per window

        logger.info("Telegram command listener initialized (rate limit: 10 commands/min)")

    def start(self):
        """Start polling in a background daemon thread."""
        if self._running:
            logger.warning("Command listener already running")
            return

        self._running = True
        self.thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="TelegramCommandListener"
        )
        self.thread.start()
        logger.info("Telegram command listener started")

    def stop(self):
        """Stop polling."""
        self._running = False
        if self.thread:
            logger.info("Telegram command listener stopped")

    def _poll_loop(self):
        """Poll getUpdates every 5 seconds."""
        while self._running:
            try:
                url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
                resp = httpx.get(url, params={
                    "offset": self.last_update_id + 1,
                    "timeout": 5
                }, timeout=10.0)

                if resp.status_code == 200:
                    data = resp.json()
                    if not data.get("ok"):
                        logger.error(f"Telegram API error: {data}")
                        time.sleep(5)
                        continue

                    for update in data.get("result", []):
                        self.last_update_id = update["update_id"]
                        msg = update.get("message", {})

                        # Security: only respond to the configured chat
                        msg_chat_id = str(msg.get("chat", {}).get("id", ""))
                        if msg_chat_id == self.chat_id:
                            text = msg.get("text", "").strip()
                            if text:
                                logger.info(f"Received command: {text}")
                                self._handle_command(text)
                        else:
                            # Log ignored message for security monitoring
                            logger.warning(
                                f"Ignored command from unauthorized chat: {msg_chat_id}"
                            )

                elif resp.status_code == 401:
                    logger.error("Telegram authentication failed (invalid bot token)")
                    # Don't retry on auth failure
                    break

                else:
                    logger.warning(f"Telegram poll failed: {resp.status_code}")

            except httpx.TimeoutException:
                logger.debug("Telegram poll timeout (normal)")
            except Exception as e:
                logger.error(f"Telegram poll error: {e}", exc_info=True)

            time.sleep(5)

    def _handle_command(self, text: str):
        """Route commands to handlers with rate limiting."""
        command = text.lower().strip()

        # Rate limiting check
        now = time.time()
        self.command_timestamps[self.chat_id] = [
            ts for ts in self.command_timestamps[self.chat_id]
            if now - ts < self.rate_limit_window
        ]

        if len(self.command_timestamps[self.chat_id]) >= self.rate_limit_max:
            logger.warning(f"Rate limit exceeded for chat_id {self.chat_id}")
            self.notifier.send_message(
                "⚠️ <b>Rate limit exceeded</b>\n\n"
                "Maximum 10 commands per minute.\n"
                "Please wait before sending more commands.",
                parse_mode="HTML"
            )
            return

        self.command_timestamps[self.chat_id].append(now)

        try:
            if command == "/status":
                self._cmd_status()
            elif command == "/pause":
                self._cmd_pause()
            elif command == "/resume":
                self._cmd_resume()
            elif command == "/close_all":
                self._cmd_close_all()
            elif command == "/confirm_close":
                self._cmd_confirm_close()
            elif command == "/config":
                self._cmd_config()
            elif command == "/performance":
                self._cmd_performance()
            elif command == "/help":
                self._cmd_help()
            else:
                # Unknown command
                self.notifier.send_message(
                    f"❓ Unknown command: {text}\n\n"
                    f"Type /help to see available commands."
                )
        except Exception as e:
            logger.error(f"Command handler error: {e}", exc_info=True)
            self.notifier.send_message(
                f"⚠️ Error executing command: {e}\n\n"
                f"Check logs for details."
            )

    def _cmd_status(self):
        """Handle /status command."""
        try:
            # Get account info
            account = self.broker.get_account()

            # Get open positions
            positions = self.broker.get_all_positions()

            # Calculate uptime
            uptime_seconds = (datetime.now(ET) - self.start_time).total_seconds()
            hours = int(uptime_seconds // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            uptime = f"{hours}h {minutes}m"

            # Get trading mode
            paper = self.broker.paper if hasattr(self.broker, 'paper') else True
            mode = "Paper Trading" if paper else "LIVE TRADING"

            # Format positions
            if positions:
                pos_lines = []
                for pos in positions:
                    pnl_sign = "+" if pos.unrealized_pl >= 0 else ""
                    pos_lines.append(
                        f"  • {pos.symbol}: {int(pos.qty)} shares, "
                        f"{pnl_sign}{pos.unrealized_plpc:.2f}% "
                        f"(${pnl_sign}{pos.unrealized_pl:.2f})"
                    )
                positions_str = "\n".join(pos_lines)
            else:
                positions_str = "  None"

            # Get trading paused status
            paused = getattr(self.risk_manager, 'trading_paused_manual', False)
            paused_str = "Yes ⏸" if paused else "No ▶️"

            # Format daily P&L
            daily_pnl = self.risk_manager.daily_pnl
            pnl_sign = "+" if daily_pnl >= 0 else ""

            # Get last signal time (from order history)
            last_signal = "None today"
            if hasattr(self.order_manager, 'order_history') and self.order_manager.order_history:
                last_order = self.order_manager.order_history[-1]
                last_signal_time = last_order.get('timestamp', datetime.now(ET))
                last_signal = f"{last_order.get('side', 'UNKNOWN').upper()} at {last_signal_time.strftime('%I:%M %p')}"

            # Build status message
            message = (
                f"📊 <b>AlphaLive Status</b>\n\n"
                f"<b>Mode:</b> {mode}\n"
                f"<b>Strategy:</b> {self.config.strategy.name} on {self.config.ticker}\n"
                f"<b>Timeframe:</b> {self.config.timeframe}\n\n"
                f"<b>Open Positions:</b>\n{positions_str}\n\n"
                f"<b>Daily P&L:</b> {pnl_sign}${daily_pnl:.2f}\n"
                f"<b>Account Equity:</b> ${account.equity:,.2f}\n"
                f"<b>Buying Power:</b> ${account.buying_power:,.2f}\n\n"
                f"<b>Trading Paused:</b> {paused_str}\n"
                f"<b>Uptime:</b> {uptime}\n"
                f"<b>Last Signal:</b> {last_signal}"
            )

            self.notifier.send_message(message, parse_mode="HTML")

        except Exception as e:
            logger.error(f"Error getting status: {e}", exc_info=True)
            self.notifier.send_message(
                f"⚠️ Error getting status: {e}\n\nCheck broker connection."
            )

    def _cmd_pause(self):
        """Handle /pause command."""
        # Set in-memory flag
        self.risk_manager.trading_paused_manual = True

        logger.warning("Trading paused via Telegram /pause command")

        self.notifier.send_message(
            "⏸ <b>Trading Paused</b>\n\n"
            "No new entries will be placed.\n"
            "Open positions will still be monitored for exits.\n\n"
            "Use /resume to re-enable trading.",
            parse_mode="HTML"
        )

    def _cmd_resume(self):
        """Handle /resume command."""
        # Clear in-memory flag
        self.risk_manager.trading_paused_manual = False

        logger.info("Trading resumed via Telegram /resume command")

        self.notifier.send_message(
            "▶️ <b>Trading Resumed</b>\n\n"
            "New signals will be executed.\n"
            "Circuit breaker and other limits still active.",
            parse_mode="HTML"
        )

    def _cmd_close_all(self):
        """Handle /close_all command (ask for confirmation first)."""
        # Get positions count
        positions = self.broker.get_all_positions()

        if not positions:
            self.notifier.send_message("No open positions to close.")
            return

        # Set pending flag and ask for confirmation
        self._pending_close_all = True

        pos_list = "\n".join([
            f"  • {pos.symbol}: {int(pos.qty)} shares"
            for pos in positions
        ])

        self.notifier.send_message(
            f"⚠️ <b>Close ALL Positions?</b>\n\n"
            f"This will close:\n{pos_list}\n\n"
            f"Reply <code>/confirm_close</code> to proceed.",
            parse_mode="HTML"
        )

    def _cmd_confirm_close(self):
        """Handle /confirm_close command."""
        if not self._pending_close_all:
            self.notifier.send_message(
                "No pending close_all request. Use /close_all first."
            )
            return

        # Clear pending flag
        self._pending_close_all = False

        # Close all positions
        positions = self.broker.get_all_positions()

        if not positions:
            self.notifier.send_message("No open positions to close.")
            return

        logger.warning(f"Closing ALL positions via Telegram command ({len(positions)} positions)")

        results = []
        for pos in positions:
            try:
                result = self.order_manager.close_position(
                    pos.symbol,
                    reason="Manual close via Telegram /close_all"
                )

                if result.get("status") == "success":
                    results.append(f"✅ {pos.symbol}: Closed")
                else:
                    results.append(f"❌ {pos.symbol}: {result.get('reason', 'Unknown error')}")

            except Exception as e:
                logger.error(f"Error closing {pos.symbol}: {e}", exc_info=True)
                results.append(f"❌ {pos.symbol}: {e}")

        results_str = "\n".join(results)

        self.notifier.send_message(
            f"🔴 <b>Positions Closed</b>\n\n{results_str}",
            parse_mode="HTML"
        )

    def _cmd_config(self):
        """Handle /config command."""
        # Format trailing stop status
        trailing_stop = "On" if self.config.risk.trailing_stop_enabled else "Off"
        if self.config.risk.trailing_stop_enabled:
            trailing_stop += f" ({self.config.risk.trailing_stop_pct}%)"

        # Format order type
        order_type = self.config.execution.order_type.upper()
        if order_type == "LIMIT":
            order_type += f" (offset: {self.config.execution.limit_offset_pct}%)"

        message = (
            f"⚙️ <b>Strategy Configuration</b>\n\n"
            f"<b>Strategy:</b> {self.config.strategy.name}\n"
            f"<b>Ticker:</b> {self.config.ticker}\n"
            f"<b>Timeframe:</b> {self.config.timeframe}\n\n"
            f"<b>Risk Management:</b>\n"
            f"  • Stop Loss: {self.config.risk.stop_loss_pct}%\n"
            f"  • Take Profit: {self.config.risk.take_profit_pct}%\n"
            f"  • Max Position: {self.config.risk.max_position_size_pct}%\n"
            f"  • Max Daily Loss: {self.config.risk.max_daily_loss_pct}%\n"
            f"  • Max Positions: {self.config.risk.max_open_positions}\n"
            f"  • Trailing Stop: {trailing_stop}\n\n"
            f"<b>Execution:</b>\n"
            f"  • Order Type: {order_type}\n"
            f"  • Cooldown: {self.config.execution.cooldown_bars} bars"
        )

        self.notifier.send_message(message, parse_mode="HTML")

    def _cmd_performance(self):
        """Handle /performance command."""
        try:
            # Get trades from risk manager
            trades = self.risk_manager.daily_trades if hasattr(self.risk_manager, 'daily_trades') else []

            if not trades:
                self.notifier.send_message(
                    "📈 <b>Performance</b>\n\nNo trades yet today.",
                    parse_mode="HTML"
                )
                return

            # Calculate stats
            total_trades = len(trades)
            winning_trades = [t for t in trades if t.get('pnl', 0) > 0]
            losing_trades = [t for t in trades if t.get('pnl', 0) < 0]

            wins = len(winning_trades)
            losses = len(losing_trades)
            win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

            total_pnl = sum(t.get('pnl', 0) for t in trades)

            # Get account equity for percentage
            account = self.broker.get_account()
            pnl_pct = (total_pnl / account.equity * 100) if account.equity > 0 else 0

            # Best/worst trades
            best_trade = max(trades, key=lambda t: t.get('pnl', 0)) if trades else None
            worst_trade = min(trades, key=lambda t: t.get('pnl', 0)) if trades else None

            # Consecutive losses
            consecutive_losses = getattr(self.risk_manager, 'consecutive_losses', 0)

            # Format message
            pnl_sign = "+" if total_pnl >= 0 else ""

            best_str = "N/A"
            if best_trade:
                best_pnl = best_trade.get('pnl', 0)
                best_pct = (best_pnl / account.equity * 100) if account.equity > 0 else 0
                best_str = f"{best_trade.get('ticker', 'UNKNOWN')} +${best_pnl:.2f} (+{best_pct:.2f}%)"

            worst_str = "N/A"
            if worst_trade:
                worst_pnl = worst_trade.get('pnl', 0)
                worst_pct = (worst_pnl / account.equity * 100) if account.equity > 0 else 0
                worst_str = f"{worst_trade.get('ticker', 'UNKNOWN')} ${worst_pnl:.2f} ({worst_pct:.2f}%)"

            # Format start date
            start_date = self.start_time.strftime("%b %d")

            message = (
                f"📈 <b>Performance</b> (since {start_date})\n\n"
                f"<b>Total Trades:</b> {total_trades} ({wins}W / {losses}L)\n"
                f"<b>Total P&L:</b> {pnl_sign}${total_pnl:.2f} ({pnl_sign}{pnl_pct:.2f}%)\n"
                f"<b>Win Rate:</b> {win_rate:.1f}%\n\n"
                f"<b>Best Trade:</b> {best_str}\n"
                f"<b>Worst Trade:</b> {worst_str}\n\n"
                f"<b>Consecutive Losses:</b> {consecutive_losses}"
            )

            self.notifier.send_message(message, parse_mode="HTML")

        except Exception as e:
            logger.error(f"Error getting performance: {e}", exc_info=True)
            self.notifier.send_message(
                f"⚠️ Error getting performance: {e}\n\nCheck logs."
            )

    def _cmd_help(self):
        """Handle /help command."""
        message = (
            "🤖 <b>AlphaLive Commands</b>\n\n"
            "/status — Current bot state and positions\n"
            "/pause — Pause trading (no new entries)\n"
            "/resume — Resume trading\n"
            "/close_all — Close all positions (asks for confirmation)\n"
            "/config — View strategy configuration\n"
            "/performance — Performance stats since bot started\n"
            "/help — Show this help message"
        )

        self.notifier.send_message(message, parse_mode="HTML")
