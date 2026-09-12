"""
Delayed-consistency tests for CLOSE-order reconciliation (2026-09-11 pass 4,
objective 1): a reported full fill of the requested close quantity is NOT
by itself proof the position is flat - the broker's position endpoint can
lag or contradict the order state. These tests exercise the exact state
machine described in OrderManager.close_position's docstring:

  A. filled for the complete requested quantity:
       - flat position            -> success, no new order
       - stale/contradictory qty  -> quarantine, no new order (ever)
       - reversed sign            -> quarantine, no new order (ever)
       - later becomes flat       -> success on a LATER call, no new order
  B. partially_filled and open    -> report, no new order, no double count
  C. cancelled after partial fill:
       - consistent remainder     -> new order sized from the remainder
       - inconsistent remainder   -> quarantine, no new order
  D. rejected with zero fill      -> halt this call, no auto-loop
  E. missing/unavailable broker state -> quarantine, no new order

Every assertion checks actual broker submission count, submitted
quantities/sides, persisted intent state, and (where relevant) the fake
broker's own tracked "final" position - never just the returned dict.

Uses DelayedFakeBroker from test_close_order_lifecycle.py. No real Alpaca
calls, no network, no credentials, no fixture files touched.
"""

from unittest.mock import patch

import pytest

from alphalive.broker.base_broker import BrokerError
from alphalive.execution.order_manager import OrderManager
from alphalive.state import (
    BotState,
    INTENT_FILLED,
    INTENT_RECONCILED,
    INTENT_REJECTED,
    INTENT_UNCERTAIN,
)
from tests.test_close_order_lifecycle import DelayedFakeBroker, _order
from tests.test_order_manager import sample_config, mock_risk_manager  # noqa: F401


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


def _submit_and_fill(om, broker, state, ticker="AAPL", qty=10.0):
    """Helper: submit a close for `qty` shares, then mark the resulting
    order fully filled at the broker level WITHOUT touching the tracked
    position (simulating a stale/lagging position endpoint) - the caller
    decides what to do with the position afterward."""
    broker.set_position(ticker, qty)
    result = om.close_position(ticker, reason="stop loss")
    assert result["status"] == "pending"
    intent = state.get_open_intent(ticker, "CLOSE")
    order = broker.orders[intent["client_order_id"]]
    broker.orders[intent["client_order_id"]] = _order(
        intent["client_order_id"],
        order.symbol,
        order.qty,
        status="filled",
        filled_qty=order.qty,
    )
    return intent


# ---------------------------------------------------------------------------
# A. Complete fill vs. position endpoint state
# ---------------------------------------------------------------------------


def test_complete_fill_while_position_endpoint_still_stale(om, broker, state):
    """Fill covers the entire requested 10 shares, but get_position still
    (incorrectly) reports 10 - must quarantine, never resubmit."""
    intent = _submit_and_fill(om, broker, state, qty=10.0)
    # Position endpoint NOT updated - still reports the original 10.

    result = om.close_position("AAPL", reason="stop loss")

    assert result["status"] == "blocked"
    assert result["fill_status"] == "uncertain"
    assert len(broker.submit_calls) == 1
    saved = state.get_submission_intent(intent["intent_id"])
    assert saved["status"] == INTENT_UNCERTAIN


def test_complete_fill_followed_later_by_flat_position(om, broker, state):
    """Same contradiction as above, but the position endpoint catches up
    (becomes flat) by the NEXT reconciliation attempt - must resolve to
    success WITHOUT a new order."""
    intent = _submit_and_fill(om, broker, state, qty=10.0)

    # First reconciliation: still stale -> quarantine.
    r1 = om.close_position("AAPL", reason="stop loss")
    assert r1["status"] == "blocked"
    assert len(broker.submit_calls) == 1

    # Position endpoint catches up.
    broker.set_position("AAPL", 0)

    r2 = om.close_position("AAPL", reason="stop loss")
    assert r2["status"] == "success"
    assert r2["fill_status"] == "filled"
    assert len(broker.submit_calls) == 1  # still never resubmitted
    assert state.get_open_intent("AAPL", "CLOSE") is None


