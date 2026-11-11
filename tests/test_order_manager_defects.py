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


def test_legacy_quarantine_without_flag_not_abandoned(om, state):
    """Quarantines persisted before is_quarantine existed are recognised by
    their last_error and must not be auto-abandoned either."""
    it = state.create_submission_intent(
        ticker="AAPL", side="CLOSE", qty=10.0, order_type="market", strategy_name="ma_crossover"
    )
    state.update_submission_intent(
        it["intent_id"],
        status=INTENT_UNCERTAIN,
        last_error="post-cancellation position (10.0) inconsistent with expected remainder (0.0)",
        not_found_since=(datetime.now(ET) - timedelta(seconds=1200)).isoformat(),
    )
    rec = om._reconcile_intent(state.get_submission_intent(it["intent_id"]))
    assert rec["action"] == "quarantine"
    assert state.get_submission_intent(it["intent_id"])["status"] == INTENT_UNCERTAIN


def test_operator_release_unblocks_ticker_and_requires_attribution(state):
    it = state.create_submission_intent(
        ticker="AAPL", side="CLOSE", qty=10.0, order_type="market", strategy_name="ma_crossover"
    )
    state.update_submission_intent(it["intent_id"], status=INTENT_UNCERTAIN, is_quarantine=True)
    assert not state.release_quarantine_intent(it["intent_id"], "", "checked")
    assert not state.release_quarantine_intent(it["intent_id"], "bg", " ")
    assert not state.release_quarantine_intent("nope", "bg", "checked")
    assert state.get_open_intent("AAPL", "CLOSE") is not None
    assert state.release_quarantine_intent(it["intent_id"], "bg", "verified at broker")
    assert state.get_open_intent("AAPL", "CLOSE") is None
    saved = state.get_submission_intent(it["intent_id"])
    assert saved["released_by"] == "bg" and saved["status"] == "reconciled"
    # a non-quarantine uncertain intent cannot be force-released
    other = state.create_submission_intent(
        ticker="MSFT", side="BUY", qty=1, order_type="market", strategy_name="ma_crossover"
    )
    state.update_submission_intent(other["intent_id"], status=INTENT_UNCERTAIN)
    assert not state.release_quarantine_intent(other["intent_id"], "bg", "x")


def test_release_script_lists_and_releases(tmp_path, capsys):
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "release_quarantine", pathlib.Path(__file__).parent.parent / "scripts" / "release_quarantine.py"
    )
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    sf = str(tmp_path / "s.json")
    st = BotState(state_file=sf)
    it = st.create_submission_intent(
        ticker="AAPL", side="CLOSE", qty=5.0, order_type="market", strategy_name="ma_crossover"
    )
    st.update_submission_intent(it["intent_id"], status=INTENT_UNCERTAIN, is_quarantine=True)
    assert mod.main(["--state-file", sf]) == 0
    assert "1 quarantined" in capsys.readouterr().out
    assert mod.main(["--state-file", sf, "--release", it["intent_id"]]) == 1
    assert mod.main(["--state-file", sf, "--release", it["intent_id"], "--operator", "bg", "--note", "ok"]) == 0
    assert BotState(state_file=sf).get_open_intent("AAPL", "CLOSE") is None


def _quarantined(state, ticker="AAPL"):
    it = state.create_submission_intent(
        ticker=ticker, side="CLOSE", qty=10.0, order_type="market", strategy_name="ma_crossover"
    )
    state.update_submission_intent(
        it["intent_id"],
        status=INTENT_UNCERTAIN,
        is_quarantine=True,
        last_error="post-cancellation position (10.0) inconsistent with expected remainder (0.0)",
        not_found_since=(datetime.now(ET) - timedelta(seconds=1200)).isoformat(),
    )
    return it["intent_id"]


def _om_for(state, broker, mock_risk_manager, sample_config):
    return OrderManager(
        broker=broker, risk_manager=mock_risk_manager, config=sample_config,
        notifier=None, dry_run=False, state=state,
    )


def test_quarantine_persists_across_restart_and_stays_quarantined(
    tmp_path, broker, mock_risk_manager, sample_config
):
    """Process 1 quarantines; process 2 (fresh BotState + OrderManager on the same
    file, past the grace period, broker knows nothing) must still hold it."""
    f = str(tmp_path / "restart.json")
    intent_id = _quarantined(BotState(state_file=f))

    state2 = BotState(state_file=f)  # simulated restart
    reloaded = state2.get_submission_intent(intent_id)
    assert reloaded["status"] == INTENT_UNCERTAIN and reloaded["is_quarantine"] is True
    assert state2.get_open_intent("AAPL", "CLOSE") is not None

    om2 = _om_for(state2, broker, mock_risk_manager, sample_config)
    results = om2.sync_open_intents()
    assert [(r.get("action") or r.get("outcome")) for r in results if r["intent_id"] == intent_id] == ["quarantine"]
    assert BotState(state_file=f).get_submission_intent(intent_id)["status"] == INTENT_UNCERTAIN
    assert broker.submit_calls == []  # nothing resubmitted


def test_operator_release_is_idempotent_and_survives_restart(
    tmp_path, broker, mock_risk_manager, sample_config
):
    f = str(tmp_path / "release.json")
    state = BotState(state_file=f)
    intent_id = _quarantined(state)
    assert state.release_quarantine_intent(intent_id, "bg", "verified at broker")
    first = state.get_submission_intent(intent_id)

    assert not state.release_quarantine_intent(intent_id, "someone-else", "again")
    again = state.get_submission_intent(intent_id)
    assert (again["released_by"], again["released_at"]) == (first["released_by"], first["released_at"])

    state2 = BotState(state_file=f)  # restart after release
    assert state2.get_submission_intent(intent_id)["status"] == "reconciled"
    assert state2.get_open_intent("AAPL", "CLOSE") is None
    om2 = _om_for(state2, broker, mock_risk_manager, sample_config)
    assert [r for r in om2.sync_open_intents() if r["intent_id"] == intent_id] == []
    assert not state2.release_quarantine_intent(intent_id, "bg", "third time")
