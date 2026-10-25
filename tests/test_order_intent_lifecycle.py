"""
Tests for the durable submission-intent lifecycle (objective 1 of the
2026-09-11 hardening pass).

Covers: intent creation before any broker call, reuse of the same
client_order_id across same-process retries and process restarts,
reconciliation of prepared/submitted/partially_filled/filled/rejected/
cancelled broker outcomes, quarantine ("uncertain") on an unverifiable or
contradictory broker response, and that two distinct legitimate signals for
the same ticker get distinct intents.

Uses a FakeBroker test double and a real BotState backed by a tmp_path file -
no real Alpaca calls, no network.
"""

import os
import pytest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from alphalive.execution.order_manager import OrderManager
from alphalive.broker.base_broker import Order, BrokerError
from alphalive.state import (
    BotState,
    INTENT_ABANDONED,
    INTENT_FILLED,
    INTENT_PREPARED,
    INTENT_RECONCILED,
    INTENT_REJECTED,
    INTENT_SUBMITTING,
    INTENT_UNCERTAIN,
)
from tests.test_order_manager import sample_config, mock_risk_manager  # noqa: F401

ET = ZoneInfo("America/New_York")


def _order(client_order_id, status="filled", qty=10.0, filled_qty=None, symbol="AAPL"):
    return Order(
        id=f"broker_{client_order_id}",
        symbol=symbol,
        qty=qty,
        side="buy",
        order_type="market",
        limit_price=None,
        status=status,
        filled_qty=(
            filled_qty
            if filled_qty is not None
            else (qty if status == "filled" else 0.0)
        ),
        filled_avg_price=100.0 if status in ("filled", "partially_filled") else None,
        submitted_at=datetime.now(ET),
        filled_at=datetime.now(ET) if status == "filled" else None,
    )


class FakeBroker:
    """Minimal broker double giving full control over submit/lookup outcomes."""

    def __init__(self):
        self.orders_by_client_id = {}
        self.submit_calls = []
        self.submit_side_effect = None  # callable(client_order_id) -> Order | raises
        self.lookup_side_effect = (
            None  # callable(client_order_id) -> Order | None | raises
        )
        self.position = None

    def place_market_order(self, symbol, qty, side, client_order_id):
        self.submit_calls.append(client_order_id)
        if self.submit_side_effect is not None:
            return self.submit_side_effect(client_order_id)
        order = _order(client_order_id, status="filled", qty=qty, symbol=symbol)
        self.orders_by_client_id[client_order_id] = order
        return order

    def place_limit_order(self, symbol, qty, side, limit_price, client_order_id):
        return self.place_market_order(symbol, qty, side, client_order_id)

    def get_order_by_client_id(self, client_order_id):
        if self.lookup_side_effect is not None:
            return self.lookup_side_effect(client_order_id)
        return self.orders_by_client_id.get(client_order_id)

    def get_position(self, symbol):
        return self.position

    def get_all_positions(self):
        return []


@pytest.fixture
def state(tmp_path):
    return BotState(state_file=str(tmp_path / "state.json"))


@pytest.fixture
def broker():
    return FakeBroker()


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


BUY_SIGNAL = {"signal": "BUY", "reason": "test"}


# ---------------------------------------------------------------------------
# Basic lifecycle: intent is created durably, reused, and completed
# ---------------------------------------------------------------------------