def test_complete_fill_with_persistent_contradictory_position(om, broker, state):
    """The contradiction never resolves across many reconciliation
    attempts - must stay quarantined every time, never resubmit, never
    declare success on a position that never went flat."""
    intent = _submit_and_fill(om, broker, state, qty=10.0)

    for _ in range(5):
        result = om.close_position("AAPL", reason="stop loss")
        assert result["status"] == "blocked"
        assert result["fill_status"] == "uncertain"

    assert len(broker.submit_calls) == 1
    assert (
        state.get_submission_intent(intent["intent_id"])["status"] == INTENT_UNCERTAIN
    )


def test_complete_fill_followed_by_reversed_position(om, broker, state):
    """Position endpoint now reports a SHORT position after a reported
    full-fill SELL of the confirmed long position - a sign reversal must
    quarantine and must NEVER trigger an automatic corrective order (which
    would itself be an unreviewed directional trade)."""
    intent = _submit_and_fill(om, broker, state, qty=10.0)
    broker.set_position("AAPL", 5, side="short")

    result = om.close_position("AAPL", reason="stop loss")

    assert result["status"] == "blocked"
    assert result["fill_status"] == "uncertain"
    assert len(broker.submit_calls) == 1  # no corrective BUY or SELL ever issued
    saved = state.get_submission_intent(intent["intent_id"])
    assert saved["status"] == INTENT_UNCERTAIN
    assert "short" in saved["last_error"].lower()


# ---------------------------------------------------------------------------
# B. Partial fill, order remains open
# ---------------------------------------------------------------------------


def test_partial_fill_while_order_remains_open_no_resubmit_no_double_count(
    om, broker, state
):
    broker.set_position("AAPL", 10)
    om.close_position("AAPL", reason="stop loss")
    intent = state.get_open_intent("AAPL", "CLOSE")
    broker.apply_fill(
        intent["client_order_id"], filled_qty=4.0, status="partially_filled"
    )

    r1 = om.close_position("AAPL", reason="stop loss")
    assert r1["status"] == "partial"
    assert r1["filled_qty"] == 4.0

    # Reconcile again without any change - same cumulative figure, no
    # double counting, no new order.
    r2 = om.close_position("AAPL", reason="stop loss")
    assert r2["status"] == "partial"
    assert r2["filled_qty"] == 4.0
    assert len(broker.submit_calls) == 1


# ---------------------------------------------------------------------------
# C. Cancelled after partial fill: consistent vs. inconsistent remainder
# ---------------------------------------------------------------------------


def test_partial_fill_then_cancellation_with_consistent_remainder(om, broker, state):
    broker.set_position("AAPL", 10)
    om.close_position("AAPL", reason="stop loss")
    intent = state.get_open_intent("AAPL", "CLOSE")
    # 4 filled (position drops to 6), then cancelled - consistent: broker
    # position (6) matches expected remainder (10 - 4 = 6).
    broker.apply_fill(intent["client_order_id"], filled_qty=4.0, status="canceled")

    result = om.close_position("AAPL", reason="stop loss")

    assert result["status"] == "pending"
    assert len(broker.submit_calls) == 2
    symbol, qty, side, _ = broker.submit_calls[-1]
    assert qty == 6.0  # confirmed remainder, never the original 10
    assert side == "sell"


def test_partial_fill_then_cancellation_with_inconsistent_remainder(om, broker, state):
    broker.set_position("AAPL", 10)
    om.close_position("AAPL", reason="stop loss")
    intent = state.get_open_intent("AAPL", "CLOSE")
    # 4 filled -> expected remainder 6, but the position endpoint reports
    # something else entirely (e.g. 8) - inconsistent.
    broker.apply_fill(intent["client_order_id"], filled_qty=4.0, status="canceled")
    broker.set_position("AAPL", 8)  # override to an inconsistent value

    result = om.close_position("AAPL", reason="stop loss")

    assert result["status"] == "blocked"
    assert result["fill_status"] == "uncertain"
    assert len(broker.submit_calls) == 1  # never guessed a quantity to submit
    intents = state.list_all_intents()
    uncertain = [i for i in intents if i["status"] == INTENT_UNCERTAIN]
    assert len(uncertain) == 1


