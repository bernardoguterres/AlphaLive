"""
Risk Manager

The RiskManager is the gatekeeper — nothing trades without its approval.
Checks position sizing, stop loss, take profit, daily limits, and circuit breakers.

Critical: This is the last line of defense against runaway losses.
"""

import os
import logging
from datetime import datetime, date, timezone, timedelta
from typing import Dict, List, Tuple, Optional
from zoneinfo import ZoneInfo

from alphalive.strategy_schema import Risk, Execution, SafetyLimits

logger = logging.getLogger(__name__)

# US Eastern timezone for market hours
ET = ZoneInfo("America/New_York")


class RiskManager:
    """
    Per-strategy risk management.

    Tracks daily P&L, position limits, stop/take profit levels,
    and implements circuit breakers (consecutive losses, daily loss limit).
    """

    def __init__(
        self,
        risk_config: Risk,
        execution_config: Execution,
        strategy_name: str,
        safety_limits: SafetyLimits,
        notifier=None
    ):
        """
        Initialize RiskManager.

        Args:
            risk_config: Risk parameters from strategy config
            execution_config: Execution parameters (cooldown, etc.)
            strategy_name: Strategy identifier for logging
            safety_limits: Safety limits from strategy config
            notifier: Telegram notifier for alerts (optional)
        """
        self.risk_config = risk_config
        self.execution_config = execution_config
        self.strategy_name = strategy_name
        self.notifier = notifier

        # Daily tracking
        self.daily_pnl = 0.0
        self.daily_trades: List[Dict] = []
        self.last_reset_date: Optional[date] = None

        # Circuit breakers
        self.consecutive_losses = 0
        self.max_consecutive_losses = 3
        self.trading_paused_by_circuit_breaker = False

        # Last trade tracking (for cooldown)
        self.last_trade_bar: Dict[str, int] = {}  # ticker -> bar_index

        # Cost safety limits (NEW in B17)
        self.max_trades_per_day = safety_limits.max_trades_per_day
        self.max_api_calls_per_hour = safety_limits.max_api_calls_per_hour
        self.signal_timeout_seconds = safety_limits.signal_generation_timeout_seconds
        self.broker_failure_threshold = safety_limits.broker_degraded_mode_threshold_failures

        # Tracking counters (reset daily/hourly)
        self.trades_today = 0
        self.api_calls_this_hour = 0
        self.last_hour_reset = datetime.now(ET).replace(minute=0, second=0, microsecond=0)
        self.broker_consecutive_failures = 0
        self.degraded_mode = False

        logger.info(
            f"RiskManager initialized | Strategy: {strategy_name} | "
            f"Stop Loss: {risk_config.stop_loss_pct}% | "
            f"Take Profit: {risk_config.take_profit_pct}% | "
            f"Max Position Size: {risk_config.max_position_size_pct}% | "
            f"Max Daily Loss: {risk_config.max_daily_loss_pct}% | "
            f"Max Trades/Day: {self.max_trades_per_day} | "
            f"Max API Calls/Hour: {self.max_api_calls_per_hour} | "
            f"Signal Timeout: {self.signal_timeout_seconds}s | "
            f"Broker Failure Threshold: {self.broker_failure_threshold}"
        )

    def reset_daily(self) -> None:
        """
        Reset daily counters.

        Call this at the start of each trading day to reset:
        - daily_pnl to 0.0
        - daily_trades to empty list
        - consecutive_losses to 0
        - trading_paused_by_circuit_breaker to False
        - trades_today to 0 (NEW in B17)
        """
        today = datetime.now().date()

        # Only reset if it's actually a new day
        if self.last_reset_date is None or self.last_reset_date != today:
            logger.info(
                f"[{self.strategy_name}] Daily reset | "
                f"Previous P&L: ${self.daily_pnl:.2f} | "
                f"Trades: {len(self.daily_trades)} ({self.trades_today} counted) | "
                f"Consecutive Losses: {self.consecutive_losses}"
            )

            self.daily_pnl = 0.0
            self.daily_trades = []
            self.consecutive_losses = 0
            self.trading_paused_by_circuit_breaker = False
            self.trades_today = 0  # NEW: Reset trade counter
            self.last_reset_date = today

            logger.info(f"[{self.strategy_name}] Daily counters reset for {today}")
        else:
            logger.debug(f"[{self.strategy_name}] Already reset today")

    def calculate_position_size(
        self,
        ticker: str,
        signal: str,
        current_price: float,
        account_equity: float
    ) -> int:
        """
        Calculate number of shares to buy/sell.

        Formula:
            max_dollars = account_equity * max_position_size_pct / 100
            shares = floor(max_dollars / current_price)

        Args:
            ticker: Ticker symbol
            signal: "BUY" or "SELL"
            current_price: Current price per share
            account_equity: Total account equity

        Returns:
            Number of shares (0 if position would be invalid)
        """
        if current_price <= 0:
            logger.warning(
                f"[{self.strategy_name}] Invalid price: {current_price} for {ticker}"
            )
            return 0

        if account_equity <= 0:
            logger.warning(
                f"[{self.strategy_name}] Invalid equity: {account_equity}"
            )
            return 0

        # Calculate max dollars for this position
        max_dollars = account_equity * (self.risk_config.max_position_size_pct / 100.0)

        # Calculate shares (always floor to avoid overallocation)
        shares = int(max_dollars / current_price)

        logger.debug(
            f"[{self.strategy_name}] Position sizing | "
            f"{ticker} @ ${current_price:.2f} | "
            f"Max: ${max_dollars:.2f} ({self.risk_config.max_position_size_pct}% of ${account_equity:.2f}) | "
            f"Shares: {shares}"
        )

        return shares

    def check_stop_loss(
        self,
        entry_price: float,
        current_price: float,
        side: str
    ) -> bool:
        """
        Check if stop loss should trigger.

        For long positions:
            Trigger if current_price <= entry_price * (1 - stop_loss_pct/100)

        For short positions:
            Trigger if current_price >= entry_price * (1 + stop_loss_pct/100)

        Args:
            entry_price: Entry price of the position
            current_price: Current market price
            side: "long" or "short"

        Returns:
            True if stop loss should trigger
        """
        stop_loss_multiplier = self.risk_config.stop_loss_pct / 100.0

        if side.lower() == "long":
            stop_price = entry_price * (1 - stop_loss_multiplier)
            triggered = current_price <= stop_price

            if triggered:
                loss_pct = ((current_price - entry_price) / entry_price) * 100
                logger.warning(
                    f"[{self.strategy_name}] STOP LOSS TRIGGERED (long) | "
                    f"Entry: ${entry_price:.2f} | Current: ${current_price:.2f} | "
                    f"Stop: ${stop_price:.2f} | Loss: {loss_pct:.2f}%"
                )
            return triggered

        elif side.lower() == "short":
            stop_price = entry_price * (1 + stop_loss_multiplier)
            triggered = current_price >= stop_price

            if triggered:
                loss_pct = ((entry_price - current_price) / entry_price) * 100
                logger.warning(
                    f"[{self.strategy_name}] STOP LOSS TRIGGERED (short) | "
                    f"Entry: ${entry_price:.2f} | Current: ${current_price:.2f} | "
                    f"Stop: ${stop_price:.2f} | Loss: {loss_pct:.2f}%"
                )
            return triggered

        else:
            logger.error(f"[{self.strategy_name}] Invalid side: {side}")
            return False

    def check_take_profit(
        self,
        entry_price: float,
        current_price: float,
        side: str
    ) -> bool:
        """
        Check if take profit should trigger.

        For long positions:
            Trigger if current_price >= entry_price * (1 + take_profit_pct/100)

        For short positions:
            Trigger if current_price <= entry_price * (1 - take_profit_pct/100)

        Args:
            entry_price: Entry price of the position
            current_price: Current market price
            side: "long" or "short"

        Returns:
            True if take profit should trigger
        """
        take_profit_multiplier = self.risk_config.take_profit_pct / 100.0

        if side.lower() == "long":
            target_price = entry_price * (1 + take_profit_multiplier)
            triggered = current_price >= target_price

            if triggered:
                profit_pct = ((current_price - entry_price) / entry_price) * 100
                logger.info(
                    f"[{self.strategy_name}] 🎯 TAKE PROFIT TRIGGERED (long) | "
                    f"Entry: ${entry_price:.2f} | Current: ${current_price:.2f} | "
                    f"Target: ${target_price:.2f} | Profit: {profit_pct:.2f}%"
                )
            return triggered

        elif side.lower() == "short":
            target_price = entry_price * (1 - take_profit_multiplier)
            triggered = current_price <= target_price

            if triggered:
                profit_pct = ((entry_price - current_price) / entry_price) * 100
                logger.info(
                    f"[{self.strategy_name}] 🎯 TAKE PROFIT TRIGGERED (short) | "
                    f"Entry: ${entry_price:.2f} | Current: ${current_price:.2f} | "
                    f"Target: ${target_price:.2f} | Profit: {profit_pct:.2f}%"
                )
            return triggered

        else:
            logger.error(f"[{self.strategy_name}] Invalid side: {side}")
            return False

    def check_trailing_stop(
        self,
        entry_price: float,
        highest_since_entry: float,
        current_price: float,
        side: str
    ) -> bool:
        """
        Check if trailing stop should trigger.

        Only applies if trailing_stop_enabled in config.
        Trails from the highest price seen since entry.

        For long positions:
            Trigger if current_price <= highest_since_entry * (1 - trailing_stop_pct/100)

        For short positions:
            Trigger if current_price >= lowest_since_entry * (1 + trailing_stop_pct/100)
            Note: highest_since_entry param is actually lowest_since_entry for shorts

        Args:
            entry_price: Entry price of the position
            highest_since_entry: Highest price since entry (for longs) or lowest (for shorts)
            current_price: Current market price
            side: "long" or "short"

        Returns:
            True if trailing stop should trigger
        """
        # Only check if trailing stop is enabled
        if not self.risk_config.trailing_stop_enabled:
            return False

        if self.risk_config.trailing_stop_pct is None:
            logger.error(
                f"[{self.strategy_name}] Trailing stop enabled but trailing_stop_pct is None"
            )
            return False

        trailing_stop_multiplier = self.risk_config.trailing_stop_pct / 100.0

        if side.lower() == "long":
            trail_price = highest_since_entry * (1 - trailing_stop_multiplier)
            triggered = current_price <= trail_price

            if triggered:
                profit_from_entry_pct = ((current_price - entry_price) / entry_price) * 100
                logger.warning(
                    f"[{self.strategy_name}] TRAILING STOP TRIGGERED (long) | "
                    f"Entry: ${entry_price:.2f} | High: ${highest_since_entry:.2f} | "
                    f"Current: ${current_price:.2f} | Trail: ${trail_price:.2f} | "
                    f"P&L from entry: {profit_from_entry_pct:.2f}%"
                )
            return triggered

        elif side.lower() == "short":
            # For shorts, highest_since_entry is actually the lowest price
            trail_price = highest_since_entry * (1 + trailing_stop_multiplier)
            triggered = current_price >= trail_price

            if triggered:
                profit_from_entry_pct = ((entry_price - current_price) / entry_price) * 100
                logger.warning(
                    f"[{self.strategy_name}] TRAILING STOP TRIGGERED (short) | "
                    f"Entry: ${entry_price:.2f} | Low: ${highest_since_entry:.2f} | "
                    f"Current: ${current_price:.2f} | Trail: ${trail_price:.2f} | "
                    f"P&L from entry: {profit_from_entry_pct:.2f}%"
                )
            return triggered

        else:
            logger.error(f"[{self.strategy_name}] Invalid side: {side}")
            return False

    def check_daily_loss_limit(self, account_equity: float) -> bool:
        """
        Check if daily loss limit has been hit.

        Compare daily_pnl against max_daily_loss_pct of equity.

        Args:
            account_equity: Total account equity

        Returns:
            True if daily loss limit has been hit (trading should stop)
        """
        if self.daily_pnl >= 0:
            # No losses, continue trading
            return False

        max_daily_loss_dollars = account_equity * (self.risk_config.max_daily_loss_pct / 100.0)
        loss_pct = (abs(self.daily_pnl) / account_equity) * 100

        hit_limit = abs(self.daily_pnl) >= max_daily_loss_dollars

        if hit_limit:
            logger.critical(
                f"[{self.strategy_name}] 🛑 DAILY LOSS LIMIT HIT | "
                f"P&L: ${self.daily_pnl:.2f} ({loss_pct:.2f}%) | "
                f"Limit: ${max_daily_loss_dollars:.2f} ({self.risk_config.max_daily_loss_pct}%) | "
                f"Equity: ${account_equity:.2f}"
            )
        else:
            logger.debug(
                f"[{self.strategy_name}] Daily loss check | "
                f"P&L: ${self.daily_pnl:.2f} ({loss_pct:.2f}%) | "
                f"Limit: ${max_daily_loss_dollars:.2f} ({self.risk_config.max_daily_loss_pct}%)"
            )

        return hit_limit

    def check_max_positions(self, current_positions_count: int) -> bool:
        """
        Check if we can open more positions for this strategy.

        Args:
            current_positions_count: Number of currently open positions for this strategy

        Returns:
            True if we can open more positions (False if at limit)
        """
        can_open = current_positions_count < self.risk_config.max_open_positions

        if not can_open:
            logger.warning(
                f"[{self.strategy_name}] Max positions reached | "
                f"Current: {current_positions_count} | "
                f"Limit: {self.risk_config.max_open_positions}"
            )
        else:
            logger.debug(
                f"[{self.strategy_name}] Position check | "
                f"Current: {current_positions_count} / {self.risk_config.max_open_positions}"
            )

        return can_open

    def check_cooldown(
        self,
        ticker: str,
        current_bar: int
    ) -> bool:
        """
        Check if enough bars have passed since last trade for this ticker.

        Args:
            ticker: Ticker symbol
            current_bar: Current bar index

        Returns:
            True if cooldown period has passed (can trade)
        """
        if ticker not in self.last_trade_bar:
            # Never traded this ticker before
            return True

        last_bar = self.last_trade_bar[ticker]
        bars_since_last = current_bar - last_bar
        cooldown_bars = self.execution_config.cooldown_bars

        can_trade = bars_since_last >= cooldown_bars

        if not can_trade:
            logger.debug(
                f"[{self.strategy_name}] Cooldown active | "
                f"{ticker} | Last trade: bar {last_bar} | "
                f"Current: bar {current_bar} | "
                f"Need {cooldown_bars} bars, have {bars_since_last}"
            )
        else:
            logger.debug(
                f"[{self.strategy_name}] Cooldown passed | "
                f"{ticker} | Bars since last trade: {bars_since_last} (need {cooldown_bars})"
            )

        return can_trade

    def can_trade(
        self,
        ticker: str,
        signal: str,
        account_equity: float,
        current_positions_count: int,
        total_portfolio_positions: int,
        current_bar: Optional[int] = None
    ) -> Tuple[bool, str]:
        """
        Main gatekeeper - checks ALL limits before allowing a trade.

        Check order:
        1. TRADING_PAUSED env var (kill switch — checked first, always)
        2. Manual pause via Telegram /pause command (in-memory flag)
        3. Trade frequency limit (max trades per day) - NEW in B17
        4. API call budget limit (max calls per hour) - NEW in B17
        5. Degraded mode (broker connection unstable) - NEW in B17
        6. Daily loss limit (global across all strategies)
        7. Consecutive loss circuit breaker (3 stop-outs in a row = pause for the day)
        8. Max positions limit (per-strategy)
        9. Portfolio max positions limit (across ALL strategies)
        10. Cooldown period (if current_bar provided)

        Kill switch: Railway restarts the process when TRADING_PAUSED env var changes.
        Takes ~15-30 seconds, not instant, but reliable.
        Manual pause: Instant via Telegram /pause command (in-memory flag).

        Args:
            ticker: Ticker symbol
            signal: "BUY" or "SELL"
            account_equity: Total account equity
            current_positions_count: Number of open positions for this strategy
            total_portfolio_positions: Total positions across ALL strategies
            current_bar: Current bar index (for cooldown check)

        Returns:
            (True, "OK") if trade is allowed
            (False, "reason") if trade is blocked
        """
        # 1. Check kill switch (TRADING_PAUSED env var)
        #    Railway restarts the process when env vars change (~15-30s)
        #    Not instant, but reliable
        paused = os.environ.get("TRADING_PAUSED", "false").lower()
        if paused in ("true", "1", "yes"):
            reason = "🛑 Trading paused via TRADING_PAUSED env var (kill switch)"
            logger.warning(f"[{self.strategy_name}] {reason}")
            return (False, reason)

        # 2. Check manual pause flag (Telegram /pause command)
        #    Instant in-memory flag (no restart required)
        if getattr(self, 'trading_paused_manual', False):
            reason = "⏸ Trading paused via Telegram /pause command"
            logger.warning(f"[{self.strategy_name}] {reason}")
            return (False, reason)

        # 3. NEW: Trade frequency limit
        if self.trades_today >= self.max_trades_per_day:
            logger.critical(
                f"[{self.strategy_name}] COST SAFETY LIMIT: {self.trades_today} trades today "
                f"(limit: {self.max_trades_per_day}). Auto-pausing trading."
            )
            self.trading_paused_manual = True  # Emergency halt
            if self.notifier:
                self.notifier.send_alert(
                    f"🚨 EMERGENCY HALT\n\n"
                    f"Strategy: {self.strategy_name}\n"
                    f"Max trades per day reached: {self.trades_today}/{self.max_trades_per_day}\n"
                    f"Trading auto-paused. Investigate for runaway signal bugs.\n"
                    f"Resume: /resume (after verification) or set TRADING_PAUSED=false"
                )
            return (False, f"Max trades/day limit: {self.max_trades_per_day}")

        # 4. NEW: API call budget (soft limit at 80%, hard limit at 100%)
        if self.api_calls_this_hour >= self.max_api_calls_per_hour:
            logger.critical(
                f"[{self.strategy_name}] COST SAFETY LIMIT: {self.api_calls_this_hour} API calls this hour "
                f"(limit: {self.max_api_calls_per_hour}). Auto-pausing."
            )
            self.trading_paused_manual = True
            if self.notifier:
                self.notifier.send_alert(
                    f"🚨 EMERGENCY HALT\n\n"
                    f"Strategy: {self.strategy_name}\n"
                    f"Max API calls/hour exceeded: {self.api_calls_this_hour}/{self.max_api_calls_per_hour}\n"
                    f"Approaching Alpaca rate limits. Trading auto-paused.\n"
                    f"This resets at the top of each hour."
                )
            return (False, f"API call limit: {self.max_api_calls_per_hour}/hour")

        # Soft warning at 80% API budget
        if self.api_calls_this_hour >= self.max_api_calls_per_hour * 0.8:
            logger.warning(
                f"[{self.strategy_name}] API call budget 80% used: "
                f"{self.api_calls_this_hour}/{self.max_api_calls_per_hour}"
            )

        # 5. NEW: Degraded mode (broker connection unstable)
        if self.degraded_mode:
            return (False, "Degraded mode — broker connection unstable")

        # 6. Check daily loss limit
        if self.check_daily_loss_limit(account_equity):
            loss_pct = (abs(self.daily_pnl) / account_equity) * 100
            reason = (
                f"🛑 Daily loss limit hit: ${self.daily_pnl:.2f} / {loss_pct:.2f}% "
                f"(limit: {self.risk_config.max_daily_loss_pct}%)"
            )
            logger.warning(f"[{self.strategy_name}] {reason}")
            return (False, reason)

        # 7. Check consecutive loss circuit breaker
        if self.trading_paused_by_circuit_breaker:
            reason = (
                f"⚠️ Trading paused by circuit breaker: "
                f"{self.consecutive_losses} consecutive losses "
                f"(limit: {self.max_consecutive_losses})"
            )
            logger.warning(f"[{self.strategy_name}] {reason}")
            return (False, reason)

        # 8. Check max positions (per-strategy)
        if not self.check_max_positions(current_positions_count):
            reason = (
                f"Max positions reached for strategy: "
                f"{current_positions_count}/{self.risk_config.max_open_positions}"
            )
            logger.warning(f"[{self.strategy_name}] {reason}")
            return (False, reason)

        # 9. Check portfolio max positions (across ALL strategies)
        if total_portfolio_positions >= self.risk_config.portfolio_max_positions:
            reason = (
                f"Portfolio max positions reached: "
                f"{total_portfolio_positions}/{self.risk_config.portfolio_max_positions} "
                f"(across all strategies)"
            )
            logger.warning(f"[{self.strategy_name}] {reason}")
            return (False, reason)

        # 10. Check cooldown period (if current_bar provided)
        if current_bar is not None:
            if not self.check_cooldown(ticker, current_bar):
                bars_since = current_bar - self.last_trade_bar.get(ticker, 0)
                reason = (
                    f"Cooldown active for {ticker}: "
                    f"{bars_since} bars since last trade "
                    f"(need {self.execution_config.cooldown_bars})"
                )
                logger.debug(f"[{self.strategy_name}] {reason}")
                return (False, reason)

        # All checks passed
        logger.info(
            f"[{self.strategy_name}] ✅ Trade approved | "
            f"{ticker} {signal} | "
            f"Positions: {current_positions_count}/{self.risk_config.max_open_positions} | "
            f"Portfolio: {total_portfolio_positions}/{self.risk_config.portfolio_max_positions} | "
            f"Trades today: {self.trades_today}/{self.max_trades_per_day} | "
            f"API calls this hour: {self.api_calls_this_hour}/{self.max_api_calls_per_hour}"
        )
        return (True, "OK")

    def record_trade(
        self,
        ticker: str,
        pnl: float,
        current_bar: Optional[int] = None
    ) -> None:
        """
        Record a completed trade's P&L for daily tracking.

        Updates:
        - daily_pnl (cumulative)
        - daily_trades list
        - consecutive_losses counter
        - trading_paused_by_circuit_breaker flag
        - trades_today counter (NEW in B17)

        If consecutive losses >= max_consecutive_losses:
        - Set trading_paused_by_circuit_breaker = True
        - Log CRITICAL warning
        - Caller should send Telegram alert

        Args:
            ticker: Ticker symbol
            pnl: Profit/Loss in dollars (negative for losses)
            current_bar: Current bar index (for cooldown tracking)
        """
        # Update daily P&L
        self.daily_pnl += pnl

        # NEW: Increment trade counter
        self.trades_today += 1

        # Record trade
        trade_record = {
            "ticker": ticker,
            "pnl": pnl,
            "timestamp": datetime.now().isoformat(),
            "bar": current_bar
        }
        self.daily_trades.append(trade_record)

        # Update consecutive losses counter
        if pnl < 0:
            self.consecutive_losses += 1
            logger.warning(
                f"[{self.strategy_name}] Loss recorded | "
                f"{ticker} | P&L: ${pnl:.2f} | "
                f"Consecutive losses: {self.consecutive_losses} | "
                f"Daily P&L: ${self.daily_pnl:.2f}"
            )
        else:
            # Win resets the counter
            if self.consecutive_losses > 0:
                logger.info(
                    f"[{self.strategy_name}] Win breaks losing streak | "
                    f"{ticker} | P&L: ${pnl:.2f} | "
                    f"Previous consecutive losses: {self.consecutive_losses}"
                )
            self.consecutive_losses = 0
            logger.info(
                f"[{self.strategy_name}] Win recorded | "
                f"{ticker} | P&L: ${pnl:.2f} | "
                f"Daily P&L: ${self.daily_pnl:.2f}"
            )

        # Check circuit breaker
        if self.consecutive_losses >= self.max_consecutive_losses:
            self.trading_paused_by_circuit_breaker = True
            logger.critical(
                f"[{self.strategy_name}] ⚠️ CIRCUIT BREAKER TRIGGERED | "
                f"{self.consecutive_losses} consecutive losses — "
                f"trading paused for the rest of the day"
            )

        # Update last trade bar for cooldown
        if current_bar is not None:
            self.last_trade_bar[ticker] = current_bar
            logger.debug(
                f"[{self.strategy_name}] Cooldown started | "
                f"{ticker} | Last trade bar: {current_bar}"
            )

        # Summary
        logger.info(
            f"[{self.strategy_name}] Trade recorded | "
            f"Total trades today: {len(self.daily_trades)} ({self.trades_today} counted) | "
            f"Daily P&L: ${self.daily_pnl:.2f} | "
            f"Consecutive losses: {self.consecutive_losses}"
        )

    def record_api_call(self, endpoint: str) -> None:
        """
        Track API calls for rate limit protection.

        Increments hourly counter and resets at top of each hour.

        Args:
            endpoint: API endpoint name (for logging)
        """
        now = datetime.now(ET)

        # Reset counter at top of each hour
        if now.hour != self.last_hour_reset.hour:
            logger.info(
                f"[{self.strategy_name}] Hourly API call counter reset. "
                f"Last hour: {self.api_calls_this_hour} calls"
            )
            self.api_calls_this_hour = 0
            self.last_hour_reset = now.replace(minute=0, second=0, microsecond=0)

        self.api_calls_this_hour += 1

        logger.debug(
            f"[{self.strategy_name}] API call: {endpoint} | "
            f"Calls this hour: {self.api_calls_this_hour}/{self.max_api_calls_per_hour}"
        )

    def record_broker_failure(self, error: Exception) -> None:
        """
        Track consecutive broker failures for degraded mode.

        Args:
            error: Exception from broker call
        """
        self.broker_consecutive_failures += 1

        logger.warning(
            f"[{self.strategy_name}] Broker failure #{self.broker_consecutive_failures} | "
            f"Error: {error}"
        )

        if self.broker_consecutive_failures >= self.broker_failure_threshold:
            self.enter_degraded_mode(reason=str(error))

    def record_broker_success(self) -> None:
        """Reset failure counter on successful broker call."""
        if self.broker_consecutive_failures > 0:
            logger.info(
                f"[{self.strategy_name}] Broker connection restored after "
                f"{self.broker_consecutive_failures} failures"
            )
        self.broker_consecutive_failures = 0

    def enter_degraded_mode(self, reason: str) -> None:
        """
        Enter degraded mode: no new entries, exits on best effort with cached data.

        Degraded mode is triggered after consecutive broker failures exceed threshold.
        Trading is paused until broker connection is restored.

        Args:
            reason: Reason for entering degraded mode
        """
        if self.degraded_mode:
            return  # Already in degraded mode

        self.degraded_mode = True
        logger.critical(
            f"[{self.strategy_name}] ENTERING DEGRADED MODE: Broker connection failed "
            f"{self.broker_consecutive_failures} consecutive times. Reason: {reason}"
        )

        if self.notifier:
            self.notifier.send_alert(
                f"⚠️ DEGRADED MODE\n\n"
                f"Strategy: {self.strategy_name}\n"
                f"Broker API connection unstable ({self.broker_consecutive_failures} failures).\n"
                f"No new entries will be placed.\n"
                f"Exits will attempt on best effort (using last known prices).\n"
                f"Retrying connection every 5 minutes.\n\n"
                f"Error: {reason}"
            )

    def exit_degraded_mode(self) -> None:
        """Exit degraded mode after broker connection restored."""
        if not self.degraded_mode:
            return

        logger.info(f"[{self.strategy_name}] Exiting degraded mode — broker connection stable")
        self.degraded_mode = False
        self.broker_consecutive_failures = 0

        if self.notifier:
            self.notifier.send_alert(
                f"✅ DEGRADED MODE CLEARED\n\n"
                f"Strategy: {self.strategy_name}\n"
                f"Broker connection restored. Normal trading resumed."
            )

    def get_safety_stats(self) -> Dict:
        """
        Get current safety limit statistics for monitoring.

        Returns:
            Dictionary with safety stats
        """
        return {
            "trades_today": self.trades_today,
            "max_trades_per_day": self.max_trades_per_day,
            "api_calls_this_hour": self.api_calls_this_hour,
            "max_api_calls_per_hour": self.max_api_calls_per_hour,
            "degraded_mode": self.degraded_mode,
            "broker_consecutive_failures": self.broker_consecutive_failures,
            "broker_failure_threshold": self.broker_failure_threshold
        }


