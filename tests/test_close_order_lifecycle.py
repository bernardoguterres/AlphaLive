"""
Tests for the redesigned close-order mechanism (2026-09-11 pass 3, objective 1):
close_position() now submits an ordinary client_order_id-capable market SELL
(via place_market_order) sized from the confirmed current position, instead
of calling the broker's close-position endpoint and reconciling by position
presence alone. Position presence alone is not proof of anything - an
accepted close can still be open, partially filled, or its response lost
while the order is still live.

Uses a DelayedFakeBroker that models orders progressing through
new -> partially_filled -> filled (or -> rejected/cancelled) across separate
calls, and a position that only changes when a fill is applied - not a
broker that instantly deletes positions on any close attempt.

No real Alpaca calls, no network, no credentials.
"""

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from alphalive.broker.base_broker import BrokerError, Order, Position
from alphalive.execution.order_manager import OrderManager
from alphalive.state import (
    BotState,
    INTENT_CANCELLED,
    INTENT_FILLED,
    INTENT_PARTIALLY_FILLED,
    INTENT_REJECTED,
    INTENT_UNCERTAIN,
)
from tests.test_order_manager import sample_config, mock_risk_manager  # noqa: F401

ET = ZoneInfo("America/New_York")


def _order(client_order_id, symbol, qty, status, filled_qty=0.0):
    return Order(
        id=f"broker_{client_order_id}",
        symbol=symbol,
        qty=qty,
        side="sell",
        order_type="market",
        limit_price=None,
        status=status,
        filled_qty=filled_qty,
        filled_avg_price=100.0 if filled_qty else None,
        submitted_at=datetime.now(ET),
        filled_at=datetime.now(ET) if status == "filled" else None,
    )


class DelayedFakeBroker:
    """Models orders progressing across separate calls (new -> partially_filled
    -> filled, or -> rejected/cancelled) and a position that only changes
    when a fill is explicitly applied - never instantly deleted on submit."""

    def __init__(self):
        self.orders = {}  # client_order_id -> Order
        self.positions = {}  # symbol -> qty (always positive; direction in .sides)
        self.sides = {}  # symbol -> "long" | "short"
        self.submit_calls = []
        self.submit_side_effect = None
        self.lookup_side_effect = None
        self.position_lookup_side_effect = None

    def set_position(self, symbol, qty, side="long"):
        if qty <= 0:
            self.positions.pop(symbol, None)
            self.sides.pop(symbol, None)
        else:
            self.positions[symbol] = qty
            self.sides[symbol] = side

    def place_market_order(self, symbol, qty, side, client_order_id):
        self.submit_calls.append((symbol, qty, side, client_order_id))
        if self.submit_side_effect is not None:
            return self.submit_side_effect(symbol, qty, side, client_order_id)
        order = _order(client_order_id, symbol, qty, status="new", filled_qty=0.0)
        self.orders[client_order_id] = order
        return order

    def place_limit_order(self, symbol, qty, side, limit_price, client_order_id):
        return self.place_market_order(symbol, qty, side, client_order_id)

    def get_order_by_client_id(self, client_order_id):
        if self.lookup_side_effect is not None:
            return self.lookup_side_effect(client_order_id)
        return self.orders.get(client_order_id)

    def get_position(self, symbol):
        if self.position_lookup_side_effect is not None:
            return self.position_lookup_side_effect(symbol)
        qty = self.positions.get(symbol)
        if qty is None or qty <= 0:
            return None
        return Position(
            symbol=symbol,
            qty=qty,
            side=self.sides.get(symbol, "long"),
            avg_entry_price=100.0,
            current_price=100.0,
            unrealized_pl=0.0,
            unrealized_plpc=0.0,
            market_value=qty * 100.0,
        )

    def get_all_positions(self):
        return [self.get_position(s) for s in list(self.positions.keys())]

    def apply_fill(self, client_order_id, filled_qty, status="filled"):
        """Progress a previously-submitted order: reduce the tracked
        position by the newly-filled amount and update the order's status/
        filled_qty. `filled_qty` is the order's CUMULATIVE filled quantity
        (matches Alpaca's own semantics), not a delta."""
        order = self.orders[client_order_id]
        already_reflected = order.filled_qty or 0.0
        delta = filled_qty - already_reflected
        if delta > 0:
            symbol = order.symbol
            current = self.positions.get(symbol, 0.0)
            self.set_position(symbol, current - delta)
        self.orders[client_order_id] = _order(
            client_order_id,
            order.symbol,
            order.qty,
            status=status,
            filled_qty=filled_qty,
        )

    def set_order_status(self, client_order_id, status):
        order = self.orders[client_order_id]
        self.orders[client_order_id] = _order(
            client_order_id,
            order.symbol,
            order.qty,
            status=status,
            filled_qty=order.filled_qty,
        )


