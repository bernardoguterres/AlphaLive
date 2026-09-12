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
from zoneinfo import ZoneInfo
from collections import defaultdict

from alphalive.utils.env_bool import read_bool_env

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

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        order_manager,
        risk_manager,
        broker,
        notifier,
        config,
        global_risk=None,
        order_manager_map=None,
        strategy_configs=None,
    ):
        """
        Initialize command listener.

        Args:
            bot_token: Telegram bot token
            chat_id: Only respond to this chat (security)
            order_manager: OrderManager instance (first configured strategy -
                used only as the fallback single entry when
                order_manager_map is not provided).
            risk_manager: RiskManager instance (first configured strategy -
                same fallback role, and used for /pause and /resume when
                global_risk is not provided).
            broker: Broker instance. Shared across every strategy (one
                Alpaca account), so broker.get_all_positions() already
                returns every strategy's positions regardless of how many
                strategies are configured.
            notifier: TelegramNotifier instance
            config: Strategy configuration (first configured strategy -
                same fallback role as order_manager/risk_manager).
            global_risk: Optional GlobalRiskManager shared across every
                registered strategy. When provided, /pause and /resume
                operate on it instead of the single risk_manager, so they
                actually gate every strategy in multi-strategy mode (see
                RiskManager.can_trade()'s check of
                global_risk.is_manual_paused()), and its
                strategy_managers dict becomes the source of truth for
                per-strategy risk state (/status, /performance).
            order_manager_map: Optional {ticker: OrderManager} covering
                every configured strategy. When provided, /close_all closes
                each position through the OrderManager that actually owns
                its ticker (correct submission-intent/ledger attribution)
                instead of always using the single `order_manager`.
            strategy_configs: Optional {ticker: StrategySchema} covering
                every configured strategy, for /config and /status.

        With exactly one configured strategy (order_manager_map/
        strategy_configs omitted, or containing a single entry),
        /status /config /performance /close_all behave exactly as the
        original single-strategy, untargeted commands did - no behavior
        change for single-strategy deployments. With more than one, /config
        and /performance return a per-strategy summary unless given an
        explicit ticker argument (e.g. "/config AAPL"), and /close_all,
        /status aggregate across all of them - never silently defaulting to
        just the first configured strategy.
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.order_manager = order_manager
        self.risk_manager = risk_manager
        self.broker = broker
        self.notifier = notifier
        self.config = config
        self.global_risk = global_risk

        self.order_manager_map = order_manager_map or {config.ticker: order_manager}
        self.strategy_configs = strategy_configs or {config.ticker: config}
        self.risk_manager_map = (
            global_risk.strategy_managers
            if global_risk is not None and global_risk.strategy_managers
            else {config.ticker: risk_manager}
        )

        self.last_update_id = 0
        self._running = False
        self.thread = None
        self.start_time = datetime.now(ET)

        # Confirmation state for /close_all (expires after 60 seconds)
        self._pending_close_all = False
        self._close_all_requested_at: float = 0.0

        # Rate limiting (prevent command spam/abuse)
        self.command_timestamps = defaultdict(list)
        self.rate_limit_window = 60  # seconds
        self.rate_limit_max = 10  # commands per window

        logger.info(
            "Telegram command listener initialized (rate limit: 10 commands/min)"
        )

    def _delete_webhook(self):
        """Delete any existing webhook to avoid 409 conflict with getUpdates."""
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/deleteWebhook"
            resp = httpx.post(url, timeout=10.0)
            if resp.status_code == 200:
                logger.info("Telegram webhook deleted (ready for polling)")
            else:
                logger.warning(
                    f"Failed to delete webhook: {resp.status_code} - {resp.text}"
                )
        except Exception as e:
            logger.warning(f"Error deleting webhook: {e}")

    def start(self):
        """Start polling in a background daemon thread."""
        if self._running:
            logger.warning("Command listener already running")
            return

        # Delete any existing webhook before polling (prevents 409 conflict)
        self._delete_webhook()

        self._running = True
        self.thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="TelegramCommandListener"
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
                resp = httpx.get(
                    url,
                    params={"offset": self.last_update_id + 1, "timeout": 5},
                    timeout=10.0,
                )

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
                    # Log full error for debugging (especially 409 conflicts)
                    logger.warning(
                        f"Telegram poll failed: {resp.status_code} - {resp.text[:200]}"
                    )

            except httpx.TimeoutException:
                logger.debug("Telegram poll timeout (normal)")
            except Exception as e:
                logger.error(f"Telegram poll error: {e}", exc_info=True)

            time.sleep(5)

    def _handle_command(self, text: str):
        """Route commands to handlers with rate limiting."""
        parts = text.strip().split(maxsplit=1)
        command = parts[0].lower() if parts else ""
        arg = parts[1].strip().upper() if len(parts) > 1 else None

        # Rate limiting check
        now = time.time()
        self.command_timestamps[self.chat_id] = [
            ts
            for ts in self.command_timestamps[self.chat_id]
            if now - ts < self.rate_limit_window
        ]

        if len(self.command_timestamps[self.chat_id]) >= self.rate_limit_max:
            logger.warning(f"Rate limit exceeded for chat_id {self.chat_id}")
            self.notifier.send_message(
                "⚠️ <b>Rate limit exceeded</b>\n\n"
                "Maximum 10 commands per minute.\n"
                "Please wait before sending more commands.",
                parse_mode="HTML",
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
                self._cmd_config(arg)
            elif command == "/performance":
                self._cmd_performance(arg)
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
                f"⚠️ Error executing command: {e}\n\n" f"Check logs for details."
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
            paper = self.broker.paper if hasattr(self.broker, "paper") else True
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

            # Get trading paused status - the global flag (reflects what
            # /pause and /resume actually control) when available;
            # otherwise ANY registered strategy's own manual-pause flag,
            # so single-strategy deployments without a GlobalRiskManager
            # still report truthfully rather than silently checking only
            # the first configured strategy.
            if self.global_risk is not None:
                paused = self.global_risk.is_manual_paused()
            else:
                paused = any(
                    getattr(rm, "trading_paused_manual", False)
                    for rm in self.risk_manager_map.values()
                )
            paused_str = "Yes ⏸" if paused else "No ▶️"

            multi = len(self.strategy_configs) > 1

            # Aggregate daily P&L truthfully across every registered
            # strategy, not just the first configured one.
            total_daily_pnl = sum(
                getattr(rm, "daily_pnl", 0.0) for rm in self.risk_manager_map.values()
            )
            pnl_sign = "+" if total_daily_pnl >= 0 else ""

            if multi:
                strategy_lines = []
                for ticker, cfg in self.strategy_configs.items():
                    rm = self.risk_manager_map.get(ticker)
                    strat_pnl = getattr(rm, "daily_pnl", 0.0) if rm else 0.0
                    strat_sign = "+" if strat_pnl >= 0 else ""
                    halt_note = ""
                    if rm is not None:
                        if getattr(rm, "trading_paused_by_circuit_breaker", False):
                            halt_note = " ⛔ circuit breaker"
                        elif getattr(rm, "degraded_mode", False):
                            halt_note = " ⚠️ degraded mode"
                        elif getattr(rm, "trading_paused_manual", False):
                            halt_note = " ⏸ paused"
                    strategy_lines.append(
                        f"  • {cfg.strategy.name}/{ticker}: "
                        f"{strat_sign}${strat_pnl:.2f}{halt_note}"
                    )
                strategy_block = (
                    f"<b>Strategies ({len(self.strategy_configs)}):</b>\n"
                    + "\n".join(strategy_lines)
                    + "\n\n"
                )
            else:
                only_ticker, only_cfg = next(iter(self.strategy_configs.items()))
                strategy_block = (
                    f"<b>Strategy:</b> {only_cfg.strategy.name} on {only_ticker}\n"
                    f"<b>Timeframe:</b> {only_cfg.timeframe}\n\n"
                )

            # Get last signal time across every OrderManager's order history
            # - never just the first configured strategy's.
            last_signal = "None today"
            candidates = []
            for ticker, om in self.order_manager_map.items():
                history = getattr(om, "order_history", None)
                if history:
                    candidates.append(history[-1])
            if candidates:
                last_order = max(
                    candidates,
                    key=lambda o: o.get("timestamp", datetime(1970, 1, 1, tzinfo=ET)),
                )
                last_signal_time = last_order.get("timestamp", datetime.now(ET))
                last_signal_ticker = last_order.get("ticker", "")
                last_signal = (
                    f"{last_order.get('side', 'UNKNOWN').upper()} "
                    f"{last_signal_ticker} at {last_signal_time.strftime('%I:%M %p')}"
                ).strip()

            pnl_label = "Total Daily P&L" if multi else "Daily P&L"

            # Build status message
            message = (
                f"📊 <b>AlphaLive Status</b>\n\n"
                f"<b>Mode:</b> {mode}\n"
                f"{strategy_block}"
                f"<b>Open Positions:</b>\n{positions_str}\n\n"
                f"<b>{pnl_label}:</b> {pnl_sign}${total_daily_pnl:.2f}\n"
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
        """Handle /pause command.

        Routes through the shared GlobalRiskManager when available, so this
        gates every registered strategy's can_trade() - not just whichever
        strategy this listener happens to hold a direct risk_manager
        reference to (see __init__'s global_risk docstring).
        """
        if self.global_risk is not None:
            self.global_risk.set_manual_pause("Paused via Telegram /pause command")
            scope = (
                f"{len(self.global_risk.strategy_managers)} strategies"
                if len(self.global_risk.strategy_managers) > 1
                else "all trading"
            )
        else:
            self.risk_manager.trading_paused_manual = True
            scope = f"{self.config.ticker} only (no global control configured)"

        logger.warning("Trading paused via Telegram /pause command")

        self.notifier.send_message(
            f"⏸ <b>Trading Paused</b> ({scope})\n\n"
            "No new entries will be placed.\n"
            "Open positions will still be monitored for exits.\n\n"
            "Use /resume to re-enable trading.",
            parse_mode="HTML",
        )

    def _cmd_resume(self):
        """Handle /resume command.

        Clears ONLY the Telegram manual-pause flag. Other active halts -
        the TRADING_PAUSED env var, the dashboard kill switch, a per-
        strategy circuit breaker or degraded-mode auto-pause, the global
        daily-loss halt - are independent and are NOT cleared by /resume;
        the response below reports any of those that are still active so
        the operator isn't told trading resumed when it didn't.
        """
        if self.global_risk is not None:
            self.global_risk.clear_manual_pause()
        else:
            self.risk_manager.trading_paused_manual = False

        logger.info("Trading resumed via Telegram /resume command")

        remaining = self._describe_other_active_halts()
        if remaining:
            message = (
                "▶️ <b>Telegram Pause Cleared</b>\n\n"
                "Telegram's manual pause is off, but trading is still "
                "halted for other reasons:\n"
                + "\n".join(f"  • {r}" for r in remaining)
                + "\n\nResolve those separately to actually resume trading."
            )
        else:
            message = (
                "▶️ <b>Trading Resumed</b>\n\n"
                "New signals will be executed.\n"
                "Circuit breaker and other limits still active."
            )

        self.notifier.send_message(message, parse_mode="HTML")

    def _describe_other_active_halts(self) -> list:
        """Return human-readable descriptions of any halt still active
        after clearing the Telegram manual pause, so /resume never claims
        trading resumed when the env var, a circuit breaker, or degraded
        mode is still blocking it."""
        halts = []
        try:
            if read_bool_env("TRADING_PAUSED", default=False):
                halts.append("TRADING_PAUSED environment variable is set")
        except ValueError:
            halts.append("TRADING_PAUSED environment variable is malformed")

        if self.global_risk is not None:
            stats = self.global_risk.get_global_stats()
            if stats.get("strategies_halted"):
                halts.append(
                    f"Global daily loss limit: {stats.get('halt_reason', 'halted')}"
                )
            for ticker, rm in self.global_risk.strategy_managers.items():
                if getattr(rm, "trading_paused_by_circuit_breaker", False):
                    halts.append(f"{ticker}: consecutive-loss circuit breaker")
                if getattr(rm, "degraded_mode", False):
                    halts.append(f"{ticker}: broker degraded mode")
        elif getattr(self.risk_manager, "trading_paused_by_circuit_breaker", False):
            halts.append("Consecutive-loss circuit breaker")

        return halts

    def _cmd_close_all(self):
        """Handle /close_all command (ask for confirmation first).

        Operates across every configured strategy: broker.get_all_positions()
        already returns every position in the (single, shared) Alpaca
        account regardless of how many strategies are configured.
        """
        positions = self.broker.get_all_positions()

        if not positions:
            self.notifier.send_message("No open positions to close.")
            return

        # Set pending flag and timestamp for expiry check
        self._pending_close_all = True
        self._close_all_requested_at = time.time()

        pos_list = "\n".join(
            [f"  • {pos.symbol}: {int(pos.qty)} shares" for pos in positions]
        )
        scope_note = (
            f" across {len(self.strategy_configs)} strategies"
            if len(self.strategy_configs) > 1
            else ""
        )

        self.notifier.send_message(
            f"⚠️ <b>Close ALL Positions?</b>{scope_note}\n\n"
            f"This will close:\n{pos_list}\n\n"
            f"Reply <code>/confirm_close</code> to proceed.",
            parse_mode="HTML",
        )

    def _cmd_confirm_close(self):
        """Handle /confirm_close command.

        Each position is closed through the OrderManager that actually owns
        its ticker (order_manager_map), so the durable submission-intent
        lifecycle and ledger attribution are correct per strategy - not
        always routed through a single (first-configured) OrderManager. A
        position for a ticker no strategy currently owns falls back to the
        default order_manager with a warning (best-effort; happens only if
        a config changed since the position was opened).

        Idempotent: OrderManager.close_position() itself recognizes an
        already-closed position (via the CLOSE submission-intent
        reconciliation against the broker's actual position state) and
        returns success without re-issuing the broker call - so a repeated
        /close_all after a partial success only re-attempts what's still
        open. An "uncertain" (quarantined) result is reported distinctly,
        never folded into "Closed".
        """
        if not self._pending_close_all:
            self.notifier.send_message(
                "No pending close_all request. Use /close_all first."
            )
            return

        if time.time() - self._close_all_requested_at > 60:
            self._pending_close_all = False
            self.notifier.send_message(
                "Confirmation window expired (60 s). Use /close_all again."
            )
            return

        # Clear pending flag
        self._pending_close_all = False

        # Close all positions
        positions = self.broker.get_all_positions()

        if not positions:
            self.notifier.send_message("No open positions to close.")
            return

        logger.warning(
            f"Closing ALL positions via Telegram command ({len(positions)} positions)"
        )

        results = []
        any_incomplete = False
        for pos in positions:
            om = self.order_manager_map.get(pos.symbol)
            if om is None:
                om = self.order_manager
                logger.warning(
                    f"/close_all: {pos.symbol} has no configured OrderManager - "
                    f"using the default strategy's OrderManager as a fallback."
                )
            try:
                result = om.close_position(
                    pos.symbol, reason="Manual close via Telegram /close_all"
                )
                status = result.get("status")

                if status == "success":
                    results.append(f"✅ {pos.symbol}: Closed")
                elif status == "partial":
                    # Order accepted and partially filled, position still
                    # open - a checkmark here would be a false all-clear.
                    any_incomplete = True
                    filled = result.get("filled_qty")
                    results.append(
                        f"🟡 {pos.symbol}: Partially filled"
                        + (f" ({filled} shares so far)" if filled else "")
                    )
                elif status == "pending":
                    any_incomplete = True
                    results.append(f"⏳ {pos.symbol}: Order submitted, not yet filled")
                elif status == "blocked":
                    # Quarantined submission intent - genuinely unknown
                    # outcome, never reported as closed.
                    any_incomplete = True
                    results.append(
                        f"❓ {pos.symbol}: Uncertain - {result.get('reason', 'quarantined')}"
                    )
                else:
                    any_incomplete = True
                    results.append(
                        f"❌ {pos.symbol}: {result.get('reason', 'Unknown error')}"
                    )

            except Exception as e:
                any_incomplete = True
                logger.error(f"Error closing {pos.symbol}: {e}", exc_info=True)
                results.append(f"❌ {pos.symbol}: {e}")

        results_str = "\n".join(results)

        if any_incomplete:
            header = "⚠️ <b>Close ALL: Incomplete</b>"
            footer = (
                "\n\nNot every position was confirmed closed - review above "
                "before assuming trading is flat. Send /close_all again to "
                "retry what's still open."
            )
        else:
            header = "🔴 <b>Positions Closed</b>"
            footer = ""

        self.notifier.send_message(
            f"{header}\n\n{results_str}{footer}", parse_mode="HTML"
        )

    def _cmd_config(self, ticker=None):
        """Handle /config [TICKER] command.

        One configured strategy: untargeted /config shows it (unchanged
        behavior). Multiple: an explicit ticker shows that strategy's full
        config; no ticker returns a compact, clearly-labelled per-strategy
        summary instead of silently defaulting to the first one.
        """
        if ticker:
            cfg = self.strategy_configs.get(ticker)
            if cfg is None:
                self.notifier.send_message(
                    f"❓ Unknown strategy ticker: {ticker}\n\n"
                    f"Configured: {', '.join(self.strategy_configs.keys())}"
                )
                return
            self.notifier.send_message(self._format_config(cfg), parse_mode="HTML")
            return

        if len(self.strategy_configs) == 1:
            cfg = next(iter(self.strategy_configs.values()))
            self.notifier.send_message(self._format_config(cfg), parse_mode="HTML")
            return

        sections = [
            self._format_config(cfg, compact=True)
            for cfg in self.strategy_configs.values()
        ]
        self.notifier.send_message(
            f"⚙️ <b>Strategy Configurations ({len(self.strategy_configs)})</b>\n\n"
            + "\n\n".join(sections)
            + "\n\nUse /config TICKER for full details on one strategy.",
            parse_mode="HTML",
        )

    def _format_config(self, cfg, compact: bool = False) -> str:
        trailing_stop = "On" if cfg.risk.trailing_stop_enabled else "Off"
        if cfg.risk.trailing_stop_enabled:
            trailing_stop += f" ({cfg.risk.trailing_stop_pct}%)"

        order_type = cfg.execution.order_type.upper()
        if order_type == "LIMIT":
            order_type += f" (offset: {cfg.execution.limit_offset_pct}%)"

        if compact:
            return (
                f"<b>{cfg.strategy.name}/{cfg.ticker}</b> ({cfg.timeframe}): "
                f"SL {cfg.risk.stop_loss_pct}% / TP {cfg.risk.take_profit_pct}% / "
                f"Trailing {trailing_stop} / {order_type}"
            )

        return (
            f"⚙️ <b>Strategy Configuration</b>\n\n"
            f"<b>Strategy:</b> {cfg.strategy.name}\n"
            f"<b>Ticker:</b> {cfg.ticker}\n"
            f"<b>Timeframe:</b> {cfg.timeframe}\n\n"
            f"<b>Risk Management:</b>\n"
            f"  • Stop Loss: {cfg.risk.stop_loss_pct}%\n"
            f"  • Take Profit: {cfg.risk.take_profit_pct}%\n"
            f"  • Max Position: {cfg.risk.max_position_size_pct}%\n"
            f"  • Max Daily Loss: {cfg.risk.max_daily_loss_pct}%\n"
            f"  • Max Positions: {cfg.risk.max_open_positions}\n"
            f"  • Trailing Stop: {trailing_stop}\n\n"
            f"<b>Execution:</b>\n"
            f"  • Order Type: {order_type}\n"
            f"  • Cooldown: {cfg.execution.cooldown_bars} bars"
        )

    def _cmd_performance(self, ticker=None):
        """Handle /performance [TICKER] command.

        Same targeting rule as /config: one strategy stays untargeted and
        unchanged; multiple require an explicit ticker for full detail, or
        return a labelled per-strategy summary otherwise.
        """
        try:
            account = self.broker.get_account()

            if ticker:
                rm = self.risk_manager_map.get(ticker)
                if rm is None:
                    self.notifier.send_message(
                        f"❓ Unknown strategy ticker: {ticker}\n\n"
                        f"Configured: {', '.join(self.risk_manager_map.keys())}"
                    )
                    return
                self.notifier.send_message(
                    self._format_performance(ticker, rm, account), parse_mode="HTML"
                )
                return

            if len(self.risk_manager_map) == 1:
                only_ticker, rm = next(iter(self.risk_manager_map.items()))
                self.notifier.send_message(
                    self._format_performance(only_ticker, rm, account),
                    parse_mode="HTML",
                )
                return

            sections = [
                self._format_performance(t, rm, account, compact=True)
                for t, rm in self.risk_manager_map.items()
            ]
            self.notifier.send_message(
                f"📈 <b>Performance ({len(self.risk_manager_map)} strategies)</b>\n\n"
                + "\n".join(sections)
                + "\n\nUse /performance TICKER for full details on one strategy.",
                parse_mode="HTML",
            )

        except Exception as e:
            logger.error(f"Error getting performance: {e}", exc_info=True)
            self.notifier.send_message(
                f"⚠️ Error getting performance: {e}\n\nCheck logs."
            )

    def _format_performance(self, ticker, rm, account, compact: bool = False) -> str:
        trades = getattr(rm, "daily_trades", None) or []

        if not trades:
            return (
                f"<b>{ticker}</b>: No trades yet today."
                if compact
                else f"📈 <b>Performance ({ticker})</b>\n\nNo trades yet today."
            )

        total_trades = len(trades)
        wins = len([t for t in trades if t.get("pnl", 0) > 0])
        losses = len([t for t in trades if t.get("pnl", 0) < 0])
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        pnl_pct = (total_pnl / account.equity * 100) if account.equity > 0 else 0
        pnl_sign = "+" if total_pnl >= 0 else ""

        if compact:
            return (
                f"  • <b>{ticker}</b>: {total_trades} trades ({wins}W/{losses}L), "
                f"{pnl_sign}${total_pnl:.2f} ({win_rate:.0f}% win rate)"
            )

        best_trade = max(trades, key=lambda t: t.get("pnl", 0))
        worst_trade = min(trades, key=lambda t: t.get("pnl", 0))
        best_pnl = best_trade.get("pnl", 0)
        worst_pnl = worst_trade.get("pnl", 0)
        best_pct = (best_pnl / account.equity * 100) if account.equity > 0 else 0
        worst_pct = (worst_pnl / account.equity * 100) if account.equity > 0 else 0
        best_str = (
            f"{best_trade.get('ticker', 'UNKNOWN')} +${best_pnl:.2f} (+{best_pct:.2f}%)"
        )
        worst_str = f"{worst_trade.get('ticker', 'UNKNOWN')} ${worst_pnl:.2f} ({worst_pct:.2f}%)"
        consecutive_losses = getattr(rm, "consecutive_losses", 0)
        start_date = self.start_time.strftime("%b %d")

        return (
            f"📈 <b>Performance ({ticker})</b> (since {start_date})\n\n"
            f"<b>Total Trades:</b> {total_trades} ({wins}W / {losses}L)\n"
            f"<b>Total P&L:</b> {pnl_sign}${total_pnl:.2f} ({pnl_sign}{pnl_pct:.2f}%)\n"
            f"<b>Win Rate:</b> {win_rate:.1f}%\n\n"
            f"<b>Best Trade:</b> {best_str}\n"
            f"<b>Worst Trade:</b> {worst_str}\n\n"
            f"<b>Consecutive Losses:</b> {consecutive_losses}"
        )

    def _cmd_help(self):
        """Handle /help command."""
        multi = len(self.strategy_configs) > 1
        targeting_note = (
            "\n\nMultiple strategies configured: /config and /performance "
            "show a per-strategy summary unless given a ticker, e.g. "
            "<code>/config AAPL</code>."
            if multi
            else ""
        )
        message = (
            "🤖 <b>AlphaLive Commands</b>\n\n"
            "/status - Aggregate bot state and positions across all strategies\n"
            "/pause - Pause trading globally (no new entries, any strategy)\n"
            "/resume - Resume trading (reports any other halt still active)\n"
            "/close_all - Close all positions, all strategies (asks for confirmation)\n"
            "/config [TICKER] - View strategy configuration\n"
            "/performance [TICKER] - Performance stats since bot started\n"
            "/help - Show this help message"
            f"{targeting_note}"
        )

        self.notifier.send_message(message, parse_mode="HTML")