class GlobalRiskManager:
    """
    Global Risk Manager for Multi-Strategy Mode.

    Tracks cross-strategy metrics and enforces portfolio-level limits:
    - Global daily P&L (sum across all strategies)
    - Global daily loss limit (halts ALL strategies if exceeded)

    This wraps multiple RiskManager instances and provides global checks
    that must pass BEFORE executing any signal across all strategies.
    """

    def __init__(self):
        """Initialize global risk manager."""
        # Global daily stats
        self.global_daily_stats = {
            "date": date.today(),
            "total_trades": 0,
            "total_pnl": 0.0,
            "start_equity": None,
            "strategies_halted": False,
            "halt_reason": None
        }

        # Track individual strategy managers
        self.strategy_managers: Dict[str, RiskManager] = {}

        logger.info("Global risk manager initialized for multi-strategy mode")

    def register_strategy(self, strategy_name: str, risk_manager: RiskManager) -> None:
        """
        Register a strategy's risk manager.

        Args:
            strategy_name: Strategy identifier
            risk_manager: Strategy's RiskManager instance
        """
        self.strategy_managers[strategy_name] = risk_manager
        logger.info(f"Registered strategy: {strategy_name}")

    def check_global_daily_loss(
        self,
        account_equity: float,
        max_daily_loss_pct: float
    ) -> Tuple[bool, str]:
        """
        Check if global daily loss limit has been exceeded.

        This sums daily_pnl across ALL registered strategies and compares
        against the global limit. If exceeded, ALL strategies halt.

        Args:
            account_equity: Total account equity
            max_daily_loss_pct: Maximum daily loss percentage (e.g., 5.0 for 5%)

        Returns:
            (True, "OK") if trading can continue
            (False, "reason") if global limit hit
        """
        # Reset if new day
        self._check_daily_reset()

        # Check if already halted
        if self.global_daily_stats["strategies_halted"]:
            return (False, self.global_daily_stats["halt_reason"])

        # Set start equity on first check
        if self.global_daily_stats["start_equity"] is None:
            self.global_daily_stats["start_equity"] = account_equity
            logger.info(f"Global risk tracking started | Start equity: ${account_equity:.2f}")

        # Calculate total daily P&L across all strategies
        total_pnl = sum(
            manager.daily_pnl
            for manager in self.strategy_managers.values()
        )

        start_equity = self.global_daily_stats["start_equity"]
        total_pnl_pct = (total_pnl / start_equity) * 100

        self.global_daily_stats["total_pnl"] = total_pnl

        # Check against limit
        if total_pnl_pct <= -max_daily_loss_pct:
            reason = (
                f"🛑 GLOBAL daily loss limit exceeded: ${total_pnl:.2f} ({total_pnl_pct:.2f}%) — "
                f"ALL strategies halted for today"
            )
            self.global_daily_stats["strategies_halted"] = True
            self.global_daily_stats["halt_reason"] = reason
            logger.critical(reason)
            return (False, reason)

        logger.debug(f"Global daily P&L: ${total_pnl:.2f} ({total_pnl_pct:.2f}%)")
        return (True, "OK")

    def _check_daily_reset(self) -> None:
        """Reset global stats if new day."""
        today = date.today()

        if self.global_daily_stats["date"] != today:
            logger.info(f"Resetting global daily stats for {today}")

            # Log previous day summary
            prev_stats = self.global_daily_stats
            logger.info(
                f"Previous day summary | "
                f"Trades: {prev_stats['total_trades']} | "
                f"P&L: ${prev_stats['total_pnl']:.2f} | "
                f"Halted: {prev_stats['strategies_halted']}"
            )

            self.global_daily_stats = {
                "date": today,
                "total_trades": 0,
                "total_pnl": 0.0,
                "start_equity": None,
                "strategies_halted": False,
                "halt_reason": None
            }

    def record_trade(self, strategy_name: str, pnl: float) -> None:
        """
        Record a completed trade for global tracking.

        Args:
            strategy_name: Name of strategy that executed the trade
            pnl: Profit/loss from the trade
        """
        self._check_daily_reset()

        self.global_daily_stats["total_trades"] += 1
        logger.debug(
            f"Trade recorded | Strategy: {strategy_name} | "
            f"P&L: ${pnl:.2f} | Total trades today: {self.global_daily_stats['total_trades']}"
        )

    def get_global_stats(self) -> Dict:
        """
        Get global daily statistics.

        Returns:
            Dictionary with global stats
        """
        return self.global_daily_stats.copy()

    def is_trading_halted(self) -> bool:
        """
        Check if trading is globally halted.

        Returns:
            True if all strategies should be halted
        """
        return self.global_daily_stats["strategies_halted"]