@pytest.fixture
def state(tmp_path):
    return BotState(state_file=str(tmp_path / "state.json"))


@pytest.fixture
def broker():
    return DelayedFakeBroker()


@pytest.fixture
def om(broker, mock_risk_manager, sample_config, state):
    return OrderManager(
        broker=broker,
        risk_manager=mock_risk_manager,
        config=sample_config,
        notifier=None,
        dry_run=False,
        state=state,
    )


# ---------------------------------------------------------------------------
# Close accepted but still open / response lost while still open
# ---------------------------------------------------------------------------


def test_close_accepted_but_still_open_reports_pending_not_success(om, broker, state):
    broker.set_position("AAPL", 10)
    result = om.close_position("AAPL", reason="stop loss")

    assert result["status"] == "pending"
    assert result["fill_status"] == "submitted"
    # Position must still show as open - no false "closed".
    assert broker.get_position("AAPL") is not None
    intents = state.list_all_intents()
    assert len(intents) == 1
    assert intents[0]["status"] == "submitted"


def test_response_lost_while_close_remains_open_does_not_resubmit(om, broker, state):
    broker.set_position("AAPL", 10)

    def _lost_response(symbol, qty, side, client_order_id):
        # Broker DID create the order, but the client never sees the
        # response (network drop after acceptance).
        order = _order(client_order_id, symbol, qty, status="new", filled_qty=0.0)
        broker.orders[client_order_id] = order
        raise ConnectionError("response lost")

    broker.submit_side_effect = _lost_response
    result1 = om.close_position("AAPL", reason="stop loss")
    assert result1["status"] in ("blocked", "error")
    intent = state.list_all_intents()[0]
    assert intent["status"] == INTENT_UNCERTAIN
    calls_after_first_attempt = len(broker.submit_calls)  # internal retries, all raised

    broker.submit_side_effect = None
    result2 = om.close_position("AAPL", reason="stop loss")
    # Reconciles the SAME order (found via get_order_by_client_id) - no
    # second top-level submission.
    assert result2["status"] == "pending"
    assert len(broker.submit_calls) == calls_after_first_attempt
    assert len(state.list_all_intents()) == 1


def test_position_still_present_while_close_order_open_is_not_treated_as_failure(
    om, broker, state
):
    """Position presence alone must never be read as proof the close
    request was absent or failed - the order is legitimately still open."""
    broker.set_position("AAPL", 10)
    om.close_position("AAPL", reason="stop loss")
    intent = state.list_all_intents()[0]

    result = om.close_position("AAPL", reason="stop loss")
    assert result["status"] == "pending"
    assert (
        len(broker.submit_calls) == 1
    )  # never resubmitted just because a position exists
    assert state.get_submission_intent(intent["intent_id"])["status"] == "submitted"


# ---------------------------------------------------------------------------
# Partial fills: restart, repeated /close_all, then full fill
# ---------------------------------------------------------------------------


def test_partial_fill_followed_by_restart_reconciles_not_resubmits(om, broker, state):
    broker.set_position("AAPL", 10)
    om.close_position("AAPL", reason="stop loss")
    intent = state.list_all_intents()[0]
    broker.apply_fill(
        intent["client_order_id"], filled_qty=4.0, status="partially_filled"
    )

    # "Restart": fresh OrderManager sharing the same BotState/broker.
    om2 = OrderManager(
        broker=broker,
        risk_manager=om.risk,
        config=om.config,
        notifier=None,
        dry_run=False,
        state=state,
    )
    result = om2.close_position("AAPL", reason="stop loss")

    assert result["status"] == "partial"
    assert result["filled_qty"] == 4.0
    assert len(broker.submit_calls) == 1  # no new order placed
    assert broker.get_position("AAPL").qty == 6.0  # remaining, not double-reduced


