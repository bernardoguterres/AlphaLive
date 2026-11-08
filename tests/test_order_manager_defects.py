"""
Regression tests for OrderManager defects:
1. Protective close racing an entry order in INTENT_SUBMITTING
2. Discrepancy quarantine intent being auto-abandoned after grace period
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest

from alphalive.execution.order_manager import OrderManager
from alphalive.state import (
    BotState,
    INTENT_ABANDONED,
    INTENT_FILLED,
    INTENT_PREPARED,
    INTENT_SUBMITTED,
    INTENT_SUBMITTING,
    INTENT_UNCERTAIN,
)
from alphalive.broker.base_broker import Order
from tests.test_order_manager import sample_config, mock_risk_manager

ET = ZoneInfo("America/New_York")


class FakeBroker:
    def __init__(self):
        self.orders_by_client_id = {}
        self.submit_calls = []
        self.cancelled_orders = []
        self.position = None

    def place_market_order(self, symbol, qty, side, client_order_id):
        self.submit_calls.append(client_order_id)
        order = Order(
            id=f"broker_{client_order_id}",
            symbol=symbol,
            qty=qty,
            side=side,
            order_type="market",
            limit_price=None,
            status="filled",
            filled_qty=qty,
            filled_avg_price=100.0,
            submitted_at=datetime.now(ET),
            filled_at=datetime.now(ET),
        )
        self.orders_by_client_id[client_order_id] = order
        return order

    def cancel_order(self, order_id):
        self.cancelled_orders.append(order_id)
        return True

    def get_order_by_client_id(self, client_order_id):
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


def test_close_position_defers_when_entry_in_submitting(om, broker, state):
    """If an entry BUY is currently in INTENT_SUBMITTING, _settle_working_orders_before_close
    must detect it and defer the close rather than racing a market SELL against it."""
    intent = state.create_submission_intent(
        ticker="AAPL",
        side="BUY",
        qty=10,
        order_type="market",
        strategy_name="ma_crossover",
    )
    state.update_submission_intent(intent["intent_id"], status=INTENT_SUBMITTING)

    # Calling _settle_working_orders_before_close should NOT return None.
    # It must detect the submitting intent and defer or wait.
    res = om._settle_working_orders_before_close("AAPL", "take_profit")
    assert res is not None, "Protective close proceeded while entry BUY was in INTENT_SUBMITTING"
    assert res["status"] == "pending"


def test_discrepancy_quarantine_intent_not_abandoned_after_grace(om, broker, state):
    """When a position discrepancy creates a quarantine intent (status=INTENT_UNCERTAIN with
    no broker order), it must NOT be abandoned after 15 minutes of grace. It must remain
    quarantined until explicitly resolved by an operator."""
    quarantine_intent = state.create_submission_intent(
        ticker="AAPL",
        side="CLOSE",
        qty=10.0,
        order_type="market",
        strategy_name="ma_crossover",
    )
    state.update_submission_intent(
        quarantine_intent["intent_id"],
        status=INTENT_UNCERTAIN,
        is_quarantine=True,
        last_error="post-cancellation position (10.0) inconsistent with expected remainder (0.0)",
        not_found_since=(datetime.now(ET) - timedelta(seconds=1200)).isoformat(),
    )

    # Reconciling this intent should NOT abandon it
    rec = om._reconcile_intent(state.get_submission_intent(quarantine_intent["intent_id"]))
    saved = state.get_submission_intent(quarantine_intent["intent_id"])

    assert rec["action"] == "quarantine"
    assert saved["status"] == INTENT_UNCERTAIN
    assert saved["status"] != INTENT_ABANDONED