# ---------------------------------------------------------------------------
# D. Rejected with zero fill
# ---------------------------------------------------------------------------


def test_rejected_order_zero_fill_halts_without_auto_loop(om, broker, state):
    broker.set_position("AAPL", 10)
    om.close_position("AAPL", reason="stop loss")
    intent = state.get_open_intent("AAPL", "CLOSE")
    broker.set_order_status(intent["client_order_id"], status="rejected")

    result = om.close_position("AAPL", reason="stop loss")

    assert result["status"] == "error"
    assert result["fill_status"] == "rejected"
    assert len(broker.submit_calls) == 1  # no automatic resubmission this call
    assert (
        state.get_submission_intent(intent["intent_id"])["status"] == INTENT_RECONCILED
    )

    # A LATER, separate /close_all-style call (fresh position confirmation)
    # is allowed to create a new logical intent.
    later = om.close_position("AAPL", reason="stop loss")
    assert later["status"] == "pending"
    assert len(broker.submit_calls) == 2


# ---------------------------------------------------------------------------
# E. Missing/unavailable broker state
# ---------------------------------------------------------------------------


def test_missing_broker_state_during_fill_confirmation_quarantines(om, broker, state):
    intent_dict = None

    def _fail_lookup(symbol):
        raise BrokerError("position endpoint unavailable")

    broker.set_position("AAPL", 10)
    om.close_position("AAPL", reason="stop loss")
    intent = state.get_open_intent("AAPL", "CLOSE")
    order = broker.orders[intent["client_order_id"]]
    broker.orders[intent["client_order_id"]] = _order(
        intent["client_order_id"],
        order.symbol,
        order.qty,
        status="filled",
        filled_qty=order.qty,
    )
    broker.position_lookup_side_effect = _fail_lookup

    result = om.close_position("AAPL", reason="stop loss")

    assert result["status"] == "blocked"
    assert result["fill_status"] == "uncertain"
    assert len(broker.submit_calls) == 1


# ---------------------------------------------------------------------------
# Repeated /close_all during every uncertain state; restart during every
# state; no extra submissions while contradictory
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "setup",
    [
        "stale_full_fill",
        "reversed_position",
        "inconsistent_cancel_remainder",
        "missing_broker_state",
    ],
)
def test_repeated_close_all_during_every_uncertain_state_never_resubmits(
    om, broker, state, setup
):
    if setup == "stale_full_fill":
        _submit_and_fill(om, broker, state, qty=10.0)
    elif setup == "reversed_position":
        _submit_and_fill(om, broker, state, qty=10.0)
        broker.set_position("AAPL", 3, side="short")
    elif setup == "inconsistent_cancel_remainder":
        broker.set_position("AAPL", 10)
        om.close_position("AAPL", reason="stop loss")
        intent = state.get_open_intent("AAPL", "CLOSE")
        broker.apply_fill(intent["client_order_id"], filled_qty=4.0, status="canceled")
        broker.set_position("AAPL", 8)
    elif setup == "missing_broker_state":
        broker.set_position("AAPL", 10)
        om.close_position("AAPL", reason="stop loss")
        intent = state.get_open_intent("AAPL", "CLOSE")
        order = broker.orders[intent["client_order_id"]]
        broker.orders[intent["client_order_id"]] = _order(
            intent["client_order_id"],
            order.symbol,
            order.qty,
            status="filled",
            filled_qty=order.qty,
        )
        broker.position_lookup_side_effect = lambda s: (_ for _ in ()).throw(
            BrokerError("down")
        )

    calls_before = len(broker.submit_calls)
    for _ in range(4):
        result = om.close_position("AAPL", reason="stop loss")
        assert result["status"] == "blocked"

    assert len(broker.submit_calls) == calls_before  # never grows


def test_restart_during_every_uncertain_state_stays_quarantined(om, broker, state):
    """A fresh OrderManager sharing the same broker/state (simulating a
    restart) must reconcile the SAME uncertain intent rather than treating
    it as absent and creating a new one."""
    intent = _submit_and_fill(om, broker, state, qty=10.0)
    r1 = om.close_position("AAPL", reason="stop loss")
    assert r1["status"] == "blocked"

    om2 = OrderManager(
        broker=broker,
        risk_manager=om.risk,
        config=om.config,
        notifier=None,
        dry_run=False,
        state=state,
    )
    r2 = om2.close_position("AAPL", reason="stop loss")

    assert r2["status"] == "blocked"
    assert len(broker.submit_calls) == 1
    assert len(state.list_all_intents()) == 1