def test_intent_created_before_broker_call(om, broker, state):
    """A submission intent must exist in durable state, in a non-terminal
    status, even if we could observe it mid-flight (simulated here by
    checking state right after execute_signal - the create+prepare happens
    before place_market_order is invoked)."""
    result = om.execute_signal(
        ticker="AAPL",
        signal=BUY_SIGNAL,
        current_price=100.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    assert result["status"] == "success"
    intents = state.list_all_intents()
    assert len(intents) == 1
    assert intents[0]["status"] == INTENT_FILLED
    assert intents[0]["ticker"] == "AAPL"
    assert intents[0]["side"] == "BUY"
    # client_order_id actually used on the broker call
    assert broker.submit_calls == [intents[0]["client_order_id"]]


def test_prepared_intent_is_abandoned_not_reused(om, broker, state):
    """An intent still "prepared" was persisted before "submitting", so it
    provably never reached the broker. Its decision context is gone: it is
    abandoned (terminal), never replayed for a later signal."""
    intent = state.create_submission_intent(
        ticker="AAPL",
        side="BUY",
        qty=10,
        order_type="market",
        strategy_name="ma_crossover",
    )
    reconciliation = om._reconcile_intent(state.get_open_intent("AAPL", "BUY"))
    assert reconciliation["action"] == "abandoned"
    assert (
        state.get_submission_intent(intent["intent_id"])["status"] == INTENT_ABANDONED
    )
    assert broker.submit_calls == []


# ---------------------------------------------------------------------------
# Crash-before-broker-call / crash-after-intent-creation: restart reuses the
# same client_order_id instead of generating a new one.
# ---------------------------------------------------------------------------


def test_restart_after_prepared_abandons_old_intent_and_submits_new_decision_once(
    om, broker, state
):
    """Crash before the broker call (status=prepared). The restarted
    process's NEW signal gets a NEW intent; the stale one is abandoned and
    never sent. Exactly one broker order results."""
    stale = state.create_submission_intent(
        ticker="AAPL",
        side="BUY",
        qty=10,
        order_type="market",
        strategy_name="ma_crossover",
    )
    assert stale["status"] == INTENT_PREPARED
    result = om.execute_signal(
        ticker="AAPL",
        signal=BUY_SIGNAL,
        current_price=100.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    assert result["status"] == "success"
    assert state.get_submission_intent(stale["intent_id"])["status"] == INTENT_ABANDONED
    intents = state.list_all_intents()
    assert len(intents) == 2
    new = [i for i in intents if i["intent_id"] != stale["intent_id"]][0]
    assert broker.submit_calls == [new["client_order_id"]]


# ---------------------------------------------------------------------------
# Broker accepted but client lost the response / crashed after acceptance
# but before local recording: restart must recover, not resubmit.
# ---------------------------------------------------------------------------


def test_restart_finds_accepted_order_blocks_without_resubmitting(om, broker, state):
    """Broker accepted the order but the process never recorded the result.
    The order is still live: a new decision must be BLOCKED (order in
    flight) - never reported as a successful new order, never resubmitted,
    and no position is booked for an unfilled order."""
    intent = state.create_submission_intent(
        ticker="AAPL",
        side="BUY",
        qty=10,
        order_type="market",
        strategy_name="ma_crossover",
    )
    state.update_submission_intent(intent["intent_id"], status=INTENT_SUBMITTING)
    broker.orders_by_client_id[intent["client_order_id"]] = _order(
        intent["client_order_id"], status="new", qty=10
    )
    result = om.execute_signal(
        ticker="AAPL",
        signal=BUY_SIGNAL,
        current_price=100.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    assert result["status"] == "blocked"
    assert "in flight" in result["reason"]
    assert broker.submit_calls == []
    assert state.get_submission_intent(intent["intent_id"])["status"] == "submitted"
    assert state.get_open_positions() == {}


def test_restart_finds_partially_filled_order(om, broker, state):
    """The confirmed partial fill is booked (4 shares); the order is still
    live so the new decision is blocked."""
    intent = state.create_submission_intent(
        ticker="AAPL",
        side="BUY",
        qty=10,
        order_type="market",
        strategy_name="ma_crossover",
    )
    state.update_submission_intent(intent["intent_id"], status=INTENT_SUBMITTING)
    broker.orders_by_client_id[intent["client_order_id"]] = _order(
        intent["client_order_id"], status="partially_filled", qty=10, filled_qty=4.0
    )
    result = om.execute_signal(
        ticker="AAPL",
        signal=BUY_SIGNAL,
        current_price=100.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    assert result["status"] == "blocked"
    assert broker.submit_calls == []
    assert state.get_open_positions()["AAPL"]["qty"] == 4.0


def test_restart_finds_filled_order_books_it_then_treats_signal_as_new(
    om, broker, state
):
    """A fill that happened while the process was down is booked exactly
    once through its own intent; the old intent becomes terminal and does
    NOT stand in for the new decision (pre-repair it was returned as a
    "recovered" success, swallowing the new order)."""
    intent = state.create_submission_intent(
        ticker="AAPL",
        side="BUY",
        qty=10,
        order_type="market",
        strategy_name="ma_crossover",
    )
    state.update_submission_intent(intent["intent_id"], status=INTENT_SUBMITTING)
    broker.orders_by_client_id[intent["client_order_id"]] = _order(
        intent["client_order_id"], status="filled", qty=10
    )
    result = om.execute_signal(
        ticker="AAPL",
        signal=BUY_SIGNAL,
        current_price=100.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    old = state.get_submission_intent(intent["intent_id"])
    assert old["status"] == INTENT_FILLED
    assert old["filled_qty_applied"] == 10.0  # booked once, via its own intent
    assert result["status"] == "success"
    # The FakeBroker fills the new order (66 from the mocked sizer) instantly:
    # ledger = 10 (recovered fill) + 66 (new decision), nothing double-counted.
    assert state.get_open_positions()["AAPL"]["qty"] == 76.0
    assert result.get("recovered") is None
    assert len(broker.submit_calls) == 1  # the new decision's own order


def test_restart_finds_rejected_order_closes_it_without_booking(om, broker, state):
    intent = state.create_submission_intent(
        ticker="AAPL",
        side="BUY",
        qty=10,
        order_type="market",
        strategy_name="ma_crossover",
    )
    state.update_submission_intent(intent["intent_id"], status=INTENT_SUBMITTING)
    broker.orders_by_client_id[intent["client_order_id"]] = _order(
        intent["client_order_id"], status="rejected", qty=10
    )
    result = om.execute_signal(
        ticker="AAPL",
        signal=BUY_SIGNAL,
        current_price=100.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    saved = state.get_submission_intent(intent["intent_id"])
    assert saved["status"] == INTENT_REJECTED
    assert saved["filled_qty_applied"] == 0.0
    # Only the new decision's (instantly-filled) order is in the ledger.
    assert state.get_open_positions()["AAPL"]["qty"] == 66.0
    # The rejected order belonged to an earlier decision; this signal is a
    # new decision with its own intent.
    assert result["status"] == "success"
    assert len(broker.submit_calls) == 1
    assert broker.submit_calls[0] != intent["client_order_id"]


def test_restart_finds_cancelled_order_closes_it_without_booking(om, broker, state):
    intent = state.create_submission_intent(
        ticker="AAPL",
        side="BUY",
        qty=10,
        order_type="market",
        strategy_name="ma_crossover",
    )
    state.update_submission_intent(intent["intent_id"], status=INTENT_SUBMITTING)
    broker.orders_by_client_id[intent["client_order_id"]] = _order(
        intent["client_order_id"], status="canceled", qty=10
    )
    om.execute_signal(
        ticker="AAPL",
        signal=BUY_SIGNAL,
        current_price=100.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    assert state.get_submission_intent(intent["intent_id"])["status"] == "cancelled"
    assert state.get_open_positions()["AAPL"]["qty"] == 66.0
    assert len(broker.submit_calls) == 1


# ---------------------------------------------------------------------------
# Uncertain outcomes: quarantine, never silently resubmit, never convert
# unknown into definite failure.
# ---------------------------------------------------------------------------


def test_broker_lookup_temporarily_unavailable_quarantines(om, broker, state):
    intent = state.create_submission_intent(
        ticker="AAPL",
        side="BUY",
        qty=10,
        order_type="market",
        strategy_name="ma_crossover",
    )
    state.update_submission_intent(intent["intent_id"], status=INTENT_SUBMITTING)
    broker.lookup_side_effect = lambda coid: (_ for _ in ()).throw(
        BrokerError("network unavailable")
    )

    result = om.execute_signal(
        ticker="AAPL",
        signal=BUY_SIGNAL,
        current_price=100.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )

    assert result["status"] == "blocked"
    assert broker.submit_calls == []
    assert (
        state.get_submission_intent(intent["intent_id"])["status"] == INTENT_UNCERTAIN
    )


def test_broker_contradictory_status_quarantines(om, broker, state):
    """An unrecognized broker status string must never be guessed into a
    known state - quarantine instead."""
    intent = state.create_submission_intent(
        ticker="AAPL",
        side="BUY",
        qty=10,
        order_type="market",
        strategy_name="ma_crossover",
    )
    state.update_submission_intent(intent["intent_id"], status=INTENT_SUBMITTING)
    broker.orders_by_client_id[intent["client_order_id"]] = _order(
        intent["client_order_id"], status="some_unmapped_future_status", qty=10
    )

    result = om.execute_signal(
        ticker="AAPL",
        signal=BUY_SIGNAL,
        current_price=100.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )

    assert result["status"] == "blocked"
    assert (
        state.get_submission_intent(intent["intent_id"])["status"] == INTENT_UNCERTAIN
    )


def test_ambiguous_submit_failure_quarantines_not_resubmits(om, broker, state):
    """The broker "accepted but the client lost the response" case: the
    submit call itself raises a generic (non-definite-rejection) exception
    after retries exhausted. The intent must go to 'uncertain', not
    'rejected' - we don't actually know it failed."""

    def _raise(_coid):
        raise ConnectionError("connection reset")

    broker.submit_side_effect = _raise

    result = om.execute_signal(
        ticker="AAPL",
        signal=BUY_SIGNAL,
        current_price=100.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )

    assert result["status"] == "error"
    intents = state.list_all_intents()
    assert len(intents) == 1
    assert intents[0]["status"] == INTENT_UNCERTAIN
    calls_after_first_attempt = len(broker.submit_calls)
    assert calls_after_first_attempt >= 1  # retried internally, all raised

    # A second top-level call for the same ticker/side must NOT place a new
    # order while quarantined - even though the broker never actually got
    # it in this fake (submit_side_effect always raises), the point is the
    # decision path: it must reconcile first, not blindly retry-create.
    broker.submit_side_effect = None
    result2 = om.execute_signal(
        ticker="AAPL",
        signal=BUY_SIGNAL,
        current_price=100.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    # Broker has no record for that client_order_id (submit_side_effect
    # never actually stored an order) -> reconciliation finds nothing and
    # status wasn't "prepared" -> quarantines again, rather than treating
    # "not found" as "safe to retry".
    assert result2["status"] == "blocked"
    assert len(broker.submit_calls) == calls_after_first_attempt  # no new submit
    assert len(state.list_all_intents()) == 1


# ---------------------------------------------------------------------------
# Logical-order identity: distinct decisions get distinct intents; one
# logical intent never creates two accepted broker orders.
# ---------------------------------------------------------------------------


def test_two_legitimate_signals_get_distinct_intents(om, broker, state):
    result1 = om.execute_signal(
        ticker="AAPL",
        signal=BUY_SIGNAL,
        current_price=100.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    assert result1["status"] == "success"
    first_intent = state.list_all_intents()[0]
    # Reconcile it so it's no longer "open" (simulates the position being
    # closed and reopened later - a later signal should not be treated as
    # a retry of this one).
    state.update_submission_intent(first_intent["intent_id"], status=INTENT_RECONCILED)
    # Unrelated in-process 60s duplicate guard (OrderManager._check_recent_order)
    # would otherwise block a second same-ticker/side order within 60s -
    # clear it so this test isolates intent identity, not that guard.
    om._recent_order_index.clear()

    result2 = om.execute_signal(
        ticker="AAPL",
        signal=BUY_SIGNAL,
        current_price=101.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    assert result2["status"] == "success"
    intents = state.list_all_intents()
    assert len(intents) == 2
    assert intents[0]["client_order_id"] != intents[1]["client_order_id"]
    assert broker.submit_calls == [
        intents[0]["client_order_id"],
        intents[1]["client_order_id"],
    ]


def test_one_intent_never_creates_two_accepted_broker_orders(om, broker, state):
    """First submit reaches the broker and is accepted, but the client-side
    call raises (response lost). The intent is quarantined; the next cycle
    finds the live order by client_order_id and BLOCKS (order in flight)
    instead of submitting a second order."""
    call_count = {"n": 0}

    def _flaky_submit(client_order_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            broker.orders_by_client_id[client_order_id] = _order(
                client_order_id, status="new", qty=10
            )
        raise ConnectionError("response lost after broker accepted")

    broker.submit_side_effect = _flaky_submit
    result1 = om.execute_signal(
        ticker="AAPL",
        signal=BUY_SIGNAL,
        current_price=100.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    assert result1["status"] == "error"
    intent = state.list_all_intents()[0]
    assert intent["status"] == INTENT_UNCERTAIN
    calls_in_first_attempt = call_count["n"]

    broker.submit_side_effect = None
    om._recent_order_index.clear()
    result2 = om.execute_signal(
        ticker="AAPL",
        signal=BUY_SIGNAL,
        current_price=100.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    assert result2["status"] == "blocked"
    assert call_count["n"] == calls_in_first_attempt
    assert len(state.list_all_intents()) == 1
    assert state.get_submission_intent(intent["intent_id"])["status"] == "submitted"

    # Broker fills it later: the next sync books exactly 10 shares.
    broker.orders_by_client_id[intent["client_order_id"]] = _order(
        intent["client_order_id"], status="filled", qty=10
    )
    om.sync_open_intents()
    assert state.get_open_positions()["AAPL"]["qty"] == 10.0


def test_reconciliation_does_not_double_apply_a_fill(om, broker, state):
    """Reconciling an already-filled order repeatedly books it once."""
    intent = state.create_submission_intent(
        ticker="AAPL",
        side="BUY",
        qty=10,
        order_type="market",
        strategy_name="ma_crossover",
    )
    state.update_submission_intent(intent["intent_id"], status=INTENT_SUBMITTING)
    broker.orders_by_client_id[intent["client_order_id"]] = _order(
        intent["client_order_id"], status="filled", qty=10
    )

    r1 = om._reconcile_intent(state.get_open_intent("AAPL", "BUY"))
    assert r1["action"] == "filled"
    assert state.get_open_intent("AAPL", "BUY") is None
    r2 = om._reconcile_intent(state.get_submission_intent(intent["intent_id"]))
    assert r2["action"] == "filled"
    assert state.get_open_positions()["AAPL"]["qty"] == 10.0
    assert broker.submit_calls == []


def test_partial_fill_progressing_to_full_fill_does_not_double_count(om, broker, state):
    """Cumulative broker fills 4 -> 4 (duplicate) -> 2 (stale/out-of-order)
    -> 10 book exactly 10 shares in total."""
    intent = state.create_submission_intent(
        ticker="AAPL",
        side="BUY",
        qty=10,
        order_type="market",
        strategy_name="ma_crossover",
    )
    state.update_submission_intent(intent["intent_id"], status=INTENT_SUBMITTING)
    broker.orders_by_client_id[intent["client_order_id"]] = _order(
        intent["client_order_id"], status="partially_filled", qty=10, filled_qty=4.0
    )

    r1 = om._reconcile_intent(state.get_open_intent("AAPL", "BUY"))
    assert r1["action"] == "in_flight"
    assert state.get_open_positions()["AAPL"]["qty"] == 4.0
    om._reconcile_intent(state.get_open_intent("AAPL", "BUY"))  # duplicate report
    assert state.get_open_positions()["AAPL"]["qty"] == 4.0
    broker.orders_by_client_id[intent["client_order_id"]] = _order(
        intent["client_order_id"], status="partially_filled", qty=10, filled_qty=2.0
    )
    om._reconcile_intent(state.get_open_intent("AAPL", "BUY"))  # stale report
    assert state.get_open_positions()["AAPL"]["qty"] == 4.0
    broker.orders_by_client_id[intent["client_order_id"]] = _order(
        intent["client_order_id"], status="filled", qty=10, filled_qty=10.0
    )
    r2 = om._reconcile_intent(state.get_open_intent("AAPL", "BUY"))
    assert r2["action"] == "filled"
    assert state.get_open_positions()["AAPL"]["qty"] == 10.0
    assert broker.submit_calls == []


def test_position_drift_with_uncertain_intent_never_triggers_corrective_order(
    mock_risk_manager, sample_config, state
):
    """A quarantined ("uncertain") submission intent coexisting with
    detected broker/ledger position drift must never cause an automatic
    corrective order - _run_position_reconciliation (main.py) only ever
    halts trading (TRADING_PAUSED=true), it has no code path that calls
    execute_signal/close_position. Verified here structurally: the broker
    double records every call it receives, and none of them are order
    submissions during reconciliation."""
    from alphalive import main as main_module
    from unittest.mock import Mock

    broker = FakeBroker()
    # Ledger disagrees with broker (drift) - broker has no AAPL position,
    # but the ledger (via bot_state) still tracks one.
    state.record_position_open("AAPL", 10, 100.0)
    # Also leave an uncertain intent lying around for the same ticker.
    intent = state.create_submission_intent(
        ticker="AAPL",
        side="BUY",
        qty=10,
        order_type="market",
        strategy_name="ma_crossover",
    )
    state.update_submission_intent(intent["intent_id"], status=INTENT_UNCERTAIN)

    app_config = Mock(trading_paused=False)
    notifier = Mock()
    order_manager_map = {"AAPL": Mock()}

    # _run_position_reconciliation sets the real TRADING_PAUSED env var on
    # drift - patch.dict restores whatever it was afterward so this test
    # can't leak a stuck kill switch into every other test in the run.
    with patch.dict(os.environ, {}, clear=False):
        main_module._run_position_reconciliation(
            broker, order_manager_map, app_config, notifier, state
        )

    assert app_config.trading_paused is True  # halted
    assert broker.submit_calls == []  # never placed a corrective order
    order_manager_map["AAPL"].execute_signal.assert_not_called()
    order_manager_map["AAPL"].close_position.assert_not_called()
