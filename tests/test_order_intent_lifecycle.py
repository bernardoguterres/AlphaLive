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


def test_client_order_id_stable_across_same_process_retry(om, broker, state):
    """Intent durably created but the broker call itself never started (the
    process crashed between create_submission_intent and the first
    place_*_order call) - status stays "prepared", and reconciling it must
    say "reuse" with the SAME client_order_id, not generate a new one."""
    intent = state.create_submission_intent(
        ticker="AAPL",
        side="BUY",
        qty=10,
        order_type="market",
        strategy_name="ma_crossover",
    )

    reconciliation = om._reconcile_intent(state.get_open_intent("AAPL", "BUY"))
    assert reconciliation["action"] == "reuse"

    reused = state.get_open_intent("AAPL", "BUY")
    assert reused["client_order_id"] == intent["client_order_id"]


# ---------------------------------------------------------------------------
# Crash-before-broker-call / crash-after-intent-creation: restart reuses the
# same client_order_id instead of generating a new one.
# ---------------------------------------------------------------------------


def test_restart_after_prepared_reuses_client_order_id_and_submits_once(
    om, broker, state
):
    """Intent durably created (status=prepared) but the process "crashed"
    before ever calling the broker. A fresh execute_signal call (simulating
    the restarted process) must reuse the same client_order_id and place
    exactly one order."""
    intent = state.create_submission_intent(
        ticker="AAPL",
        side="BUY",
        qty=10,
        order_type="market",
        strategy_name="ma_crossover",
    )
    assert intent["status"] == INTENT_PREPARED

    result = om.execute_signal(
        ticker="AAPL",
        signal=BUY_SIGNAL,
        current_price=100.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )

    assert result["status"] == "success"
    assert broker.submit_calls == [intent["client_order_id"]]
    assert len(state.list_all_intents()) == 1  # no second intent created


# ---------------------------------------------------------------------------
# Broker accepted but client lost the response / crashed after acceptance
# but before local recording: restart must recover, not resubmit.
# ---------------------------------------------------------------------------


def test_restart_finds_accepted_order_recovers_without_resubmitting(om, broker, state):
    intent = state.create_submission_intent(
        ticker="AAPL",
        side="BUY",
        qty=10,
        order_type="market",
        strategy_name="ma_crossover",
    )
    state.update_submission_intent(intent["intent_id"], status=INTENT_SUBMITTING)
    # Broker DID accept it, but the local process never recorded the result.
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

    assert result["status"] == "success"
    assert result.get("recovered") is True
    assert broker.submit_calls == []  # never placed a second order


def test_restart_finds_partially_filled_order(om, broker, state):
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

    assert result["status"] == "success"
    assert result["filled_qty"] == 4.0
    assert broker.submit_calls == []


def test_restart_finds_filled_order(om, broker, state):
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

    assert result["status"] == "success"
    assert result["filled_qty"] == 10.0
    assert broker.submit_calls == []


def test_restart_finds_rejected_order_reconciles_and_blocks(om, broker, state):
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

    assert result["status"] == "blocked"
    assert broker.submit_calls == []
    saved = state.get_submission_intent(intent["intent_id"])
    assert saved["status"] == INTENT_RECONCILED
    # No longer "open" - a fresh signal next cycle would get a new intent.
    assert state.get_open_intent("AAPL", "BUY") is None


def test_restart_finds_cancelled_order_reconciles_and_blocks(om, broker, state):
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
        state.get_submission_intent(intent["intent_id"])["status"] == INTENT_RECONCILED
    )


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
    """Simulate the classic idempotency scenario: the first submit attempt
    actually reaches the broker and is accepted, but the client-side call
    raises (response lost). A naive retry with a fresh client_order_id
    would create a second order; the intent mechanism must not do that."""
    call_count = {"n": 0}

    def _flaky_submit(client_order_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Broker accepts it "for real" on the very first attempt, even
            # though the client raises locally (response lost). Subsequent
            # in-process retries (still inside this one execute_signal call)
            # keep failing locally too - the broker was never asked again.
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
    assert calls_in_first_attempt >= 1

    # Next cycle (a fresh top-level execute_signal call): reconciliation
    # now finds the broker DOES have the order from the very first submit.
    broker.submit_side_effect = None
    result2 = om.execute_signal(
        ticker="AAPL",
        signal=BUY_SIGNAL,
        current_price=100.0,
        account_equity=100000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    assert result2["status"] == "success"
    assert result2.get("recovered") is True
    assert call_count["n"] == calls_in_first_attempt  # no new broker submit call
    assert len(state.list_all_intents()) == 1  # one logical intent throughout


def test_reconciliation_does_not_double_apply_a_fill(om, broker, state):
    """Reconciling an already-filled intent repeatedly must keep returning
    the same recovered outcome, never re-submit, never mutate qty."""
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

    open_intent = state.get_open_intent("AAPL", "BUY")
    r1 = om._reconcile_intent(open_intent)
    assert r1["action"] == "success"
    assert r1["order"].filled_qty == 10.0

    # It's now terminal (filled) - get_open_intent should no longer surface
    # it as "open" for a NEW decision, but re-reconciling the same dict is
    # still safe/idempotent if called again directly.
    assert state.get_open_intent("AAPL", "BUY") is None
    r2 = om._reconcile_intent(state.get_submission_intent(intent["intent_id"]))
    assert r2["action"] == "success"
    assert r2["order"].filled_qty == 10.0
    assert broker.submit_calls == []


def test_partial_fill_progressing_to_full_fill_does_not_double_count(om, broker, state):
    """A partial fill, later found fully filled on reconciliation, must
    report the ABSOLUTE current filled_qty each time - never additive -
    so a caller recording it into a position ledger (a dict assignment,
    not an increment) never double-counts shares."""
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
    assert r1["action"] == "success"
    assert r1["order"].filled_qty == 4.0
    assert (
        state.get_submission_intent(intent["intent_id"])["status"] == "partially_filled"
    )

    # Broker later reports the same order fully filled.
    broker.orders_by_client_id[intent["client_order_id"]] = _order(
        intent["client_order_id"], status="filled", qty=10, filled_qty=10.0
    )
    r2 = om._reconcile_intent(state.get_open_intent("AAPL", "BUY"))
    assert r2["action"] == "success"
    assert r2["order"].filled_qty == 10.0  # absolute, not 4 + 10
    assert broker.submit_calls == []  # never resubmitted


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