def test_partial_fill_followed_by_repeated_close_all_reduces_remaining(
    om, broker, state
):
    broker.set_position("AAPL", 10)
    om.close_position("AAPL", reason="stop loss")
    intent = state.list_all_intents()[0]
    broker.apply_fill(
        intent["client_order_id"], filled_qty=4.0, status="partially_filled"
    )

    r2 = om.close_position("AAPL", reason="stop loss")
    assert r2["status"] == "partial"
    assert len(broker.submit_calls) == 1

    # The order later finishes (rest cancelled by the exchange, a real
    # possibility for partial market fills) with 4 filled, 6 unfilled.
    broker.set_order_status(intent["client_order_id"], status="canceled")

    r3 = om.close_position("AAPL", reason="stop loss")
    # A fresh order must be sized from the CONFIRMED remaining position
    # (6), never the original 10.
    assert len(broker.submit_calls) == 2
    _, qty, _, _ = broker.submit_calls[-1]
    assert qty == 6.0


def test_full_fill_followed_by_lost_response_recovers_via_client_order_id(
    om, broker, state
):
    broker.set_position("AAPL", 10)

    def _lost_response(symbol, qty, side, client_order_id):
        order = _order(client_order_id, symbol, qty, status="filled", filled_qty=qty)
        broker.orders[client_order_id] = order
        broker.set_position(symbol, 0.0)  # broker-side, fill actually happened
        raise ConnectionError("response lost after fill")

    broker.submit_side_effect = _lost_response
    result1 = om.close_position("AAPL", reason="stop loss")
    assert result1["status"] in ("blocked", "error")
    calls_after_first_attempt = len(broker.submit_calls)

    broker.submit_side_effect = None
    result2 = om.close_position("AAPL", reason="stop loss")
    assert result2["status"] == "success"
    assert result2["fill_status"] == "filled"
    assert len(broker.submit_calls) == calls_after_first_attempt  # never resubmitted


# ---------------------------------------------------------------------------
# Rejected / cancelled close orders
# ---------------------------------------------------------------------------


def test_rejected_close_order_is_reconciled_not_retried_with_full_qty_forever(
    om, broker, state
):
    broker.set_position("AAPL", 10)

    def _reject(symbol, qty, side, client_order_id):
        raise ValueError("Insufficient buying power")  # definite rejection

    broker.submit_side_effect = _reject
    result1 = om.close_position("AAPL", reason="stop loss")
    assert result1["status"] == "error"
    intent = state.list_all_intents()[0]
    assert intent["status"] == INTENT_REJECTED

    # Next attempt creates a genuinely new intent (old one reconciled),
    # sized from the still-unchanged confirmed position.
    broker.submit_side_effect = None
    result2 = om.close_position("AAPL", reason="stop loss")
    assert result2["status"] == "pending"
    intents = state.list_all_intents()
    assert len(intents) == 2
    assert intents[1]["client_order_id"] != intent["client_order_id"]


def test_cancelled_close_order_reconciles_and_resizes_from_remainder(om, broker, state):
    broker.set_position("AAPL", 10)
    om.close_position("AAPL", reason="stop loss")
    intent = state.list_all_intents()[0]
    # Cancelled with a partial fill already applied.
    broker.apply_fill(intent["client_order_id"], filled_qty=3.0, status="canceled")

    result = om.close_position("AAPL", reason="stop loss")
    assert len(broker.submit_calls) == 2
    _, qty, _, _ = broker.submit_calls[-1]
    assert qty == 7.0  # 10 - 3 already filled


# ---------------------------------------------------------------------------
# Broker lookup unavailable (order lookup, and position lookup)
# ---------------------------------------------------------------------------


def test_broker_order_lookup_unavailable_quarantines(om, broker, state):
    broker.set_position("AAPL", 10)
    om.close_position("AAPL", reason="stop loss")

    broker.lookup_side_effect = lambda coid: (_ for _ in ()).throw(
        BrokerError("network down")
    )
    result = om.close_position("AAPL", reason="stop loss")

    assert result["status"] == "blocked"
    assert result["fill_status"] == "uncertain"
    intent = state.list_all_intents()[0]
    assert intent["status"] == INTENT_UNCERTAIN


def test_position_lookup_unavailable_before_new_order_errors_safely(om, broker, state):
    """No prior intent exists yet, so there's nothing to quarantine - but a
    position lookup failure must still prevent submitting a blind order."""
    broker.position_lookup_side_effect = lambda symbol: (_ for _ in ()).throw(
        BrokerError("network down")
    )
    result = om.close_position("AAPL", reason="stop loss")

    assert result["status"] == "error"
    assert broker.submit_calls == []


