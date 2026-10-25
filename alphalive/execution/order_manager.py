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
import logging
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError as AlpacaAPIError

from alphalive.broker.base_broker import BaseBroker, OrderError
from alphalive.broker.transport import BrokerMutationBlocked
from alphalive.execution.risk_manager import RiskManager
from alphalive.strategy_schema import StrategySchema
from alphalive.utils.retry import RetryDecision, RetryOutcome, retry_with_backoff
from alphalive.state import (
    INTENT_ABANDONED,
    INTENT_CANCELLED,
    INTENT_EXPIRED,
    INTENT_FILLED,
    INTENT_PARTIALLY_FILLED,
    INTENT_PREPARED,
    INTENT_RECONCILED,
    INTENT_REJECTED,
    INTENT_SUBMITTED,
    INTENT_SUBMITTING,
    INTENT_UNCERTAIN,
)

logger = logging.getLogger(__name__)

# Maps an Alpaca order status string to a submission-intent status. Unknown
# strings are deliberately NOT in this table - see _map_broker_order_status.
_BROKER_STATUS_TO_INTENT_STATUS = {
    "filled": INTENT_FILLED,
    "partially_filled": INTENT_PARTIALLY_FILLED,
    "rejected": INTENT_REJECTED,
    "canceled": INTENT_CANCELLED,
    "cancelled": INTENT_CANCELLED,
    "expired": INTENT_EXPIRED,
    "new": INTENT_SUBMITTED,
    "accepted": INTENT_SUBMITTED,
    "pending_new": INTENT_SUBMITTED,
    "accepted_for_bidding": INTENT_SUBMITTED,
    "held": INTENT_SUBMITTED,
    "pending_cancel": INTENT_SUBMITTED,
}


def _map_broker_order_status(status: Optional[str]) -> Optional[str]:
    """Map a broker order status string to an intent status, or None if the
    status string isn't recognized (caller should quarantine rather than
    guess)."""
    if status is None:
        return None
    return _BROKER_STATUS_TO_INTENT_STATUS.get(status.lower())


# Quantity tolerance for close-order reconciliation (2026-09-11 pass 4):
# matches the repository's established drift-tolerance convention (see
# main.py's ledger-vs-broker qty-mismatch check, `abs(diff) > 1e-6`), kept
# as the same numeric value rather than a newly-invented threshold, but
# applied via Decimal rather than raw float subtraction to avoid binary
# floating-point artifacts when comparing share quantities.
QTY_TOLERANCE = Decimal("0.000001")


def _to_decimal(value: Any) -> Decimal:
    """Best-effort Decimal conversion for a broker-reported quantity.
    Converts via str() (never via float() directly) to avoid importing a
    float's own binary-representation error into the Decimal. None or an
    unparseable value becomes Decimal("0") - the safe (never-negative,
    never-poisons-a-comparison) default for a missing quantity."""
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _qty_equal(a: Any, b: Any, tolerance: Decimal = QTY_TOLERANCE) -> bool:
    """True if two quantities are equal within QTY_TOLERANCE, Decimal-safe."""
    return abs(_to_decimal(a) - _to_decimal(b)) <= tolerance


# Eastern Time (US stock market timezone)
ET = ZoneInfo("America/New_York")