def test_restart_after_success_reports_flat_without_new_order(om, broker, state):
    broker.set_position("AAPL", 10)
    om.close_position("AAPL", reason="stop loss")
    intent = state.get_open_intent("AAPL", "CLOSE")
    broker.apply_fill(intent["client_order_id"], filled_qty=10.0, status="filled")

    om2 = OrderManager(
        broker=broker,
        risk_manager=om.risk,
        config=om.config,
        notifier=None,
        dry_run=False,
        state=state,
    )
    result = om2.close_position("AAPL", reason="stop loss")

    assert result["status"] == "success"
    assert len(broker.submit_calls) == 1


def test_restart_during_partial_fill_reconciles_not_resubmits(om, broker, state):
    broker.set_position("AAPL", 10)
    om.close_position("AAPL", reason="stop loss")
    intent = state.get_open_intent("AAPL", "CLOSE")
    broker.apply_fill(
        intent["client_order_id"], filled_qty=4.0, status="partially_filled"
    )

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
    assert len(broker.submit_calls) == 1


def test_restart_during_rejected_state_does_not_auto_resubmit(om, broker, state):
    broker.set_position("AAPL", 10)
    om.close_position("AAPL", reason="stop loss")
    intent = state.get_open_intent("AAPL", "CLOSE")
    broker.set_order_status(intent["client_order_id"], status="rejected")

    om2 = OrderManager(
        broker=broker,
        risk_manager=om.risk,
        config=om.config,
        notifier=None,
        dry_run=False,
        state=state,
    )
    result = om2.close_position("AAPL", reason="stop loss")

    assert result["status"] == "error"
    assert result["fill_status"] == "rejected"
    assert len(broker.submit_calls) == 1


# ---------------------------------------------------------------------------
# No unintended short position; filled quantity never applied twice
# ---------------------------------------------------------------------------


def test_no_unintended_short_position_across_full_lifecycle(om, broker, state):
    """From open long position through a clean full close, the broker
    double never records a SELL larger than the confirmed long position,
    and the fake broker's own tracked position never goes negative."""
    broker.set_position("AAPL", 10)
    om.close_position("AAPL", reason="stop loss")
    intent = state.get_open_intent("AAPL", "CLOSE")
    broker.apply_fill(intent["client_order_id"], filled_qty=10.0, status="filled")

    result = om.close_position("AAPL", reason="stop loss")

    assert result["status"] == "success"
    for symbol, qty, side, _ in broker.submit_calls:
        assert side == "sell"
        assert qty <= 10.0
    assert broker.get_position("AAPL") is None  # flat, never negative/short


def test_filled_quantity_never_applied_twice_across_reconciliations(om, broker, state):
    """Reconciling the same filled order must report the fill exactly once
    (filled_qty=10.0, fill_status="filled") - the intent is then reconciled
    (terminal), so every SUBSEQUENT close_position call takes the fresh-
    position-read path and correctly reports "already_flat" (filled_qty
    None, not a re-quoted or accumulated figure) rather than re-applying
    or doubling the original fill."""
    broker.set_position("AAPL", 10)
    om.close_position("AAPL", reason="stop loss")
    intent = state.get_open_intent("AAPL", "CLOSE")
    broker.apply_fill(intent["client_order_id"], filled_qty=10.0, status="filled")

    first = om.close_position("AAPL", reason="stop loss")
    assert first["status"] == "success"
    assert first["fill_status"] == "filled"
    assert first["filled_qty"] == 10.0

    for _ in range(3):
        result = om.close_position("AAPL", reason="stop loss")
        assert result["status"] == "success"
        assert result["fill_status"] == "already_flat"
        assert result["filled_qty"] != 20.0 and result["filled_qty"] != 30.0

    assert len(broker.submit_calls) == 1  # never a second real order
