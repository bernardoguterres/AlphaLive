"""
Order Safety Scenarios (Priority 1 Regression Test Suite):
1. Partial fill -> cancellation request -> late fill before terminal state ->
   protective close waits while entry can still fill -> sizes from reconciled
   position (including late fill) -> exactly one correctly sized close -> no position reversal.
2. Restart while cancellation is pending -> recovery and idempotency -> no duplicate orders ->
   correct close sizing once terminal.
3. Overnight quarantine durability -> survives grace period (>900s) -> survives restart ->
   existing operator release path unblocks trading.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest

from alphalive.broker.base_broker import Order, Position
from alphalive.execution.order_manager import OrderManager
from alphalive.state import (
    BotState,
    INTENT_ABANDONED,
    INTENT_CANCELLED,
    INTENT_FILLED,
    INTENT_PARTIALLY_FILLED,
    INTENT_RECONCILED,
    INTENT_SUBMITTED,
    INTENT_SUBMITTING,
    INTENT_UNCERTAIN,
)
from tests.test_order_manager import sample_config, mock_risk_manager

ET = ZoneInfo("America/New_York")


class OrderSafetyFakeBroker:
    """Fake broker designed specifically for lifecycle testing with fine-grained
    control over order status (new -> partially_filled -> pending_cancel -> canceled),
    fills during pending_cancel, and positions."""

    def __init__(self):
        self.orders = {}  # client_order_id -> Order
        self.positions = {}  # symbol -> qty
        self.submit_calls = []  # [(symbol, qty, side, client_order_id)]
        self.cancel_calls = []  # [order_id]
        self.auto_cancel_on_delete = False

    def place_market_order(self, symbol, qty, side, client_order_id):
        self.submit_calls.append((symbol, float(qty), side.lower(), client_order_id))
        order = Order(
            id=f"broker_{client_order_id}",
            symbol=symbol,
            qty=float(qty),
            side=side.lower(),
            order_type="market",
            limit_price=None,
            status="new",
            filled_qty=0.0,
            filled_avg_price=None,
            submitted_at=datetime.now(ET),
            filled_at=None,
        )
        self.orders[client_order_id] = order
        return order

    def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)
        for coid, o in self.orders.items():
            if o.id == order_id:
                if self.auto_cancel_on_delete:
                    self._update_order(coid, status="canceled")
                else:
                    self._update_order(coid, status="pending_cancel")
                return True
        return False

    def get_order_by_client_id(self, client_order_id):
        return self.orders.get(client_order_id)

    def get_position(self, symbol):
        qty = self.positions.get(symbol, 0.0)
        if qty <= 1e-9:
            return None
        return Position(
            symbol=symbol,
            qty=qty,
            side="long",
            avg_entry_price=100.0,
            current_price=100.0,
            unrealized_pl=0.0,
            unrealized_plpc=0.0,
            market_value=qty * 100.0,
        )

    def get_all_positions(self):
        return [self.get_position(s) for s in self.positions if self.positions[s] > 1e-9]

    def _update_order(self, client_order_id, **fields):
        o = self.orders[client_order_id]
        d = {
            "id": o.id,
            "symbol": o.symbol,
            "qty": o.qty,
            "side": o.side,
            "order_type": o.order_type,
            "limit_price": o.limit_price,
            "status": fields.get("status", o.status),
            "filled_qty": fields.get("filled_qty", o.filled_qty),
            "filled_avg_price": fields.get("filled_avg_price", o.filled_avg_price),
            "submitted_at": o.submitted_at,
            "filled_at": fields.get("filled_at", o.filled_at),
        }
        self.orders[client_order_id] = Order(**d)

    def apply_fill(self, client_order_id, cumulative_qty, status="partially_filled", price=100.0):
        o = self.orders[client_order_id]
        prev_filled = o.filled_qty or 0.0
        delta = cumulative_qty - prev_filled
        if delta > 0:
            if o.side == "buy":
                self.positions[o.symbol] = self.positions.get(o.symbol, 0.0) + delta
            else:
                self.positions[o.symbol] = max(0.0, self.positions.get(o.symbol, 0.0) - delta)
        self._update_order(
            client_order_id,
            filled_qty=cumulative_qty,
            filled_avg_price=price,
            status=status,
            filled_at=datetime.now(ET) if status == "filled" else None,
        )


@pytest.fixture
def broker():
    return OrderSafetyFakeBroker()


@pytest.fixture
def state(tmp_path):
    return BotState(state_file=str(tmp_path / "state.json"))


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


def test_entry_partial_fill_cancellation_late_fill_protective_close(om, broker, state):
    """Priority 1:
    - Entry partially fills (40 of 100 shares).
    - Receives cancellation request (status -> pending_cancel).
    - Fills again before terminal state (20 more shares, total 60).
    - Protective close is called while entry can still fill:
        Verify it WAITS (status='pending') and submits NO close order.
    - Broker reports terminal state (canceled).
    - Protective close is called again:
        Verify it sizes from reconciled position including late fill (60 shares).
        Verify exactly ONE close order is submitted.
        Verify position is completely closed (0.0) with no position reversal.
    """
    # 1. Submit entry BUY order for 100 shares
    res = om.execute_signal(
        "AAPL",
        {"signal": "BUY", "reason": "momentum_entry"},
        current_price=100.0,
        account_equity=100_000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    assert res["status"] == "success"
    buy_coid = broker.submit_calls[0][3]
    entry_intent_id = state.list_all_intents()[-1]["intent_id"]

    # 2. Broker partially fills 40 shares
    broker.apply_fill(buy_coid, cumulative_qty=40.0, status="partially_filled")
    om.sync_open_intents()

    assert state.get_open_positions()["AAPL"]["qty"] == 40.0
    assert broker.get_position("AAPL").qty == 40.0

    # 3. Cancellation request received at broker -> order transitions to pending_cancel
    broker._update_order(buy_coid, status="pending_cancel")

    # 4. Late fill occurs before broker reports terminal state: 20 more shares fill!
    broker.apply_fill(buy_coid, cumulative_qty=60.0, status="pending_cancel")
    assert broker.get_position("AAPL").qty == 60.0

    # 5. Protective close is triggered while entry is in pending_cancel
    close_attempt_1 = om.close_position("AAPL", reason="protective_stop_loss")

    # VERIFICATION: Protective close must WAIT while entry can still fill
    assert close_attempt_1["status"] == "pending", (
        f"Expected protective close to wait with status='pending', got {close_attempt_1['status']}"
    )
    assert "waiting for entry order to cancel" in close_attempt_1["reason"]

    # Late fill must already be reconciled into local position
    assert state.get_open_positions()["AAPL"]["qty"] == 60.0

    # Verify NO close order was placed at the broker yet!
    close_orders = [c for c in broker.submit_calls if c[2] == "sell"]
    assert len(close_orders) == 0, f"Premature close order placed while entry working: {close_orders}"

    # 6. Broker finally reports terminal state (canceled)
    broker._update_order(buy_coid, status="canceled")

    # 7. Protective close is called again after terminal cancellation
    close_attempt_2 = om.close_position("AAPL", reason="protective_stop_loss")

    # VERIFICATION: Exactly ONE close order placed
    close_orders = [c for c in broker.submit_calls if c[2] == "sell"]
    assert len(close_orders) == 1, f"Expected exactly 1 close order, found {len(close_orders)}"

    sym, close_qty, side, close_coid = close_orders[0]
    # Sizing verification: Expected 60.0 (reconciled 40 + 20 late fill), Actual close_qty
    assert close_qty == 60.0, f"Expected close qty 60.0, got {close_qty}"
    assert side == "sell"

    # Fill the close order at broker
    broker.apply_fill(close_coid, cumulative_qty=60.0, status="filled")
    om.sync_open_intents()

    # Position verification: Exactly flat, no reversal
    final_broker_pos = broker.get_position("AAPL")
    assert final_broker_pos is None or final_broker_pos.qty == 0.0
    final_state_pos = state.get_open_positions().get("AAPL", {}).get("qty", 0.0)
    assert final_state_pos == 0.0


def test_restart_while_cancellation_is_pending_recovery_and_idempotency(broker, tmp_path, mock_risk_manager, sample_config):
    """Priority 1: Exercise process restart while entry cancellation is pending:
    - Order is in pending_cancel with late fill.
    - Process dies and restarts from disk state.
    - Startup sync reconciles without duplicating entry order.
    - Protective close waits while cancellation pending.
    - Protective close closes exact position once canceled.
    """
    state_file = str(tmp_path / "state_restart.json")
    state1 = BotState(state_file=state_file)
    om1 = OrderManager(
        broker=broker,
        risk_manager=mock_risk_manager,
        config=sample_config,
        notifier=None,
        dry_run=False,
        state=state1,
    )

    # 1. Entry order submitted and partially filled (40 shares)
    om1.execute_signal(
        "AAPL",
        {"signal": "BUY", "reason": "entry"},
        current_price=100.0,
        account_equity=100_000.0,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    buy_coid = broker.submit_calls[0][3]
    broker.apply_fill(buy_coid, cumulative_qty=40.0, status="partially_filled")
    om1.sync_open_intents()

    # 2. Cancellation pending + late fill (20 shares -> 60 total)
    broker._update_order(buy_coid, status="pending_cancel")
    broker.apply_fill(buy_coid, cumulative_qty=60.0, status="pending_cancel")

    # 3. Crash process: state1 persisted, om1 deleted
    state1.save()
    del om1, state1

    # 4. Restart process: fresh state2 and om2 from disk
    state2 = BotState(state_file=state_file)
    om2 = OrderManager(
        broker=broker,
        risk_manager=mock_risk_manager,
        config=sample_config,
        notifier=None,
        dry_run=False,
        state=state2,
    )

    # Startup sync: must reconcile the pending_cancel order and book the late fill
    om2.sync_open_intents()

    # IDEMPOTENCY: No new BUY order submitted to broker
    buy_orders = [c for c in broker.submit_calls if c[2] == "buy"]
    assert len(buy_orders) == 1, f"Expected exactly 1 BUY order across restart, found {len(buy_orders)}"

    # Reconciled position includes the late fill
    assert state2.get_open_positions()["AAPL"]["qty"] == 60.0

    # Protective close while still pending_cancel must wait
    res_pending = om2.close_position("AAPL", reason="protective_stop")
    assert res_pending["status"] == "pending"
    assert len([c for c in broker.submit_calls if c[2] == "sell"]) == 0

    # Broker completes cancellation
    broker._update_order(buy_coid, status="canceled")

    # Protective close proceeds
    res_close = om2.close_position("AAPL", reason="protective_stop")
    sell_orders = [c for c in broker.submit_calls if c[2] == "sell"]
    assert len(sell_orders) == 1
    assert sell_orders[0][1] == 60.0  # exact quantity 60.0

    # Fill close
    broker.apply_fill(sell_orders[0][3], cumulative_qty=60.0, status="filled")
    om2.sync_open_intents()

    assert broker.get_position("AAPL") is None
    assert state2.get_open_positions().get("AAPL", {}).get("qty", 0.0) == 0.0


def test_quarantine_durability_across_grace_period_and_restart(broker, tmp_path, mock_risk_manager, sample_config):
    """Priority 1: Verify overnight quarantine change:
    - Position discrepancy quarantine survives elapsed grace period (>900s).
    - Survives process restart.
    - Existing operator release path unblocks trading.
    """
    state_file = str(tmp_path / "quarantine_state.json")
    state = BotState(state_file=state_file)
    om = OrderManager(
        broker=broker,
        risk_manager=mock_risk_manager,
        config=sample_config,
        notifier=None,
        dry_run=False,
        state=state,
    )

    # Create synthetic discrepancy quarantine intent (age 1500s > 900s grace)
    old_time = (datetime.now(ET) - timedelta(seconds=1500)).isoformat()
    q_intent = state.create_submission_intent(
        ticker="AAPL",
        side="CLOSE",
        qty=15.0,
        order_type="market",
        strategy_name="ma_crossover",
    )
    state.update_submission_intent(
        q_intent["intent_id"],
        status=INTENT_UNCERTAIN,
        is_quarantine=True,
        last_error="post-cancellation position (15.0) inconsistent with expected remainder (0.0)",
        not_found_since=old_time,
    )

    # 1. Verify quarantine survives grace period during sync
    sync_res = om.sync_open_intents()
    assert len(sync_res) == 1
    action = sync_res[0].get("action") or sync_res[0].get("outcome")
    assert action == "quarantine"

    saved = state.get_submission_intent(q_intent["intent_id"])
    assert saved["status"] == INTENT_UNCERTAIN
    assert saved["status"] != INTENT_ABANDONED

    # Close attempt must be BLOCKED by quarantine
    blocked_res = om.close_position("AAPL", reason="take_profit")
    assert blocked_res["status"] == "blocked"
    assert blocked_res["fill_status"] == "uncertain"

    # 2. Verify quarantine survives process restart
    state.save()
    del om, state

    state2 = BotState(state_file=state_file)
    om2 = OrderManager(
        broker=broker,
        risk_manager=mock_risk_manager,
        config=sample_config,
        notifier=None,
        dry_run=False,
        state=state2,
    )

    om2.sync_open_intents()
    restarted_intent = state2.get_submission_intent(q_intent["intent_id"])
    assert restarted_intent["status"] == INTENT_UNCERTAIN
    assert restarted_intent.get("is_quarantine") is True

    blocked_res2 = om2.close_position("AAPL", reason="take_profit")
    assert blocked_res2["status"] == "blocked"

    # 3. Existing Operator Release Path:
    # Operator inspects broker and resolves discrepancy by moving intent to INTENT_RECONCILED
    state2.update_submission_intent(
        q_intent["intent_id"],
        status=INTENT_RECONCILED,
        operator_note="manual broker audit confirmed position reconciled",
    )
    # Operator synchronizes ledger with confirmed broker position (flat)
    broker.positions.pop("AAPL", None)

    # Trading is now unblocked!
    released_res = om2.close_position("AAPL", reason="take_profit")
    assert released_res["status"] == "success"
    assert "already flat" in released_res["reason"]