def _unknown_order_grace_seconds() -> float:
    """How long an intent that MAY have reached the broker (timeout,
    lost response) may stay unresolved while the broker reports no order
    for its client_order_id before it is abandoned. Default 15 minutes -
    far longer than any acknowledgement delay; override with
    ALPHALIVE_UNKNOWN_ORDER_GRACE_SECONDS (used by the simulator scenarios)."""
    try:
        return float(os.environ.get("ALPHALIVE_UNKNOWN_ORDER_GRACE_SECONDS", "900"))
    except ValueError:
        return 900.0


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
        dry_run: bool = False,
        state=None,
    ):
        """
        Initialize order manager.

        Args:
            broker: Broker instance for order execution
            risk_manager: Risk manager for limit checks
            config: Strategy configuration
            notifier: Telegram notifier (optional)
            dry_run: If True, log orders without executing
            state: BotState instance for durable submission-intent tracking
                (see execute_signal). Optional for backward compatibility
                (tests, replay mode) - when None, falls back to the old
                in-process-only idempotency key with no restart recovery.
        """
        self.broker = broker
        self.risk = risk_manager
        self.config = config
        self.notifier = notifier
        self.dry_run = dry_run
        self.state = state

        # Order tracking
        # Broker-confirmed fill deltas booked by this manager (for logs/tests).
        self.fill_events: List[Dict[str, Any]] = []
        self.order_history: List[Dict[str, Any]] = []  # All orders placed today
        self.pending_orders: Dict[str, str] = {}  # {ticker: order_id}
        # O(1) duplicate-check index: (ticker, side) → most recent order record
        self._recent_order_index: Dict[Tuple[str, str], Dict[str, Any]] = {}

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
        current_bar: Optional[int] = None,
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
            return {
                "status": "blocked",
                "reason": f"Non-actionable signal: {signal_action}",
            }

        # 1. RISK CHECK
        can_trade, reason = self.risk.can_trade(
            ticker=ticker,
            signal=signal_action,
            account_equity=account_equity,
            current_positions_count=current_positions_count,
            total_portfolio_positions=total_portfolio_positions,
            current_bar=current_bar,
        )

        if not can_trade:
            logger.info(f"Signal BLOCKED for {ticker}: {reason}")
            return {"status": "blocked", "reason": reason}

        # 2. DUPLICATE ORDER CHECK (same-process, same-decision burst guard)
        recent_order = self._check_recent_order(ticker, signal_action)
        if recent_order:
            logger.warning(
                f"Duplicate order prevented: {ticker} {signal_action} - "
                f"already placed order {recent_order['order_id']} "
                f"at {recent_order['timestamp']}"
            )
            return {
                "status": "blocked",
                "reason": f"Duplicate prevention: order placed {recent_order['age_seconds']:.0f}s ago",
            }

        # 3. IN-FLIGHT CHECK. Bring every open intent for this ticker up to
        # date with the broker first (applying any fills that happened since
        # the last sync). An intent still open afterwards is a genuinely
        # live or unresolved order - never a reason to report a NEW decision
        # as "already done" (the pre-2026-10 behaviour, which silently
        # swallowed a later legitimate BUY by returning an old filled
        # order as a "recovered" success).
        if self.state is not None and not self.dry_run:
            self.sync_open_intents()
            still_open = [
                i for i in self.state.list_open_intents() if i["ticker"] == ticker
            ]
            if still_open:
                desc = ", ".join(
                    f"{i['side']} {i['intent_id'][:8]} ({i['status']})"
                    for i in still_open
                )
                reason = (
                    f"Order in flight/unresolved for {ticker}: {desc} - no new "
                    f"{signal_action} until it resolves"
                )
                logger.warning(reason)
                return {"status": "blocked", "reason": reason}

        # 4. POSITION + SIZE. SELL sells exactly the broker-confirmed held
        # quantity (never a computed size - that could open a short). BUY is
        # sized against the CUMULATIVE cap: existing position value counts.
        try:
            position = self.broker.get_position(ticker)
        except Exception as e:
            logger.error(f"Could not fetch position for {signal_action} {ticker}: {e}")
            return {"status": "error", "reason": f"Position lookup failed: {e}"}
        held_qty = float(position.qty) if position is not None else 0.0

        if signal_action == "SELL":
            if held_qty <= 0:
                logger.info(
                    f"SELL signal for {ticker} with no open position - skipping "
                    f"(would open a short)"
                )
                return {
                    "status": "blocked",
                    "reason": "SELL signal with no open position",
                }
            qty = held_qty
        else:
            qty = self.risk.calculate_position_size(
                ticker=ticker,
                signal=signal_action,
                current_price=current_price,
                account_equity=account_equity,
                existing_position_value=max(held_qty, 0.0) * current_price,
            )

        if qty == 0:
            return {
                "status": "blocked",
                "reason": "Position size = 0 (below min or cumulative cap reached)",
            }

        bar_ts = None
        if (
            current_bar is not None
            and hasattr(current_bar, "__len__")
            and len(current_bar)
        ):
            bar_ts = current_bar[-1]

        # 5. DRY RUN CHECK - no broker call, no intent, no ledger change.
        if self.dry_run:
            dry_run_key = self._generate_idempotency_key(
                ticker, signal_action.lower(), datetime.now(ET)
            )
            logger.info(
                f"[DRY RUN] Would execute: {signal_action} {qty} {ticker} @ ${current_price:.2f} | "
                f"Reason: {signal.get('reason', 'N/A')}"
            )
            return {
                "status": "success",
                "order_status": "dry_run",
                "order_id": f"DRY_RUN_{dry_run_key}",
                "requested_qty": qty,
                "filled_qty": qty,
                "filled_price": current_price,
                "slippage_pct": 0.0,
            }

        # 6. SUBMISSION INTENT: durably recorded BEFORE any broker call, then
        # moved to "submitting" (also persisted) immediately before sending.
        # "prepared" on disk therefore proves the order was never sent.
        intent = None
        if self.state is not None:
            intent = self.state.create_submission_intent(
                ticker=ticker,
                side=signal_action,
                qty=qty,
                order_type=self.config.execution.order_type,
                strategy_name=self.config.strategy.name,
                decision_price=current_price,
                decision_bar_ts=(str(bar_ts) if bar_ts is not None else None),
                signal_reason=signal.get("reason"),
            )
            client_order_id = intent["client_order_id"]
            self.state.update_submission_intent(
                intent["intent_id"], status=INTENT_SUBMITTING
            )
        else:
            # No durable state wired (tests, replay mode) - best-effort
            # timestamp-based key only; no restart recovery possible.
            client_order_id = self._generate_idempotency_key(
                ticker, signal_action.lower(), datetime.now(ET)
            )

        # Counts toward max_trades_per_day and starts the cooldown - for
        # every order actually attempted, BUY or SELL.
        self.risk.record_order_submitted(ticker, bar_ts)
        logger.info(f"Using client_order_id: {client_order_id}")

        # 7. ORDER TYPE SELECTION & PLACEMENT WITH RETRY
        order_type = self.config.execution.order_type
        # Set once the broker has returned the order. An exception after this
        # point is a local bookkeeping/notification failure, never a broker
        # rejection: the order is live and must stay open for reconciliation.
        accepted_order_id = None

        try:
            if order_type == "market":
                result = self._place_with_retry(
                    lambda: self.broker.place_market_order(
                        symbol=ticker,
                        qty=qty,
                        side=signal_action.lower(),
                        client_order_id=client_order_id,
                    ),
                    ticker=ticker,
                    max_retries=3,
                    client_order_id=client_order_id,
                )
            else:  # limit
                limit_price = self._calculate_limit_price(
                    current_price, signal_action, self.config.execution.limit_offset_pct
                )
                result = self._place_with_retry(
                    lambda: self.broker.place_limit_order(
                        symbol=ticker,
                        qty=qty,
                        side=signal_action.lower(),
                        limit_price=limit_price,
                        client_order_id=client_order_id,
                    ),
                    ticker=ticker,
                    max_retries=3,
                    client_order_id=client_order_id,
                )

            accepted_order_id = result.id
            self.risk.record_api_call(f"place_{order_type}_order")

            order_id = result.id
            raw_status = getattr(result, "status", None)
            mapped = _map_broker_order_status(raw_status)

            # Fill state comes ONLY from the broker: cumulative filled_qty and
            # filled_avg_price as reported. An "accepted"/"new" response has
            # filled_qty 0 and creates no position.
            if self.state is not None and intent is not None:
                self._apply_order_progress(intent, result)
                if mapped is None:
                    self.state.update_submission_intent(
                        intent["intent_id"],
                        status=INTENT_UNCERTAIN,
                        broker_order_id=order_id,
                        last_error=f"unrecognized broker status: {raw_status}",
                    )
                else:
                    self.state.update_submission_intent(
                        intent["intent_id"], status=mapped, broker_order_id=order_id
                    )

            confirmed_qty = float(result.filled_qty) if result.filled_qty else 0.0
            confirmed_price = (
                float(result.filled_avg_price) if result.filled_avg_price else None
            )

            slippage_pct = None
            if confirmed_qty > 0 and confirmed_price:
                slippage_pct = (
                    abs(confirmed_price - current_price) / current_price * 100
                )
                if slippage_pct > 1.0:
                    logger.warning(
                        f"HIGH SLIPPAGE on {ticker}: decision ${current_price:.2f}, "
                        f"fill ${confirmed_price:.2f} ({slippage_pct:.2f}%)"
                    )
                    if self.notifier:
                        self.notifier.send_alert(
                            f"⚠️ High slippage: {ticker} {signal_action} "
                            f"({slippage_pct:.1f}% slippage)"
                        )

            if mapped == INTENT_PARTIALLY_FILLED or (0 < confirmed_qty < qty):
                logger.warning(
                    f"PARTIAL FILL: {ticker} {signal_action} - requested {qty}, "
                    f"broker-confirmed {confirmed_qty} so far"
                )
                if self.notifier:
                    self.notifier.send_alert(
                        f"📊 Partial fill: {ticker} {confirmed_qty}/{qty} shares filled"
                    )

            _order_record = {
                "ticker": ticker,
                "side": signal_action,
                "qty": qty,
                "filled_qty": confirmed_qty,
                "price": confirmed_price,
                "order_id": order_id,
                "timestamp": datetime.now(ET),
                "signal_reason": signal.get("reason", "N/A"),
                "bar": str(bar_ts) if bar_ts is not None else None,
                "status": mapped or "uncertain",
            }
            self.order_history.append(_order_record)
            self._recent_order_index[(ticker, signal_action)] = _order_record

            logger.info(
                f"ORDER SUBMITTED: {signal_action} {qty} {ticker} (order_id: {order_id}) "
                f"| broker status={raw_status} | confirmed filled {confirmed_qty}"
            )

            return {
                "status": "success",
                "order_status": mapped or "uncertain",
                "order_id": order_id,
                "requested_qty": qty,
                "filled_qty": confirmed_qty,
                "filled_price": confirmed_price,
                "slippage_pct": slippage_pct,
            }

        except Exception as e:
            if (
                accepted_order_id is not None
                and self.state is not None
                and intent is not None
            ):
                current = self.state.get_submission_intent(intent["intent_id"]) or {}
                fields = {"broker_order_id": accepted_order_id, "last_error": str(e)}
                if current.get("status") in (INTENT_PREPARED, INTENT_SUBMITTING):
                    fields["status"] = INTENT_SUBMITTED
                self.state.update_submission_intent(intent["intent_id"], **fields)
                logger.error(
                    f"Post-submission error for {ticker} {signal_action} (order "
                    f"{accepted_order_id} is live; left open for reconciliation): {e}",
                    exc_info=True,
                )
                return {
                    "status": "success",
                    "order_status": fields.get("status", current.get("status")),
                    "order_id": accepted_order_id,
                    "requested_qty": qty,
                    "reason": f"post-submission error: {e}",
                }
            if self.state is not None and intent is not None:
                if self._is_definite_rejection(e):
                    self.state.update_submission_intent(
                        intent["intent_id"],
                        status=INTENT_REJECTED,
                        last_error=str(e),
                    )
                    logger.error(
                        f"ORDER FAILED (rejected): {ticker} {signal_action} - {e}"
                    )
                else:
                    # Retries exhausted on a timeout/5xx/429/network error, or a
                    # 409 whose original order couldn't be recovered - the
                    # broker's true state for this client_order_id is unknown.
                    # Quarantine; sync_open_intents() resolves it from the
                    # broker (or abandons it after the grace period if the
                    # broker never shows the order).
                    self.state.update_submission_intent(
                        intent["intent_id"],
                        status=INTENT_UNCERTAIN,
                        last_error=str(e),
                    )
                    logger.error(
                        f"ORDER OUTCOME UNCERTAIN (quarantined): {ticker} "
                        f"{signal_action} - {e}",
                        exc_info=True,
                    )
            else:
                logger.error(
                    f"ORDER FAILED: {ticker} {signal_action} - {e}", exc_info=True
                )
            if self.notifier:
                self.notifier.send_error_alert(
                    f"❌ Order failed: {ticker} {signal_action} - {str(e)}"
                )
            return {"status": "error", "reason": str(e)}

    # ------------------------------------------------------------------
    # Broker-confirmed fill lifecycle (2026-10 repair)
    # ------------------------------------------------------------------

    def _apply_order_progress(self, intent: Dict[str, Any], order) -> Optional[Dict]:
        """Apply any NEW broker-confirmed fill on `order` to the ledger and
        risk counters exactly once (see BotState.apply_intent_fill_progress).
        Every path that observes a broker order - submit response, periodic
        sync, startup reconciliation, close reconciliation - goes through
        here, so a fill is booked once no matter how often it is seen."""
        if self.state is None or order is None:
            return None
        filled = getattr(order, "filled_qty", None)
        price = getattr(order, "filled_avg_price", None)
        if not filled or not price:
            return None
        fill = self.state.apply_intent_fill_progress(
            intent["intent_id"],
            filled,
            price,
            commission=float(self.config.risk.commission_per_trade or 0.0),
        )
        if fill is None:
            return None
        self.risk.record_fill_result(fill)
        self.state.add_daily_pnl(fill["realized_pnl"])
        # Global stats count completed round trips, not individual fill reports.
        if self.risk.global_risk is not None and fill.get("round_trip_closed"):
            self.risk.global_risk.record_trade(
                strategy_name=self.config.strategy.name,
                pnl=fill.get("round_trip_pnl") or 0.0,
            )
        ticker = fill["ticker"]
        if fill["side"] == "BUY":
            if ticker not in self.state.state.get("entry_timestamps", {}):
                self.state.record_entry(ticker)
        elif fill["round_trip_closed"]:
            self.state.clear_position_high(ticker)
            self.state.clear_entry_timestamp(ticker)
        self.fill_events.append(fill)
        if self.notifier:
            try:
                self.notifier.send_trade_notification(
                    ticker=ticker,
                    side=fill["side"] if fill["side"] != "CLOSE" else "SELL",
                    qty=fill["delta_qty"],
                    price=fill["delta_price"],
                    reason=f"broker-confirmed fill (intent {intent['intent_id'][:8]})",
                )
            except Exception as e:  # notifications must never break booking
                logger.warning(f"Fill notification failed: {e}")
        return fill

    def _settle_working_orders_before_close(
        self, ticker: str, reason: str
    ) -> Optional[Dict[str, Any]]:
        """A protective close must not race a strategy order that is still
        working at the broker. A working SELL already reduces the position,
        so no second sell is sent. A working entry BUY is cancelled first
        (its partial fill is kept and booked); closing while it can still
        fill would leave the CLOSE "filled but not flat" and quarantine the
        ticker. Returns a "pending" result to retry at the next exit check,
        or None when the close may proceed. Quarantined (uncertain) intents
        have no confirmed live order and do not hold the close back."""
        live = (INTENT_SUBMITTED, INTENT_PARTIALLY_FILLED)

        def working(side: str) -> Optional[Dict[str, Any]]:
            intent = self.state.get_open_intent(ticker, side)
            if intent is None or intent["status"] not in live:
                return None
            self._reconcile_intent(intent)
            intent = self.state.get_open_intent(ticker, side)
            return intent if intent and intent["status"] in live else None

        sell = working("SELL")
        if sell is not None:
            logger.info(
                f"Close for {ticker} deferred: strategy SELL "
                f"{sell.get('broker_order_id')} is still working ({reason})"
            )
            return {
                "status": "pending",
                "order_id": sell.get("broker_order_id"),
                "reason": f"{reason} (strategy SELL already working)",
                "filled_price": None,
                "filled_qty": None,
                "fill_status": sell["status"],
            }
        if working("BUY") is not None:
            self.cancel_open_entry_orders(f"protective close: {reason}")
            buy = working("BUY")
            if buy is not None:
                logger.warning(
                    f"Close for {ticker} deferred until entry order "
                    f"{buy.get('broker_order_id')} is cancelled ({reason})"
                )
                return {
                    "status": "pending",
                    "order_id": buy.get("broker_order_id"),
                    "reason": f"{reason} (waiting for entry order to cancel)",
                    "filled_price": None,
                    "filled_qty": None,
                    "fill_status": buy["status"],
                }
        return None

    def cancel_open_entry_orders(self, reason: str) -> int:
        """Cancel BUY orders still working at the broker (pause policy: a
        paused bot takes on no new exposure). Exit/close orders are never
        cancelled here. Any partial fill already executed is kept and
        booked by the following reconciliation. Returns orders cancelled."""
        if self.state is None or self.dry_run:
            return 0
        n = 0
        for intent in self.state.list_open_intents():
            if intent["ticker"] != self.config.ticker or intent["side"] != "BUY":
                continue
            if intent["status"] not in (INTENT_SUBMITTED, INTENT_PARTIALLY_FILLED):
                continue
            order_id = intent.get("broker_order_id")
            if not order_id:
                continue
            try:
                self.broker.cancel_order(str(order_id))
                n += 1
                logger.warning(
                    f"Cancelled open entry order {order_id} for {intent['ticker']} "
                    f"({reason})"
                )
            except Exception as e:
                logger.error(f"Could not cancel entry order {order_id}: {e}")
            self._reconcile_intent(
                self.state.get_submission_intent(intent["intent_id"])
            )
        return n

    def sync_open_intents(self) -> List[Dict[str, Any]]:
        """Reconcile every open intent for this manager's ticker against the
        broker: book new fills, move finished orders to their terminal
        status, abandon never-sent/never-seen ones. Called every main-loop
        iteration, at startup, and before any new order decision. Returns
        the per-intent reconciliation results (for logs/tests)."""
        if self.state is None or self.dry_run:
            return []
        results = []
        for intent in self.state.list_open_intents():
            if intent["ticker"] != self.config.ticker:
                continue
            try:
                if intent["side"] == "CLOSE":
                    res = self._reconcile_open_close_intent(intent)
                else:
                    res = self._reconcile_intent(intent)
            except Exception as e:
                logger.error(f"Intent sync failed for {intent['intent_id']}: {e}")
                continue
            results.append({"intent_id": intent["intent_id"], **res})
        return results

    def _is_definite_rejection(self, e: Exception) -> bool:
        """True if `e` represents a definite pre-acceptance rejection (Alpaca
        never created an order for this client_order_id) rather than a
        network/5xx/429/ambiguous-409 case where the broker's true state is
        unknown.

        ValueError/RuntimeError are raised explicitly inside
        _place_with_retry's classify() for 403 (insufficient buying power)
        and the two guarded 422 sub-cases (market closed, invalid symbol) -
        all pre-acceptance rejections. A 422 that reaches here as an
        AlpacaAPIError/OrderError (the "other 422" FATAL branch: bad
        qty/params) is also a definite rejection. Everything else
        (unresolved 409, 5xx/429 after retries exhausted, bare network
        errors) is ambiguous.
        """
        if isinstance(e, (ValueError, RuntimeError)):
            return True
        if isinstance(e, (AlpacaAPIError, OrderError)):
            return e.status_code == 422
        return False

    def _reconcile_intent(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        """Reconcile one non-terminal submission intent against the broker.

        Books any new broker-confirmed fill (once - see
        _apply_order_progress) BEFORE updating the intent's status, so a
        terminal status can never be recorded without its fills.

        Returns a dict with an "action" key:
            "filled"     - broker reports the order fully filled (terminal).
            "in_flight"  - order live at the broker (new/accepted/partially
                           filled); "order" holds it.
            "terminal"   - rejected/cancelled/expired (any partial fill
                           before that has been booked); "order" holds it.
            "abandoned"  - the order never reached the broker ("prepared"
                           at reconciliation time), or the broker still has
                           no record of it after the uncertainty grace
                           period. Closed out - never resubmitted, because
                           its decision context no longer exists.
            "quarantine" - outcome unknown (lookup failed, not-found within
                           the grace period, unrecognized status). No new
                           order for this ticker while quarantined.
        """
        intent_id = intent["intent_id"]
        client_order_id = intent["client_order_id"]

        try:
            broker_order = self.broker.get_order_by_client_id(client_order_id)
        except Exception as e:
            logger.warning(
                f"Reconciliation: broker lookup failed for intent {intent_id} "
                f"({client_order_id}): {e}. Quarantining - not safe to retry "
                f"or treat as failed."
            )
            if intent["status"] != INTENT_UNCERTAIN:
                self.state.update_submission_intent(
                    intent_id, status=INTENT_UNCERTAIN, last_error=str(e)
                )
            return {"action": "quarantine", "detail": f"broker lookup unavailable: {e}"}

        if broker_order is None:
            if intent["status"] == INTENT_PREPARED:
                # Persisted before "submitting" was written, so it was never
                # sent. Abandon - a stale decision must not be replayed.
                self.state.update_submission_intent(
                    intent_id,
                    status=INTENT_ABANDONED,
                    last_error="never submitted (prepared at reconciliation)",
                )
                return {"action": "abandoned", "detail": "prepared, never submitted"}

            now = datetime.now(ET)
            first_missing = intent.get("not_found_since")
            if first_missing is None:
                self.state.update_submission_intent(
                    intent_id, status=INTENT_UNCERTAIN, not_found_since=now.isoformat()
                )
                first_missing = now.isoformat()
            age = (now - datetime.fromisoformat(first_missing)).total_seconds()
            grace = _unknown_order_grace_seconds()
            if age >= grace:
                msg = (
                    f"Intent {intent_id} ({intent['ticker']} {intent['side']}): broker "
                    f"has had no order for client_order_id {client_order_id} for "
                    f"{age:.0f}s (grace {grace:.0f}s) - abandoning. Nothing was "
                    f"booked for it."
                )
                logger.warning(msg)
                self.state.update_submission_intent(
                    intent_id, status=INTENT_ABANDONED, last_error=msg
                )
                if self.notifier:
                    self.notifier.send_alert(f"⚠️ {msg}")
                return {"action": "abandoned", "detail": "not found after grace period"}
            logger.warning(
                f"Reconciliation: intent {intent_id} status={intent['status']} but "
                f"broker has no order for {client_order_id} yet ({age:.0f}s of "
                f"{grace:.0f}s grace). Quarantined."
            )
            return {
                "action": "quarantine",
                "detail": "broker has no record of a submitted order (within grace)",
            }

        mapped = _map_broker_order_status(getattr(broker_order, "status", None))
        if mapped is None:
            logger.warning(
                f"Reconciliation: intent {intent_id} has unrecognized broker "
                f"status '{broker_order.status}'. Quarantining."
            )
            self._apply_order_progress(intent, broker_order)
            self.state.update_submission_intent(
                intent_id, status=INTENT_UNCERTAIN, broker_order_id=broker_order.id
            )
            return {
                "action": "quarantine",
                "detail": f"unrecognized broker status: {broker_order.status}",
            }

        self._apply_order_progress(intent, broker_order)
        self.state.update_submission_intent(
            intent_id, status=mapped, broker_order_id=broker_order.id
        )

        if mapped == INTENT_FILLED:
            return {"action": "filled", "order": broker_order, "detail": mapped}
        if mapped in (INTENT_PARTIALLY_FILLED, INTENT_SUBMITTED):
            return {"action": "in_flight", "order": broker_order, "detail": mapped}
        return {"action": "terminal", "order": broker_order, "detail": mapped}

    def _place_with_retry(
        self,
        order_func,
        ticker: str,
        max_retries: int = 3,
        client_order_id: Optional[str] = None,
    ):
        """
        Place order with exponential backoff retry.

        Routes on HTTP status code (AlpacaAPIError.status_code, or
        OrderError.status_code when the broker has wrapped the original
        APIError - see AlpacaBroker._execute()), not error strings:
        - 403 Forbidden          → no retry (insufficient buying power)
        - 409 Conflict           → duplicate client_order_id: a previous attempt
                                   already placed this order. Recover it via
                                   get_order_by_client_id and return it as
                                   success - this is what makes retries after
                                   ambiguous failures genuinely idempotent.
        - 422 Unprocessable      → no retry; sub-case by message (market closed / bad symbol)
        - 429 Too Many Requests  → retry with 4s/8s/16s backoff
        - 5xx / unknown 4xx      → retry with 2s/4s/8s backoff
        - Non-API exceptions     → retry with 2s/4s/8s backoff (network, timeout)

        Args:
            order_func: Zero-argument callable that places the order.
            ticker: Ticker symbol (for error messages).
            max_retries: Maximum retry attempts.
            client_order_id: Idempotency key the order was placed with -
                used to recover the existing order on a 409.

        Returns:
            Order object from broker.

        Raises:
            Exception if all retries exhausted or a fatal (non-retryable) error occurs.
        """
        # Independent attempt counter, incremented once per classify() call.
        # classify() is invoked exactly once per failed attempt inside
        # retry_with_backoff's loop, in order, so this stays in lockstep
        # with that loop's own attempt number - needed here (unlike the
        # other three retry call sites) because the 429 and generic-backoff
        # delays are computed from the attempt number itself, not just a
        # running "delay *= multiplier" state.
        attempt_state = {"n": 0}

        def _classify(e: Exception) -> RetryOutcome:
            attempt_state["n"] += 1
            attempt = attempt_state["n"]
            is_last = attempt >= max_retries

            # Refused by the read-only transport: definitely never sent.
            if isinstance(e, BrokerMutationBlocked):
                return RetryOutcome(RetryDecision.FATAL)

            if isinstance(e, (AlpacaAPIError, OrderError)):
                status = e.status_code

                if status is None:
                    # OrderError with no status_code (e.g. a non-APIError
                    # failure the broker wrapped, like a network error) -
                    # not one of the routable HTTP-status cases below, so
                    # treat it like any other non-API exception: retry with
                    # backoff rather than falling into the 403/409/422/429
                    # branches with a None status.
                    if is_last:
                        logger.error(
                            f"All {max_retries} retry attempts exhausted for {ticker}"
                        )
                        return RetryOutcome(RetryDecision.RETRY)
                    wait_time = 2**attempt
                    return RetryOutcome(
                        RetryDecision.RETRY,
                        delay_override=wait_time,
                        log_message=(
                            f"Non-API broker error on {ticker} "
                            f"(attempt {attempt}/{max_retries}): {e}. "
                            f"Retrying in {wait_time}s..."
                        ),
                    )

                if status == 403:
                    logger.error(
                        f"ORDER REJECTED (403): Insufficient buying power for {ticker}"
                    )
                    if self.notifier:
                        self.notifier.send_alert(
                            f"❌ INSUFFICIENT BUYING POWER\n"
                            f"Cannot place order for {ticker}.\n"
                            f"Check Alpaca account equity and position sizing."
                        )
                    raise ValueError("Insufficient buying power")

                if status == 409:
                    # Idempotency recovery needs to *return* the existing
                    # order rather than raise, which classify() can't do -
                    # mark FATAL so retry_with_backoff re-raises the
                    # original exception immediately (no retry), and the
                    # outer except block below performs the recovery.
                    return RetryOutcome(RetryDecision.FATAL)

                if status == 422:
                    # Two distinct 422 sub-cases share this status code; minimal string
                    # check is the only way to distinguish them.
                    error_str = str(e).lower()
                    if "closed" in error_str:
                        logger.critical(
                            f"CRITICAL: Order attempted while market closed for {ticker}. "
                            f"is_market_open() check failed!"
                        )
                        if self.notifier:
                            self.notifier.send_alert(
                                f"🚨 CRITICAL BUG: Order attempted while market closed.\n"
                                f"Ticker: {ticker}\n"
                                f"Check is_market_open() logic immediately."
                            )
                        raise RuntimeError("Market closed (bot logic error)")
                    if "symbol" in error_str or "unknown" in error_str:
                        logger.critical(
                            f"CRITICAL: Invalid symbol {ticker} in strategy config. "
                            f"Halting bot."
                        )
                        if self.notifier:
                            self.notifier.send_alert(
                                f"🚨 CRITICAL CONFIG ERROR\n"
                                f"Invalid ticker: {ticker}\n"
                                f"Fix strategy config and redeploy.\n"
                                f"⛔ Trading halted."
                            )
                        os.environ["TRADING_PAUSED"] = "true"
                        raise ValueError(f"Invalid symbol: {ticker}")
                    # Other 422 (invalid quantity, bad params) - no retry
                    logger.error(f"ORDER REJECTED (422): {ticker} - {e}")
                    return RetryOutcome(RetryDecision.FATAL)

                if status == 429:
                    if is_last:
                        logger.error(f"Rate limit retries exhausted for {ticker}")
                        return RetryOutcome(RetryDecision.RETRY)
                    wait_time = (2**attempt) * 2  # 4s, 8s, 16s
                    return RetryOutcome(
                        RetryDecision.RETRY,
                        delay_override=wait_time,
                        log_message=(
                            f"Rate limited on order for {ticker}. "
                            f"Retrying in {wait_time}s..."
                        ),
                    )

                # Any other API error (5xx, unknown 4xx) - retry with backoff
                if is_last:
                    logger.error(
                        f"All {max_retries} retry attempts exhausted for {ticker}"
                    )
                    return RetryOutcome(RetryDecision.RETRY)
                wait_time = 2**attempt  # 2s, 4s, 8s
                return RetryOutcome(
                    RetryDecision.RETRY,
                    delay_override=wait_time,
                    log_message=(
                        f"API error on {ticker} (attempt {attempt}/{max_retries}, "
                        f"HTTP {status}): {e}. Retrying in {wait_time}s..."
                    ),
                )

            # Non-API errors: network timeout, connection reset, etc.
            if is_last:
                logger.error(f"All {max_retries} retry attempts exhausted for {ticker}")
                return RetryOutcome(RetryDecision.RETRY)
            wait_time = 2**attempt  # 2s, 4s, 8s
            return RetryOutcome(
                RetryDecision.RETRY,
                delay_override=wait_time,
                log_message=(
                    f"Order placement failed (attempt {attempt}/{max_retries}): {e}. "
                    f"Retrying in {wait_time}s..."
                ),
            )

        try:
            return retry_with_backoff(
                order_func,
                classify=_classify,
                max_retries=max_retries,
                base_delay=2.0,
                multiplier=2.0,
            )
        except (AlpacaAPIError, OrderError) as e:
            if e.status_code == 409:
                logger.info(
                    f"Idempotency: duplicate client_order_id for {ticker}. "
                    f"Order was already placed by a previous attempt - "
                    f"recovering the existing order."
                )
                if client_order_id is not None:
                    existing = self.broker.get_order_by_client_id(client_order_id)
                    if existing is not None:
                        return existing
                # Couldn't recover the original order - surface the 409
                raise
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

        order = self._recent_order_index.get((ticker, side))
        if order is not None:
            age_seconds = (now - order["timestamp"]).total_seconds()
            if age_seconds < 60:
                return {
                    "order_id": order["order_id"],
                    "timestamp": order["timestamp"],
                    "age_seconds": age_seconds,
                }

        return None

    def _generate_idempotency_key(
        self, ticker: str, side: str, signal_timestamp: datetime
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
        self, current_price: float, side: str, offset_pct: float
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
        self, positions: List[Dict[str, Any]], current_prices: Dict[str, float]
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
                side=pos["side"],
            ):
                exits.append(
                    {
                        "ticker": ticker,
                        "reason": (
                            f"Stop loss hit (entry ${pos['avg_entry']:.2f}, "
                            f"now ${current_price:.2f})"
                        ),
                        "current_price": current_price,
                    }
                )
                continue

            # Take profit check
            if self.risk.check_take_profit(
                entry_price=pos["avg_entry"],
                current_price=current_price,
                side=pos["side"],
            ):
                exits.append(
                    {
                        "ticker": ticker,
                        "reason": (
                            f"Take profit hit (entry ${pos['avg_entry']:.2f}, "
                            f"now ${current_price:.2f})"
                        ),
                        "current_price": current_price,
                    }
                )
                continue

            # Trailing stop check (if enabled)
            if self.risk.risk_config.trailing_stop_enabled:
                highest = pos.get("highest_since_entry", pos["avg_entry"])
                if self.risk.check_trailing_stop(
                    entry_price=pos["avg_entry"],
                    highest_since_entry=highest,
                    current_price=current_price,
                    side=pos["side"],
                ):
                    exits.append(
                        {
                            "ticker": ticker,
                            "reason": (
                                f"Trailing stop hit (high ${highest:.2f}, "
                                f"now ${current_price:.2f})"
                            ),
                            "current_price": current_price,
                        }
                    )

        return exits

    def _reconcile_open_close_intent(self, existing: Dict[str, Any]) -> Dict[str, Any]:
        """Reconcile a previously-recorded, non-terminal CLOSE intent
        against the broker, applying the fully-filled-but-not-flat /
        partial-fill / cancelled / rejected state machine (2026-09-11 pass
        4 - see close_position's docstring for the full rationale).

        Returns a dict with an "outcome" key:
            "success"    - confirmed flat; done, no new order needed.
            "in_flight"  - order still open (submitted/partially_filled);
                           report, no new order.
            "quarantine" - contradictory or unverifiable state; halt, no
                           new order. Requires a later reconciliation
                           attempt or deliberate operator/drift-resolution
                           action.
            "rejected"   - the order was rejected with zero fill; halt
                           THIS call (no automatic resubmission loop) - a
                           later, separate close_position()/close_all call
                           may create a fresh logical intent.
            "reuse"      - the prior attempt never reached the broker;
                           safe to resubmit with the SAME client_order_id.
            "proceed"    - safe to submit a fresh close order sized at
                           "qty" (Decimal) - only reached via a cancelled/
                           expired order whose expected remainder
                           (persisted pre-close qty minus cumulative
                           filled qty) matches the broker's current
                           position within QTY_TOLERANCE.
        """
        ticker = existing["ticker"]
        reconciliation = self._reconcile_intent(existing)
        action = reconciliation["action"]

        if action == "quarantine":
            return {"outcome": "quarantine", "detail": reconciliation["detail"]}

        if action == "abandoned":
            # Never sent (or never seen by the broker past the grace
            # period): nothing to reconcile - size a fresh close from a
            # fresh position read.
            return {"outcome": "fresh"}

        order = reconciliation.get("order")
        requested_qty = _to_decimal(existing.get("qty"))
        cumulative_filled = _to_decimal(getattr(order, "filled_qty", None))
        filled_price = (
            float(order.filled_avg_price)
            if order is not None and order.filled_avg_price
            else None
        )

        if action in ("filled", "in_flight"):
            mapped = _map_broker_order_status(getattr(order, "status", None))
            if mapped != INTENT_FILLED:
                # Still open (submitted/partially_filled) - report
                # accurately, never resubmit; cumulative_filled is the
                # broker's own absolute figure, never accumulated by us,
                # so repeated reconciliation can't double-count it.
                return {
                    "outcome": "in_flight",
                    "mapped": mapped,
                    "order": order,
                    "filled_qty": float(cumulative_filled),
                    "filled_price": filled_price,
                }

            # Broker reports the order itself fully filled - this alone is
            # NOT proof the position is flat (A). Confirm against the
            # broker's own position endpoint before declaring success.
            try:
                pos = self.broker.get_position(ticker)
            except Exception as e:
                self.state.update_submission_intent(
                    existing["intent_id"],
                    status=INTENT_UNCERTAIN,
                    last_error=f"filled but could not confirm flat: {e}",
                )
                return {
                    "outcome": "quarantine",
                    "detail": f"position lookup failed after fill: {e}",
                }

            actual_qty = _to_decimal(pos.qty) if pos is not None else Decimal("0")
            actual_side = getattr(pos, "side", None) if pos is not None else None

            if pos is None or _qty_equal(actual_qty, Decimal("0")):
                self.state.update_submission_intent(
                    existing["intent_id"], status=INTENT_RECONCILED
                )
                return {
                    "outcome": "success",
                    "order": order,
                    "filled_qty": float(cumulative_filled),
                    "filled_price": filled_price,
                }

            if actual_side == "short":
                # Sign reversal - never auto-correct with a market order;
                # that would itself be an unreviewed directional trade.
                self.state.update_submission_intent(
                    existing["intent_id"],
                    status=INTENT_UNCERTAIN,
                    last_error=(
                        f"position reversed to short ({actual_qty}) after a "
                        f"reported full fill of {requested_qty}"
                    ),
                )
                logger.critical(
                    f"{ticker}: close order filled requested qty "
                    f"{requested_qty} but broker now reports a SHORT "
                    f"position of {actual_qty} - contradictory. "
                    f"Quarantining; never auto-submitting a corrective order."
                )
                return {
                    "outcome": "quarantine",
                    "detail": "position reversed to short after a reported full fill",
                }

            # Nonzero same-direction position remains despite a reported
            # full fill of the ENTIRE requested quantity - contradictory
            # (e.g. a stale broker position snapshot, or a genuine
            # unaccounted discrepancy). Never silently resubmit a close for
            # what the position endpoint claims remains - that is exactly
            # the unsafe behavior this reconciliation model replaces.
            self.state.update_submission_intent(
                existing["intent_id"],
                status=INTENT_UNCERTAIN,
                last_error=(
                    f"order filled requested qty {requested_qty} but broker "
                    f"still reports {actual_qty} shares - contradictory"
                ),
            )
            logger.critical(
                f"{ticker}: close order filled the entire requested "
                f"quantity ({requested_qty}) but the broker still reports "
                f"{actual_qty} shares - contradictory position state. "
                f"Quarantining; requires reconciliation (the position may "
                f"simply be stale) or operator review, never an automatic "
                f"corrective order."
            )
            return {
                "outcome": "quarantine",
                "detail": "filled-but-not-flat contradiction",
            }

        # action == "terminal": rejected / cancelled / expired.
        mapped = _map_broker_order_status(getattr(order, "status", None))

        if mapped == INTENT_REJECTED:
            self.state.update_submission_intent(
                existing["intent_id"], status=INTENT_RECONCILED
            )
            return {"outcome": "rejected", "detail": "order rejected with zero fill"}

        # Cancelled/expired, possibly after a partial fill.
        self.state.update_submission_intent(
            existing["intent_id"], status=INTENT_RECONCILED
        )
        expected_remaining = requested_qty - cumulative_filled
        if expected_remaining < 0:
            expected_remaining = Decimal("0")

        try:
            pos = self.broker.get_position(ticker)
        except Exception as e:
            return {
                "outcome": "quarantine",
                "detail": f"position lookup failed after cancellation: {e}",
            }

        actual_qty = _to_decimal(pos.qty) if pos is not None else Decimal("0")

        if pos is None or _qty_equal(actual_qty, Decimal("0")):
            return {
                "outcome": "success",
                "order": order,
                "filled_qty": float(cumulative_filled),
                "filled_price": filled_price,
            }

        if not _qty_equal(actual_qty, expected_remaining):
            logger.critical(
                f"{ticker}: cancelled close order's expected remainder "
                f"({expected_remaining} = requested {requested_qty} - filled "
                f"{cumulative_filled}) does not match broker-reported "
                f"position ({actual_qty}) - quarantining rather than "
                f"guessing a safe close quantity."
            )
            quarantine_intent = self.state.create_submission_intent(
                ticker=ticker,
                side="CLOSE",
                qty=float(actual_qty),
                order_type="market",
                strategy_name=self.config.strategy.name,
            )
            self.state.update_submission_intent(
                quarantine_intent["intent_id"],
                status=INTENT_UNCERTAIN,
                last_error=(
                    f"post-cancellation position ({actual_qty}) inconsistent "
                    f"with expected remainder ({expected_remaining})"
                ),
            )
            return {
                "outcome": "quarantine",
                "detail": "post-cancellation position inconsistent with expected remainder",
            }

        # Broker order state and position state are mutually consistent -
        # safe to size a new logical close from the confirmed remainder.
        return {"outcome": "proceed", "qty": actual_qty}

    def close_position(self, ticker: str, reason: str) -> Dict[str, Any]:
        """
        Close a position by submitting an ordinary market SELL through the
        same client_order_id-capable order path as execute_signal's BUY/SELL.

        Why this design, not the broker's close-position endpoint:
        Alpaca's close-position endpoint (AlpacaBroker.close_position) takes
        no client_order_id, so a lost response there is unrecoverable except
        by checking whether the position vanished - and "no position" only
        proves the close eventually landed, not that a SPECIFIC earlier
        attempt is the one that isn't still in flight, partially filled, or
        about to duplicate. An ordinary place_market_order SELL, sized from
        the currently confirmed position and carrying a durable
        client_order_id, gets everything execute_signal's BUY/SELL path
        already has: Alpaca-side idempotency via get_order_by_client_id.

        Fully-filled-but-not-flat reconciliation (2026-09-11 pass 4): a
        reported full fill of the requested quantity is NOT by itself proof
        the position is flat - the broker's position endpoint can lag, and
        treating any nonzero position read as "still needs closing" risked
        submitting a second close order on top of a merely-stale read
        (real duplicate exposure risk). _reconcile_open_close_intent now
        persists and compares: the quantity confirmed before submission
        (the intent's own "qty"), the broker's cumulative filled quantity,
        the expected remaining quantity derived from those two, and the
        broker's latest actual position quantity/side - Decimal-safe,
        QTY_TOLERANCE-bounded. A filled-but-nonzero-remaining contradiction,
        or a sign reversal, is quarantined ("uncertain") rather than acted
        on automatically; only a cancelled/expired order whose expected
        remainder matches the confirmed position is treated as safe to
        retry, and only for that confirmed remainder - never the original
        requested quantity. A rejected (zero-fill) order halts this call
        outright rather than auto-looping; a later, separate invocation may
        create a fresh logical intent after a fresh position read.

        Returns a dict with:
            status: "success" (confirmed flat), "partial" (order partially
                filled, position still open), "pending" (order accepted,
                not yet filled), "blocked" (quarantined - outcome unknown,
                do not treat as closed OR failed), "error" (definite
                rejection or a pre-submission failure).
            fill_status: the underlying mapped intent status, or
                "already_flat" / "rejected" / "unknown".
            order_id, filled_price, filled_qty, reason: as before.
        """
        if self.dry_run:
            logger.info(f"[DRY RUN] Would close position: {ticker} | Reason: {reason}")
            return {"status": "success", "reason": "Dry run", "fill_status": "filled"}

        intent = None
        preconfirmed_qty: Optional[Decimal] = None

        if self.state is not None:
            waiting = self._settle_working_orders_before_close(ticker, reason)
            if waiting is not None:
                return waiting
            existing = self.state.get_open_intent(ticker, "CLOSE")
            if existing is not None:
                recon = self._reconcile_open_close_intent(existing)
                outcome = recon["outcome"]

                if outcome == "quarantine":
                    msg = (
                        f"Close intent {existing['intent_id']} for {ticker} is "
                        f"uncertain ({recon['detail']}) - quarantined until "
                        f"reconciliation succeeds or an operator resolves it "
                        f"(see the existing broker-position drift-resolution "
                        f"process). No new close order will be placed, and no "
                        f"new entry/exit order for {ticker} is safe until "
                        f"this resolves."
                    )
                    logger.warning(msg)
                    return {
                        "status": "blocked",
                        "reason": msg,
                        "fill_status": "uncertain",
                    }

                if outcome == "rejected":
                    msg = (
                        f"Prior close order for {ticker} was rejected "
                        f"(zero fill) - not automatically resubmitting. A "
                        f"later /close_all (or the next exit-check cycle) "
                        f"will create a fresh logical intent after "
                        f"confirming the current position."
                    )
                    logger.error(msg)
                    return {"status": "error", "reason": msg, "fill_status": "rejected"}

                if outcome == "in_flight":
                    mapped = recon["mapped"]
                    logger.info(
                        f"Close for {ticker} already in flight (order "
                        f"{recon['order'].id}, {mapped}) - not resubmitting"
                    )
                    return {
                        "status": (
                            "partial"
                            if mapped == INTENT_PARTIALLY_FILLED
                            else "pending"
                        ),
                        "order_id": recon["order"].id,
                        "reason": reason,
                        "filled_price": recon["filled_price"],
                        "filled_qty": recon["filled_qty"],
                        "fill_status": mapped,
                    }

                if outcome == "success":
                    order = recon["order"]
                    return {
                        "status": "success",
                        "order_id": order.id if order is not None else None,
                        "reason": reason,
                        "filled_price": recon["filled_price"],
                        "filled_qty": recon["filled_qty"],
                        "fill_status": "filled",
                    }

                if outcome == "proceed":
                    preconfirmed_qty = recon["qty"]
                # outcome == "fresh": fall through to a fresh position read.

        if intent is None:
            if preconfirmed_qty is not None:
                # A cancelled/expired order's confirmed, consistent
                # remainder - never the original requested quantity.
                qty_decimal = preconfirmed_qty
            else:
                # No prior open intent (or nothing usable from it) -
                # determine the CONFIRMED remaining position fresh. Never
                # trust a stale qty from a previous decision.
                try:
                    pos = self.broker.get_position(ticker)
                except Exception as e:
                    return {
                        "status": "error",
                        "reason": f"Could not confirm position before close: {e}",
                        "fill_status": "unknown",
                    }

                if pos is None or _qty_equal(pos.qty, Decimal("0")):
                    return {
                        "status": "success",
                        "order_id": None,
                        "reason": f"{reason} (already flat)",
                        "filled_price": None,
                        "filled_qty": None,
                        "fill_status": "already_flat",
                    }
                qty_decimal = _to_decimal(pos.qty)

            qty = float(qty_decimal)

            if self.state is not None:
                intent = self.state.create_submission_intent(
                    ticker=ticker,
                    side="CLOSE",
                    qty=qty,
                    order_type="market",
                    strategy_name=self.config.strategy.name,
                )
                self.state.update_submission_intent(
                    intent["intent_id"], status=INTENT_SUBMITTING
                )
                client_order_id = intent["client_order_id"]
            else:
                client_order_id = self._generate_idempotency_key(
                    ticker, "close", datetime.now(ET)
                )

        self.risk.record_order_submitted(ticker)

        try:
            result = self._place_with_retry(
                lambda: self.broker.place_market_order(
                    symbol=ticker,
                    qty=qty,
                    side="sell",
                    client_order_id=client_order_id,
                ),
                ticker=ticker,
                max_retries=3,
                client_order_id=client_order_id,
            )
            self.risk.record_api_call("close_position")

            order_id = result.id
            filled_qty = float(result.filled_qty) if result.filled_qty else 0.0
            filled_price = (
                float(result.filled_avg_price) if result.filled_avg_price else None
            )
            mapped = _map_broker_order_status(getattr(result, "status", None))

            if self.state is not None and intent is not None:
                self._apply_order_progress(intent, result)
                self.state.update_submission_intent(
                    intent["intent_id"],
                    status=mapped or INTENT_UNCERTAIN,
                    broker_order_id=order_id,
                )

            logger.info(
                f"Close order placed: {ticker} qty={qty} | OrderID: {order_id} | "
                f"status={mapped} | Reason: {reason}"
            )

            if mapped == INTENT_FILLED:
                return {
                    "status": "success",
                    "order_id": order_id,
                    "reason": reason,
                    "filled_price": filled_price,
                    "filled_qty": filled_qty,
                    "fill_status": "filled",
                }
            if mapped is None:
                # Unrecognized broker status string - don't guess.
                if self.state is not None and intent is not None:
                    self.state.update_submission_intent(
                        intent["intent_id"],
                        status=INTENT_UNCERTAIN,
                        last_error=f"unrecognized broker status: {getattr(result, 'status', None)}",
                    )
                return {
                    "status": "blocked",
                    "order_id": order_id,
                    "reason": f"Unrecognized broker status for {ticker} close order",
                    "filled_price": filled_price,
                    "filled_qty": filled_qty,
                    "fill_status": "uncertain",
                }
            return {
                "status": "partial" if mapped == INTENT_PARTIALLY_FILLED else "pending",
                "order_id": order_id,
                "reason": reason,
                "filled_price": filled_price,
                "filled_qty": filled_qty,
                "fill_status": mapped,
            }

        except Exception as e:
            logger.error(f"Failed to close position {ticker}: {e}", exc_info=True)
            if self.state is not None and intent is not None:
                if self._is_definite_rejection(e):
                    self.state.update_submission_intent(
                        intent["intent_id"], status=INTENT_REJECTED, last_error=str(e)
                    )
                    fill_status = "rejected"
                else:
                    self.state.update_submission_intent(
                        intent["intent_id"], status=INTENT_UNCERTAIN, last_error=str(e)
                    )
                    fill_status = "uncertain"
            else:
                fill_status = "unknown"
            if self.notifier:
                self.notifier.send_error_alert(
                    f"❌ Failed to close position: {ticker} - {str(e)}"
                )
            status = "error" if fill_status == "rejected" else "blocked"
            return {"status": status, "reason": str(e), "fill_status": fill_status}

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
        logger.info(
            f"OrderManager daily reset | Orders today: {len(self.order_history)}"
        )
        self.order_history = []
        self.pending_orders = {}
        self._recent_order_index = {}
