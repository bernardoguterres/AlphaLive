"""
Order Manager

Handles order placement, tracking, and position management with:
- Order placement with retry logic
- Duplicate order prevention
- Slippage checks
- Partial fill handling
- Idempotency keys
"""

import os
import time
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime
from zoneinfo import ZoneInfo

from alphalive.broker.base_broker import BaseBroker
from alphalive.execution.risk_manager import RiskManager
from alphalive.strategy_schema import StrategySchema

logger = logging.getLogger(__name__)

# Eastern Time (US stock market timezone)
ET = ZoneInfo("America/New_York")


class OrderManager:
    """
    Manages order lifecycle and execution.

    Wraps the broker and adds:
    - Order placement with retry logic
    - Order status tracking
    - Duplicate order prevention
    - Partial fill handling
    - Slippage checks
    - Idempotency keys
    """

    def __init__(
        self,
        broker: BaseBroker,
        risk_manager: RiskManager,
        config: StrategySchema,
        notifier=None,
        dry_run: bool = False
    ):
        """
        Initialize order manager.

        Args:
            broker: Broker instance for order execution
            risk_manager: Risk manager for limit checks
            config: Strategy configuration
            notifier: Telegram notifier (optional)
            dry_run: If True, log orders without executing
        """
        self.broker = broker
        self.risk = risk_manager
        self.config = config
        self.notifier = notifier
        self.dry_run = dry_run

        # Order tracking
        self.order_history: List[Dict[str, Any]] = []  # All orders placed today
        self.pending_orders: Dict[str, str] = {}  # {ticker: order_id}

        logger.info(
            f"OrderManager initialized | "
            f"Strategy: {config.strategy.name} | "
            f"Ticker: {config.ticker} | "
            f"OrderType: {config.execution.order_type} | "
            f"DryRun: {dry_run}"
        )

    def execute_signal(
        self,
        ticker: str,
        signal: Dict[str, Any],
        current_price: float,
        account_equity: float,
        current_positions_count: int,
        total_portfolio_positions: int,
        current_bar: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Execute a BUY or SELL signal.

        Args:
            ticker: Ticker symbol
            signal: Signal dict from signal engine
            current_price: Current market price
            account_equity: Total account equity
            current_positions_count: Current open positions for this strategy
            total_portfolio_positions: Total positions across all strategies
            current_bar: Current bar index (for cooldown)

        Returns:
            {
                "status": "success" | "blocked" | "error",
                "reason": "...",
                "order_id": "..." (if success),
                "filled_qty": int,
                "filled_price": float,
                "slippage_pct": float (if success)
            }
        """
        signal_action = signal.get("signal", "HOLD")

        if signal_action not in ["BUY", "SELL"]:
            logger.debug(f"Ignoring non-actionable signal: {signal_action}")
            return {"status": "blocked", "reason": f"Non-actionable signal: {signal_action}"}

        # 1. RISK CHECK
        can_trade, reason = self.risk.can_trade(
            ticker=ticker,
            signal=signal_action,
            account_equity=account_equity,
            current_positions_count=current_positions_count,
            total_portfolio_positions=total_portfolio_positions,
            current_bar=current_bar
        )

        if not can_trade:
            logger.info(f"Signal BLOCKED for {ticker}: {reason}")
            return {"status": "blocked", "reason": reason}

        # 2. DUPLICATE ORDER CHECK
        recent_order = self._check_recent_order(ticker, signal_action)
        if recent_order:
            logger.warning(
                f"Duplicate order prevented: {ticker} {signal_action} — "
                f"already placed order {recent_order['order_id']} "
                f"at {recent_order['timestamp']}"
            )
            return {
                "status": "blocked",
                "reason": f"Duplicate prevention: order placed {recent_order['age_seconds']:.0f}s ago"
            }

        # 2b. IDEMPOTENCY KEY GENERATION
        idempotency_key = self._generate_idempotency_key(
            ticker,
            signal_action.lower(),
            datetime.now(ET)
        )
        logger.info(f"Using idempotency key: {idempotency_key}")

        # 3. POSITION SIZE CALCULATION
        qty = self.risk.calculate_position_size(
            ticker=ticker,
            signal=signal_action,
            current_price=current_price,
            account_equity=account_equity
        )

        if qty == 0:
            return {
                "status": "blocked",
                "reason": "Position size = 0 (below min or exceeds limit)"
            }

        # 4. DRY RUN CHECK
        if self.dry_run:
            logger.info(
                f"[DRY RUN] Would execute: {signal_action} {qty} {ticker} @ ${current_price:.2f} | "
                f"Reason: {signal.get('reason', 'N/A')}"
            )
            return {
                "status": "success",
                "order_id": f"DRY_RUN_{idempotency_key}",
                "filled_qty": qty,
                "filled_price": current_price,
                "slippage_pct": 0.0
            }

        # 5. ORDER TYPE SELECTION & PLACEMENT WITH RETRY
        order_type = self.config.execution.order_type

        try:
            if order_type == "market":
                result = self._place_with_retry(
                    lambda: self.broker.place_market_order(
                        symbol=ticker,
                        qty=qty,
                        side=signal_action.lower()
                    ),
                    ticker=ticker,
                    max_retries=3
                )
            else:  # limit
                limit_price = self._calculate_limit_price(
                    current_price,
                    signal_action,
                    self.config.execution.limit_offset_pct
                )
                result = self._place_with_retry(
                    lambda: self.broker.place_limit_order(
                        symbol=ticker,
                        qty=qty,
                        side=signal_action.lower(),
                        limit_price=limit_price
                    ),
                    ticker=ticker,
                    max_retries=3
                )

            # Extract order details
            order_id = result.id
            filled_qty = float(result.filled_qty) if result.filled_qty else qty
            filled_price = float(result.filled_avg_price) if result.filled_avg_price else current_price

            # 6. SLIPPAGE CHECK
            expected_cost = current_price * qty
            actual_cost = filled_price * filled_qty
            slippage_pct = abs(actual_cost - expected_cost) / expected_cost * 100

            if slippage_pct > 1.0:  # More than 1% slippage
                logger.warning(
                    f"HIGH SLIPPAGE on {ticker}: expected ${expected_cost:.2f}, "
                    f"actual ${actual_cost:.2f} ({slippage_pct:.2f}%)"
                )
                if self.notifier:
                    self.notifier.send_alert(
                        f"⚠️ High slippage: {ticker} {signal_action} "
                        f"({slippage_pct:.1f}% slippage)"
                    )

            # 7. PARTIAL FILL HANDLING
            if filled_qty < qty:
                logger.warning(
                    f"PARTIAL FILL: {ticker} {signal_action} — "
                    f"requested {qty}, filled {filled_qty}"
                )
                if self.notifier:
                    self.notifier.send_alert(
                        f"📊 Partial fill: {ticker} {filled_qty}/{qty} shares filled"
                    )

            # 8. RECORD ORDER
            self.order_history.append({
                "ticker": ticker,
                "side": signal_action,
                "qty": filled_qty,
                "price": filled_price,
                "order_id": order_id,
                "timestamp": datetime.now(ET),
                "signal_reason": signal.get("reason", "N/A"),
                "bar": current_bar
            })

            logger.info(
                f"ORDER PLACED: {signal_action} {filled_qty} {ticker} "
                f"@ ${filled_price:.2f} (order_id: {order_id})"
            )

            return {
                "status": "success",
                "order_id": order_id,
                "filled_qty": filled_qty,
                "filled_price": filled_price,
                "slippage_pct": slippage_pct
            }

        except Exception as e:
            logger.error(f"ORDER FAILED: {ticker} {signal_action} — {e}", exc_info=True)
            if self.notifier:
                self.notifier.send_error_alert(
                    f"❌ Order failed: {ticker} {signal_action} — {str(e)}"
                )
            return {"status": "error", "reason": str(e)}

    def _place_with_retry(
        self,
        order_func,
        ticker: str,
        max_retries: int = 3
    ):
        """
        Place order with exponential backoff retry.

        Handles specific Alpaca rejection codes:
        - 403 "insufficient buying power" → No retry
        - 422 "market is closed" → No retry (critical error)
        - 403 "symbol not found" → No retry (halt bot)
        - 429 "rate limited" → Retry with exponential backoff
        - Network errors (timeout, connection) → Retry
        - 400 "client_order_id already exists" → Success (idempotency)

        Args:
            order_func: Function that places the order
            ticker: Ticker symbol (for error messages)
            max_retries: Maximum retry attempts

        Returns:
            Order object from broker

        Raises:
            Exception if all retries exhausted or fatal error
        """
        for attempt in range(1, max_retries + 1):
            try:
                return order_func()

            except Exception as e:
                error_str = str(e).lower()

                # CASE 1: Insufficient buying power — DO NOT RETRY
                if "insufficient" in error_str or "buying power" in error_str:
                    logger.error(f"❌ ORDER REJECTED: Insufficient buying power for {ticker}")
                    if self.notifier:
                        self.notifier.send_alert(
                            f"❌ INSUFFICIENT BUYING POWER\n"
                            f"Cannot place order for {ticker}.\n"
                            f"Check Alpaca account equity and position sizing."
                        )
                    raise ValueError("Insufficient buying power")

                # CASE 2: Market closed — DO NOT RETRY (should never happen)
                if "market" in error_str and "closed" in error_str:
                    logger.critical(
                        f"❌ CRITICAL: Order attempted while market closed for {ticker}. "
                        f"is_market_open() check failed!"
                    )
                    if self.notifier:
                        self.notifier.send_alert(
                            f"🚨 CRITICAL BUG: Order attempted while market closed.\n"
                            f"Ticker: {ticker}\n"
                            f"Check is_market_open() logic immediately."
                        )
                    raise RuntimeError("Market closed (bot logic error)")

                # CASE 3: Symbol not found — DO NOT RETRY (config error)
                if "symbol" in error_str and ("not found" in error_str or "invalid" in error_str):
                    logger.critical(
                        f"❌ CRITICAL: Invalid symbol {ticker} in strategy config. "
                        f"Halting bot."
                    )
                    if self.notifier:
                        self.notifier.send_alert(
                            f"🚨 CRITICAL CONFIG ERROR\n"
                            f"Invalid ticker: {ticker}\n"
                            f"Fix strategy config and redeploy.\n"
                            f"⛔ Trading halted."
                        )
                    # Auto-halt trading
                    os.environ["TRADING_PAUSED"] = "true"
                    raise ValueError(f"Invalid symbol: {ticker}")

                # CASE 4: Duplicate client_order_id — Idempotency working (success)
                if "client_order_id" in error_str and "already exists" in error_str:
                    logger.info(
                        f"Idempotency: client_order_id already exists for {ticker}. "
                        f"Order was already placed (likely by previous attempt). Not an error."
                    )
                    # Try to fetch the existing order details from broker
                    # For now, re-raise to caller can handle
                    raise

                # CASE 5: Rate limit — RETRY with backoff
                if "rate" in error_str or "429" in error_str:
                    logger.warning(f"Rate limited on order for {ticker}. Retrying...")
                    if attempt < max_retries:
                        wait_time = (2 ** attempt) * 2  # 4s, 8s, 16s
                        logger.info(f"Waiting {wait_time}s before retry...")
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"Rate limit retries exhausted for {ticker}")
                        raise

                # CASE 6: Network/timeout errors — RETRY
                if "timeout" in error_str or "connection" in error_str:
                    logger.warning(f"Network error on order for {ticker}: {e}")
                    if attempt < max_retries:
                        wait_time = 2 ** attempt  # 2s, 4s, 8s
                        logger.warning(
                            f"Order placement failed (attempt {attempt}/{max_retries}): {e}. "
                            f"Retrying in {wait_time}s..."
                        )
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"All {max_retries} retry attempts exhausted for {ticker}")
                        raise

                # CASE 7: Unknown error — RETRY (generic fallback)
                else:
                    if attempt < max_retries:
                        wait_time = 2 ** attempt  # 2s, 4s, 8s
                        logger.warning(
                            f"Order placement failed (attempt {attempt}/{max_retries}): {e}. "
                            f"Retrying in {wait_time}s..."
                        )
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"All {max_retries} retry attempts exhausted for {ticker}")
                        raise

    def _check_recent_order(self, ticker: str, side: str) -> Optional[Dict[str, Any]]:
        """
        Check if we placed an order for this ticker+side in the last 60s.

        This prevents duplicate orders if:
        - Signal fires multiple times in quick succession
        - Bot restarts mid-execution
        - Network issues cause double-submit

        Args:
            ticker: Ticker symbol
            side: "BUY" or "SELL"

        Returns:
            Dict with order details if recent order found, None otherwise
        """
        now = datetime.now(ET)

        # Check most recent orders first (reversed list)
        for order in reversed(self.order_history):
            if order["ticker"] == ticker and order["side"] == side:
                age_seconds = (now - order["timestamp"]).total_seconds()
                if age_seconds < 60:
                    return {
                        "order_id": order["order_id"],
                        "timestamp": order["timestamp"],
                        "age_seconds": age_seconds
                    }
                else:
                    # Orders are sorted by time, no need to check further
                    break

        return None

    def _generate_idempotency_key(
        self,
        ticker: str,
        side: str,
        signal_timestamp: datetime
    ) -> str:
        """
        Generate idempotency key for order.

        Format: {ticker}_{side}_{YYYYMMDD}_{HHMMSS}
        Example: AAPL_buy_20260305_093500

        Prevents duplicate orders if the bot restarts mid-signal-check.
        Pass as client_order_id to Alpaca's place_*_order().

        Alpaca will reject a duplicate client_order_id within the same
        trading day, making this idempotent across restarts.

        Args:
            ticker: Ticker symbol
            side: "buy" or "sell"
            signal_timestamp: Timestamp of signal generation

        Returns:
            Idempotency key string
        """
        return f"{ticker}_{side}_{signal_timestamp.strftime('%Y%m%d_%H%M%S')}"

    def _calculate_limit_price(
        self,
        current_price: float,
        side: str,
        offset_pct: float
    ) -> float:
        """
        Calculate limit price with offset.

        For BUY: limit slightly above current price (willing to pay more)
        For SELL: limit slightly below current price (willing to accept less)

        Args:
            current_price: Current market price
            side: "BUY" or "SELL"
            offset_pct: Offset percentage (e.g., 0.1 for 0.1%)

        Returns:
            Limit price
        """
        if side.upper() == "BUY":
            return current_price * (1 + offset_pct / 100)
        else:
            return current_price * (1 - offset_pct / 100)

    def check_exits(
        self,
        positions: List[Dict[str, Any]],
        current_prices: Dict[str, float]
    ) -> List[Dict[str, Any]]:
        """
        Check all open positions for exit conditions.

        Checks:
        - Stop loss
        - Take profit
        - Trailing stop (if enabled)

        Args:
            positions: List of position dicts with keys:
                       ticker, avg_entry, side, highest_since_entry (optional)
            current_prices: Dict of {ticker: current_price}

        Returns:
            List of exit signals: [
                {"ticker": "AAPL", "reason": "Stop loss hit", "current_price": 180.5},
                ...
            ]
        """
        exits = []

        for pos in positions:
            ticker = pos["ticker"]
            current_price = current_prices.get(ticker)

            if current_price is None:
                logger.warning(f"No current price for {ticker}, skipping exit check")
                continue

            # Stop loss check
            if self.risk.check_stop_loss(
                entry_price=pos["avg_entry"],
                current_price=current_price,
                side=pos["side"]
            ):
                exits.append({
                    "ticker": ticker,
                    "reason": (
                        f"Stop loss hit (entry ${pos['avg_entry']:.2f}, "
                        f"now ${current_price:.2f})"
                    ),
                    "current_price": current_price
                })
                continue

            # Take profit check
            if self.risk.check_take_profit(
                entry_price=pos["avg_entry"],
                current_price=current_price,
                side=pos["side"]
            ):
                exits.append({
                    "ticker": ticker,
                    "reason": (
                        f"Take profit hit (entry ${pos['avg_entry']:.2f}, "
                        f"now ${current_price:.2f})"
                    ),
                    "current_price": current_price
                })
                continue

            # Trailing stop check (if enabled)
            if self.risk.risk_config.trailing_stop_enabled:
                highest = pos.get("highest_since_entry", pos["avg_entry"])
                if self.risk.check_trailing_stop(
                    entry_price=pos["avg_entry"],
                    highest_since_entry=highest,
                    current_price=current_price,
                    side=pos["side"]
                ):
                    exits.append({
                        "ticker": ticker,
                        "reason": (
                            f"Trailing stop hit (high ${highest:.2f}, "
                            f"now ${current_price:.2f})"
                        ),
                        "current_price": current_price
                    })

        return exits

    def close_position(
        self,
        ticker: str,
        reason: str
    ) -> Dict[str, Any]:
        """
        Close an entire position.

        Args:
            ticker: Ticker symbol
            reason: Reason for closing (for logging)

        Returns:
            Dict with status and details
        """
        try:
            if self.dry_run:
                logger.info(f"[DRY RUN] Would close position: {ticker} | Reason: {reason}")
                return {"status": "success", "reason": "Dry run"}

            # Use broker's close_position method
            order = self.broker.close_position(ticker)

            logger.info(
                f"Position closed: {ticker} | "
                f"OrderID: {order.id} | "
                f"Reason: {reason}"
            )

            return {
                "status": "success",
                "order_id": order.id,
                "reason": reason
            }

        except Exception as e:
            logger.error(f"Failed to close position {ticker}: {e}", exc_info=True)
            if self.notifier:
                self.notifier.send_error_alert(
                    f"❌ Failed to close position: {ticker} — {str(e)}"
                )
            return {"status": "error", "reason": str(e)}

    def get_order_history(self) -> List[Dict[str, Any]]:
        """
        Get order history for today.

        Returns:
            List of order dicts
        """
        return self.order_history.copy()

    def reset_daily(self) -> None:
        """
        Reset daily tracking.

        Call at start of each trading day.
        """
        logger.info(f"OrderManager daily reset | Orders today: {len(self.order_history)}")
        self.order_history = []
        self.pending_orders = {}