def test_position_lookup_unavailable_after_fill_quarantines(om, broker, state):
    """Order reports filled, but confirming the position is actually flat
    fails - must quarantine, never silently declare success."""
    broker.set_position("AAPL", 10)
    om.close_position("AAPL", reason="stop loss")
    intent = state.list_all_intents()[0]
    broker.apply_fill(intent["client_order_id"], filled_qty=10.0, status="filled")

    broker.position_lookup_side_effect = lambda symbol: (_ for _ in ()).throw(
        BrokerError("network down")
    )
    result = om.close_position("AAPL", reason="stop loss")

    assert result["status"] == "blocked"
    assert (
        state.get_submission_intent(intent["intent_id"])["status"] == INTENT_UNCERTAIN
    )


def test_open_order_and_position_state_contradiction_does_not_crash(om, broker, state):
    """Order reports 'filled' for the ENTIRE requested qty, but the
    position lookup still shows shares remaining (e.g. a concurrent manual
    trade, or a stale broker position snapshot) - 2026-09-11 pass 4:
    this must quarantine (never auto-submit a second close order on top
    of a merely-stale read - see test_close_reconciliation_consistency.py
    for the full filled-but-not-flat state machine this exercises)."""
    broker.set_position("AAPL", 10)
    om.close_position("AAPL", reason="stop loss")
    intent = state.list_all_intents()[0]
    order = broker.orders[intent["client_order_id"]]
    # Mark filled WITHOUT reducing the tracked position (contradiction).
    broker.orders[intent["client_order_id"]] = _order(
        intent["client_order_id"],
        order.symbol,
        order.qty,
        status="filled",
        filled_qty=order.qty,
    )

    result = om.close_position("AAPL", reason="stop loss")
    assert result["status"] == "blocked"
    assert result["fill_status"] == "uncertain"
    assert len(broker.submit_calls) == 1  # never auto-resubmitted
    assert (
        state.get_submission_intent(intent["intent_id"])["status"] == INTENT_UNCERTAIN
    )


# ---------------------------------------------------------------------------
# Two tickers concurrently; a CLOSE intent blocking a new entry
# ---------------------------------------------------------------------------


def test_two_tickers_closing_concurrently_get_independent_intents(om, broker, state):
    broker.set_position("AAPL", 10)
    broker.set_position("MSFT", 5)

    r1 = om.close_position("AAPL", reason="stop loss")
    r2 = om.close_position("MSFT", reason="stop loss")

    assert r1["status"] == "pending"
    assert r2["status"] == "pending"
    intents = state.list_all_intents()
    assert {i["ticker"] for i in intents} == {"AAPL", "MSFT"}
    assert len(broker.submit_calls) == 2


def test_uncertain_close_intent_blocks_a_new_buy_signal(om, broker, state):
    broker.set_position("AAPL", 10)

    def _lost_response(symbol, qty, side, client_order_id):
        raise ConnectionError("lost")

    broker.submit_side_effect = _lost_response
    om.close_position("AAPL", reason="stop loss")  # -> uncertain, retries exhausted
    intent = state.list_all_intents()[0]
    assert intent["status"] == INTENT_UNCERTAIN
    assert intent["side"] == "CLOSE"

    # A BUY signal for the same ticker checks get_open_intent(ticker, "BUY")
    # - a different side, so it is NOT itself blocked by the CLOSE intent
    # (each (ticker, side) pair is tracked independently) - but a SELL
    # decision would collide by ticker+side if it were "CLOSE" too. Confirm
    # the CLOSE intent itself stays open/blocking for further close attempts.
    broker.submit_side_effect = None
    assert state.get_open_intent("AAPL", "CLOSE") is not None
    result = om.close_position("AAPL", reason="stop loss")
    assert result["status"] == "blocked"


# ---------------------------------------------------------------------------
# No unintended short / reversed position
# ---------------------------------------------------------------------------


def test_close_never_submits_more_than_confirmed_position_no_short_risk(
    om, broker, state
):
    broker.set_position("AAPL", 7)
    om.close_position("AAPL", reason="stop loss")
    _, qty, side, _ = broker.submit_calls[0]
    assert qty == 7.0  # exactly the held quantity
    assert side == "sell"  # never anything that could flip to short


def test_repeated_recovery_never_submits_duplicate_close_exposure(om, broker, state):
    broker.set_position("AAPL", 10)
    om.close_position("AAPL", reason="stop loss")
    intent = state.list_all_intents()[0]

    # Recover the same intent many times in a row (simulating repeated
    # restarts / repeated /close_all before the order resolves).
    for _ in range(5):
        result = om.close_position("AAPL", reason="stop loss")
        assert result["status"] == "pending"

    assert len(broker.submit_calls) == 1  # exactly one real order, ever
    assert len(state.list_all_intents()) == 1
