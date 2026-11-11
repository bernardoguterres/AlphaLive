"""
Tests for SignalEngine <-> Position Ledger State Synchronization.

Verifies:
1. Blocked entries (AlphaSignal sentiment gate, Global Risk, RiskManager limits,
   position sizing) leave `_in_position == False` both in memory and on disk.
2. Broker rejections (403 insufficient margin/buying power, 422 unprocessable, etc.)
   leave `_in_position == False`.
3. Restarts restore `_in_position` from actual durable ledger, discarding phantom
   states and rebuilding missing in-position states from ledger positions.
4. Continuous synchronization via `_sync_engines_with_ledger` and `sync_position`
   preserves working orders in flight while strictly aligning long/flat state
   with broker-confirmed positions.
"""

import json
import os
import tempfile
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from alpaca.common.exceptions import APIError

from alphalive import main as main_module
from alphalive.broker.base_broker import Account, Order, Position
from alphalive.execution.order_manager import OrderManager
from alphalive.execution.risk_manager import GlobalRiskManager, RiskManager
from alphalive.state import (
    INTENT_CANCELLED,
    INTENT_FILLED,
    INTENT_REJECTED,
    INTENT_SUBMITTED,
    BotState,
)
from alphalive.strategy.signal_engine import SignalEngine
from alphalive.strategy_schema import StrategySchema

ET = ZoneInfo("America/New_York")


# =============================================================================
# Test Fixtures & Helpers
# =============================================================================


@pytest.fixture
def temp_state_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("{}")
        path = f.name
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _make_strategy_config(strategy_name: str, params: dict, ticker: str = "AAPL") -> StrategySchema:
    return StrategySchema(
        schema_version="1.0",
        strategy={
            "name": strategy_name,
            "parameters": params,
            "description": f"Test {strategy_name}",
        },
        ticker=ticker,
        timeframe="1Day",
        risk={
            "stop_loss_pct": 2.0,
            "take_profit_pct": 5.0,
            "max_position_size_pct": 25.0,
            "max_daily_loss_pct": 5.0,
            "max_open_positions": 5,
            "portfolio_max_positions": 10,
            "trailing_stop_enabled": False,
            "trailing_stop_pct": 3.0,
            "commission_per_trade": 0.0,
            "drawdown_halt_enabled": False,
        },
        execution={
            "order_type": "market",
            "limit_offset_pct": 0.1,
            "cooldown_bars": 0,
        },
        safety_limits={
            "max_trades_per_day": 20,
            "circuit_breaker_drawdown_pct": 10.0,
            "max_loss_per_trade_pct": 3.0,
        },
        metadata={
            "exported_from": "AlphaLab",
            "exported_at": "2026-03-08T10:00:00Z",
            "alphalab_version": "0.2.0",
            "backtest_id": "bt_test_001",
            "backtest_period": {"start": "2020-01-01", "end": "2024-12-31"},
            "performance": {
                "sharpe_ratio": 1.5,
                "sortino_ratio": 1.8,
                "total_return_pct": 30.0,
                "max_drawdown_pct": -10.0,
                "win_rate_pct": 55.0,
                "profit_factor": 1.5,
                "total_trades": 50,
                "calmar_ratio": 2.0,
            },
        },
    )


def _bollinger_rsi_buy_bars(n: int = 50) -> pd.DataFrame:
    """Generate OHLCV dataframe where the last bar breaches lower BB and RSI < 45

    without exceeding the overnight 20% split-detection threshold.
    """
    prices = [100.0 - i * 0.5 for i in range(n - 1)] + [71.0]
    df = pd.DataFrame(
        {
            "open": prices,
            "high": [p + 0.5 for p in prices],
            "low": [p - 0.5 for p in prices],
            "close": prices,
            "volume": [1_000_000] * n,
        },
        index=pd.date_range("2024-01-01", periods=n, freq="D", tz="America/New_York"),
    )
    return df


def _mock_api_error(message: str, status_code: int) -> APIError:
    http_error = Mock()
    http_error.response = Mock(status_code=status_code)
    return APIError(message, http_error=http_error)


def _setup_test_environment(temp_state_file, strategy_name="bollinger_rsi_combo", params=None):
    if params is None:
        params = {
            "bb_period": 20,
            "rsi_period": 14,
            "rsi_oversold": 45,
            "rsi_overbought": 55,
            "exit_at_middle": True,
        }
    strat_cfg = _make_strategy_config(strategy_name, params, ticker="AAPL")
    bot_state = BotState(state_file=temp_state_file)
    engine = SignalEngine(strat_cfg)
    mock_broker = Mock()
    mock_broker.get_account.return_value = Account(
        equity=100000.0,
        cash=50000.0,
        buying_power=200000.0,
        portfolio_value=100000.0,
        long_market_value=0.0,
        short_market_value=0.0,
        daytrade_count=0,
        pattern_day_trader=False,
    )
    mock_broker.get_position.return_value = None
    mock_broker.get_all_positions.return_value = []

    risk_mgr = RiskManager(
        risk_config=strat_cfg.risk,
        execution_config=strat_cfg.execution,
        strategy_name=strat_cfg.strategy.name,
        safety_limits=strat_cfg.safety_limits,
        notifier=None,
    )
    global_risk = GlobalRiskManager()
    order_mgr = OrderManager(
        broker=mock_broker,
        risk_manager=risk_mgr,
        config=strat_cfg,
        notifier=None,
        dry_run=False,
        state=bot_state,
    )

    market_data = Mock()
    df = _bollinger_rsi_buy_bars()
    market_data.get_latest_bars.return_value = df
    market_data.get_current_price.return_value = float(df["close"].iloc[-1])

    return {
        "strat_cfg": strat_cfg,
        "bot_state": bot_state,
        "engine": engine,
        "broker": mock_broker,
        "risk_mgr": risk_mgr,
        "global_risk": global_risk,
        "order_mgr": order_mgr,
        "market_data": market_data,
        "df": df,
    }


# =============================================================================
# 1. Blocked Entries Leave Engine Flat
# =============================================================================


class TestBlockedEntriesLeaveEngineFlat:
    """Assert that if a strategy emits BUY but execution is blocked, the engine

    and durable state remain strictly flat (_in_position == False).
    """

    def test_alphasignal_gate_breach_leaves_engine_flat(self, temp_state_file, monkeypatch):
        # Disable bear market filter so raw strategy BUY reaches the gate
        monkeypatch.setenv("ENABLE_BEAR_MARKET_FILTER", "false")
        env = _setup_test_environment(temp_state_file)

        # Mock AlphaSignal client to block the entry
        mock_alphasignal = Mock()
        mock_alphasignal.is_execution_allowed = AsyncMock(
            return_value=(
                False,
                {
                    "sentiment_score": -0.85,
                    "confidence": 0.90,
                    "gate_status": "sentiment_breach",
                    "fail_policy": "closed",
                },
            )
        )

        now_et = datetime(2024, 1, 2, 9, 35, tzinfo=ET)
        main_module._check_signal_for_strategy(
            strat_cfg=env["strat_cfg"],
            now_et=now_et,
            morning_checks_done=set(),
            last_signal_check_map={},
            market_data=env["market_data"],
            signal_engine_map={env["strat_cfg"].ticker: env["engine"]},
            order_manager_map={env["strat_cfg"].ticker: env["order_mgr"]},
            alphasignal_client=mock_alphasignal,
            broker=env["broker"],
            notifier=Mock(),
            global_risk=env["global_risk"],
            bot_state=env["bot_state"],
        )

        # Assert engine is flat in memory
        assert env["engine"]._in_position is False, (
            "Phantom position lockout: engine._in_position must be False when blocked by sentiment"
        )
        assert env["engine"]._entry_price == 0.0

        # Assert durable persisted state is flat on disk
        persisted = env["bot_state"].get_engine_state(env["strat_cfg"].ticker)
        assert persisted is not None
        assert persisted["in_position"] is False
        assert persisted["entry_price"] == 0.0

        # Assert no broker orders or submission intents were placed
        env["broker"].place_market_order.assert_not_called()
        assert len(env["bot_state"].list_open_intents()) == 0
        assert env["bot_state"].get_open_positions() == {}

    def test_global_risk_daily_loss_limit_leaves_engine_flat(self, temp_state_file, monkeypatch):
        monkeypatch.setenv("ENABLE_BEAR_MARKET_FILTER", "false")
        env = _setup_test_environment(temp_state_file)

        # Latch global daily loss limit breach
        env["global_risk"].global_daily_stats["strategies_halted"] = True
        env["global_risk"].global_daily_stats["halt_reason"] = "Global daily loss limit hit" 

        now_et = datetime(2024, 1, 2, 9, 35, tzinfo=ET)
        main_module._check_signal_for_strategy(
            strat_cfg=env["strat_cfg"],
            now_et=now_et,
            morning_checks_done=set(),
            last_signal_check_map={},
            market_data=env["market_data"],
            signal_engine_map={env["strat_cfg"].ticker: env["engine"]},
            order_manager_map={env["strat_cfg"].ticker: env["order_mgr"]},
            alphasignal_client=None,
            broker=env["broker"],
            notifier=Mock(),
            global_risk=env["global_risk"],
            bot_state=env["bot_state"],
        )

        assert env["engine"]._in_position is False
        assert env["engine"]._entry_price == 0.0
        persisted = env["bot_state"].get_engine_state(env["strat_cfg"].ticker)
        assert persisted["in_position"] is False
        env["broker"].place_market_order.assert_not_called()

    def test_risk_manager_can_trade_rejection_leaves_engine_flat(
        self, temp_state_file, monkeypatch
    ):
        monkeypatch.setenv("ENABLE_BEAR_MARKET_FILTER", "false")
        env = _setup_test_environment(temp_state_file)

        # Block trading via RiskManager
        env["risk_mgr"].trading_paused_manual = True

        now_et = datetime(2024, 1, 2, 9, 35, tzinfo=ET)
        main_module._check_signal_for_strategy(
            strat_cfg=env["strat_cfg"],
            now_et=now_et,
            morning_checks_done=set(),
            last_signal_check_map={},
            market_data=env["market_data"],
            signal_engine_map={env["strat_cfg"].ticker: env["engine"]},
            order_manager_map={env["strat_cfg"].ticker: env["order_mgr"]},
            alphasignal_client=None,
            broker=env["broker"],
            notifier=Mock(),
            global_risk=env["global_risk"],
            bot_state=env["bot_state"],
        )

        assert env["engine"]._in_position is False
        persisted = env["bot_state"].get_engine_state(env["strat_cfg"].ticker)
        assert persisted["in_position"] is False
        env["broker"].place_market_order.assert_not_called()

    def test_zero_position_size_leaves_engine_flat(self, temp_state_file, monkeypatch):
        monkeypatch.setenv("ENABLE_BEAR_MARKET_FILTER", "false")
        env = _setup_test_environment(temp_state_file)

        # Force calculate_position_size to return 0
        env["risk_mgr"].calculate_position_size = Mock(return_value=0)

        now_et = datetime(2024, 1, 2, 9, 35, tzinfo=ET)
        main_module._check_signal_for_strategy(
            strat_cfg=env["strat_cfg"],
            now_et=now_et,
            morning_checks_done=set(),
            last_signal_check_map={},
            market_data=env["market_data"],
            signal_engine_map={env["strat_cfg"].ticker: env["engine"]},
            order_manager_map={env["strat_cfg"].ticker: env["order_mgr"]},
            alphasignal_client=None,
            broker=env["broker"],
            notifier=Mock(),
            global_risk=env["global_risk"],
            bot_state=env["bot_state"],
        )

        assert env["engine"]._in_position is False
        persisted = env["bot_state"].get_engine_state(env["strat_cfg"].ticker)
        assert persisted["in_position"] is False
        env["broker"].place_market_order.assert_not_called()

    def test_no_phantom_position_lockout_after_blocked_entry(self, temp_state_file, monkeypatch):
        """Verify that after an entry is blocked, the engine is NOT locked out

        and can generate a new BUY signal on a subsequent bar when gate clears.
        """
        monkeypatch.setenv("ENABLE_BEAR_MARKET_FILTER", "false")
        env = _setup_test_environment(temp_state_file)

        # Bar 1: Blocked by sentiment gate
        mock_alphasignal = Mock()
        mock_alphasignal.is_execution_allowed = AsyncMock(
            return_value=(
                False,
                {"sentiment_score": -0.85, "gate_status": "sentiment_breach", "fail_policy": "closed"},
            )
        )

        now_et = datetime(2024, 1, 2, 9, 35, tzinfo=ET)
        main_module._check_signal_for_strategy(
            strat_cfg=env["strat_cfg"],
            now_et=now_et,
            morning_checks_done=set(),
            last_signal_check_map={},
            market_data=env["market_data"],
            signal_engine_map={env["strat_cfg"].ticker: env["engine"]},
            order_manager_map={env["strat_cfg"].ticker: env["order_mgr"]},
            alphasignal_client=mock_alphasignal,
            broker=env["broker"],
            notifier=Mock(),
            global_risk=env["global_risk"],
            bot_state=env["bot_state"],
        )

        assert env["engine"]._in_position is False

        # Bar 2: Sentiment clears (allowed=True), engine must emit BUY again (not locked out!)
        mock_alphasignal.is_execution_allowed = AsyncMock(
            return_value=(
                True,
                {"sentiment_score": 0.85, "gate_status": "allowed", "fail_policy": "open"},
            )
        )
        # Mock broker to return accepted order
        accepted_order = Order(
            id="order_success_1",
            symbol="AAPL",
            qty=10,
            side="buy",
            order_type="market",
            limit_price=None,
            status="filled",
            filled_qty=10,
            filled_avg_price=70.0,
            submitted_at=datetime.now(ET),
            filled_at=datetime.now(ET),
        )
        env["broker"].place_market_order.return_value = accepted_order

        morning_checks = set()
        next_day = datetime(2024, 1, 3, 9, 35, tzinfo=ET)
        main_module._check_signal_for_strategy(
            strat_cfg=env["strat_cfg"],
            now_et=next_day,
            morning_checks_done=morning_checks,
            last_signal_check_map={},
            market_data=env["market_data"],
            signal_engine_map={env["strat_cfg"].ticker: env["engine"]},
            order_manager_map={env["strat_cfg"].ticker: env["order_mgr"]},
            alphasignal_client=mock_alphasignal,
            broker=env["broker"],
            notifier=Mock(),
            global_risk=env["global_risk"],
            bot_state=env["bot_state"],
        )

        # Engine successfully entered position on Bar 2
        assert env["engine"]._in_position is True
        assert env["broker"].place_market_order.called


# =============================================================================
# 2. Broker Rejections Leave Engine Flat
# =============================================================================


class TestBrokerRejectionsLeaveEngineFlat:
    """Assert that if a broker rejects an order (403, 422, etc.), the engine

    and durable state remain strictly flat (_in_position == False).
    """

    def test_broker_403_insufficient_margin_leaves_engine_flat(
        self, temp_state_file, monkeypatch
    ):
        monkeypatch.setenv("ENABLE_BEAR_MARKET_FILTER", "false")
        env = _setup_test_environment(temp_state_file)

        # Broker rejects order with 403 Forbidden (e.g. insufficient buying power/margin)
        err = _mock_api_error("insufficient margin / buying power", 403)
        env["broker"].place_market_order.side_effect = err

        now_et = datetime(2024, 1, 2, 9, 35, tzinfo=ET)
        main_module._check_signal_for_strategy(
            strat_cfg=env["strat_cfg"],
            now_et=now_et,
            morning_checks_done=set(),
            last_signal_check_map={},
            market_data=env["market_data"],
            signal_engine_map={env["strat_cfg"].ticker: env["engine"]},
            order_manager_map={env["strat_cfg"].ticker: env["order_mgr"]},
            alphasignal_client=None,
            broker=env["broker"],
            notifier=Mock(),
            global_risk=env["global_risk"],
            bot_state=env["bot_state"],
        )

        # Engine MUST be flat in memory
        assert env["engine"]._in_position is False, (
            "Phantom position lockout: broker 403 rejection must leave engine._in_position == False"
        )
        assert env["engine"]._entry_price == 0.0

        # Persisted state MUST be flat on disk
        persisted = env["bot_state"].get_engine_state(env["strat_cfg"].ticker)
        assert persisted["in_position"] is False
        assert persisted["entry_price"] == 0.0

        # Intent must be terminal rejected, not open
        intents = env["bot_state"].list_all_intents()
        assert len(intents) == 1
        assert intents[0]["status"] == INTENT_REJECTED
        assert len(env["bot_state"].list_open_intents()) == 0

    def test_broker_422_unprocessable_leaves_engine_flat(self, temp_state_file, monkeypatch):
        monkeypatch.setenv("ENABLE_BEAR_MARKET_FILTER", "false")
        env = _setup_test_environment(temp_state_file)

        # Broker rejects order with 422 Unprocessable Entity
        err = _mock_api_error("order invalid or market closed", 422)
        env["broker"].place_market_order.side_effect = err

        now_et = datetime(2024, 1, 2, 9, 35, tzinfo=ET)
        main_module._check_signal_for_strategy(
            strat_cfg=env["strat_cfg"],
            now_et=now_et,
            morning_checks_done=set(),
            last_signal_check_map={},
            market_data=env["market_data"],
            signal_engine_map={env["strat_cfg"].ticker: env["engine"]},
            order_manager_map={env["strat_cfg"].ticker: env["order_mgr"]},
            alphasignal_client=None,
            broker=env["broker"],
            notifier=Mock(),
            global_risk=env["global_risk"],
            bot_state=env["bot_state"],
        )

        assert env["engine"]._in_position is False
        persisted = env["bot_state"].get_engine_state(env["strat_cfg"].ticker)
        assert persisted["in_position"] is False

    def test_execution_exception_leaves_engine_flat(self, temp_state_file, monkeypatch):
        """Unexpected exception during broker call or signal processing must

        not abandon engine in an in-position state.
        """
        monkeypatch.setenv("ENABLE_BEAR_MARKET_FILTER", "false")
        env = _setup_test_environment(temp_state_file)

        env["broker"].get_account.side_effect = RuntimeError("Fatal connection failure")

        now_et = datetime(2024, 1, 2, 9, 35, tzinfo=ET)
        main_module._check_signal_for_strategy(
            strat_cfg=env["strat_cfg"],
            now_et=now_et,
            morning_checks_done=set(),
            last_signal_check_map={},
            market_data=env["market_data"],
            signal_engine_map={env["strat_cfg"].ticker: env["engine"]},
            order_manager_map={env["strat_cfg"].ticker: env["order_mgr"]},
            alphasignal_client=None,
            broker=env["broker"],
            notifier=Mock(),
            global_risk=env["global_risk"],
            bot_state=env["bot_state"],
        )

        assert env["engine"]._in_position is False


# =============================================================================
# 3. Restarts Restore Engine from Actual Durable Ledger
# =============================================================================


class TestRestartsRestoreFromLedger:
    """Assert that on startup/restart, engine state is reconciled against the

    actual durable position ledger, discarding phantom states and rebuilding
    valid positions.
    """

    def test_restart_discards_phantom_in_position_state_when_ledger_empty(self, temp_state_file):
        """Phantom state in BotState (e.g. from previous crash or bug) must be

        sanitized to flat when the broker/ledger has no open position.
        """
        strat_cfg = _make_strategy_config("bollinger_rsi_combo", {}, ticker="AAPL")
        bot_state = BotState(state_file=temp_state_file)
        engine = SignalEngine(strat_cfg)

        # Simulate saved phantom position
        bot_state.save_engine_state(
            "AAPL",
            {
                "in_position": True,
                "entry_price": 175.0,
                "peak_price": 185.0,
                "peak_bar_ts": "2024-01-02",
                "vwap_position": 0,
            },
        )
        assert bot_state.get_open_positions() == {}

        # Run startup restoration
        main_module._restore_engine_states([strat_cfg], {"AAPL": engine}, bot_state)

        # Engine must be reset to FLAT
        assert engine._in_position is False
        assert engine._entry_price == 0.0
        assert engine._peak_price == 0.0

        # Durable state file must be updated to FLAT
        persisted = bot_state.get_engine_state("AAPL")
        assert persisted["in_position"] is False
        assert persisted["entry_price"] == 0.0
        assert persisted["peak_price"] == 0.0
        assert persisted["peak_bar_ts"] is None

    def test_restart_discards_phantom_vwap_long_state(self, temp_state_file):
        """vwap_position=1 implies a long that doesn't exist - must be sanitized,

        while short bookkeeping (-1) and cooldown counters are preserved.
        """
        strat_cfg = _make_strategy_config("vwap_reversion", {}, ticker="AAPL")
        bot_state = BotState(state_file=temp_state_file)
        engine = SignalEngine(strat_cfg)

        bot_state.save_engine_state(
            "AAPL",
            {
                "in_position": True,
                "entry_price": 150.0,
                "vwap_position": 1,
                "vwap_bars_since_signal": 4,
            },
        )

        main_module._restore_engine_states([strat_cfg], {"AAPL": engine}, bot_state)

        assert engine._in_position is False
        assert engine._vwap_position == 0
        assert engine._vwap_bars_since_signal == 4  # cooldown preserved
        persisted = bot_state.get_engine_state("AAPL")
        assert persisted["vwap_position"] == 0
        assert persisted["in_position"] is False

    def test_restart_rebuilds_engine_state_when_ledger_has_position_and_state_missing(
        self, temp_state_file
    ):
        """If ledger has a position but state file has no engine state (e.g.

        pre-persistence upgrade or fresh state file), rebuild in-position state.
        """
        strat_cfg = _make_strategy_config("bollinger_rsi_combo", {}, ticker="AAPL")
        bot_state = BotState(state_file=temp_state_file)
        engine = SignalEngine(strat_cfg)

        # Record position in ledger
        bot_state.record_position_open("AAPL", qty=15, entry_price=150.0)
        bot_state.set_position_high("AAPL", 168.0)

        # Engine state is None
        assert bot_state.get_engine_state("AAPL") is None

        main_module._restore_engine_states([strat_cfg], {"AAPL": engine}, bot_state)

        assert engine._in_position is True
        assert engine._entry_price == 150.0
        assert engine._peak_price == 168.0

        persisted = bot_state.get_engine_state("AAPL")
        assert persisted["in_position"] is True
        assert persisted["entry_price"] == 150.0
        assert persisted["peak_price"] == 168.0

    def test_restart_rebuilds_vwap_long_from_ledger(self, temp_state_file):
        """Ledger position rebuilds vwap_position = 1 for vwap_reversion strategy."""
        strat_cfg = _make_strategy_config("vwap_reversion", {}, ticker="AAPL")
        bot_state = BotState(state_file=temp_state_file)
        engine = SignalEngine(strat_cfg)

        bot_state.record_position_open("AAPL", qty=10, entry_price=100.0)
        main_module._restore_engine_states([strat_cfg], {"AAPL": engine}, bot_state)

        assert engine._in_position is True
        assert engine._vwap_position == 1

    def test_restart_restores_consistent_in_position_state(self, temp_state_file):
        """When ledger and saved state both reflect an open position, state is restored."""
        strat_cfg = _make_strategy_config("greenblatt_weekly", {}, ticker="AAPL")
        bot_state = BotState(state_file=temp_state_file)
        engine = SignalEngine(strat_cfg)

        bot_state.record_position_open("AAPL", qty=20, entry_price=140.0)
        bot_state.set_position_high("AAPL", 160.0)
        saved = {
            "in_position": True,
            "entry_price": 140.0,
            "peak_price": 160.0,
            "peak_bar_ts": "2024-01-05",
        }
        bot_state.save_engine_state("AAPL", saved)

        main_module._restore_engine_states([strat_cfg], {"AAPL": engine}, bot_state)

        assert engine._in_position is True
        assert engine._entry_price == 140.0
        assert engine._peak_price == 160.0
        assert engine._peak_bar_ts == "2024-01-05"


# =============================================================================
# 4. Continuous Synchronization & In-Flight Protection
# =============================================================================


class TestContinuousSyncAndInFlightProtection:
    """Assert that _sync_engines_with_ledger protects in-flight working orders

    and ensures ledger wins upon cancellation, fill, or exit.
    """

    def test_sync_engines_with_ledger_protects_in_flight_order(self, temp_state_file):
        """An engine that emitted BUY with a working order in flight must NOT

        be reset to flat while the order is submitting/submitted.
        """
        strat_cfg = _make_strategy_config("bollinger_rsi_combo", {}, ticker="AAPL")
        bot_state = BotState(state_file=temp_state_file)
        engine = SignalEngine(strat_cfg)

        # Engine enters position on BUY signal
        engine._in_position = True
        engine._entry_price = 150.0

        # Durable intent created (in-flight at broker)
        intent = bot_state.create_submission_intent(
            ticker="AAPL",
            side="BUY",
            qty=10,
            order_type="market",
            strategy_name=strat_cfg.strategy.name,
            decision_price=150.0,
        )
        bot_state.update_submission_intent(intent["intent_id"], status=INTENT_SUBMITTED)

        # Ledger has no position yet (fill has not arrived)
        assert bot_state.get_open_positions() == {}

        # Run continuous sync
        main_module._sync_engines_with_ledger({"AAPL": engine}, bot_state)

        # In-flight protection: engine remains in-position
        assert engine._in_position is True
        assert engine._entry_price == 150.0

    def test_sync_engines_with_ledger_flattens_when_in_flight_order_cancelled(
        self, temp_state_file
    ):
        """When an in-flight order is cancelled without filling, sync must

        reset the engine to flat.
        """
        strat_cfg = _make_strategy_config("bollinger_rsi_combo", {}, ticker="AAPL")
        bot_state = BotState(state_file=temp_state_file)
        engine = SignalEngine(strat_cfg)

        engine._in_position = True
        engine._entry_price = 150.0

        intent = bot_state.create_submission_intent(
            ticker="AAPL",
            side="BUY",
            qty=10,
            order_type="market",
            strategy_name=strat_cfg.strategy.name,
            decision_price=150.0,
        )
        # Order cancelled at broker
        bot_state.update_submission_intent(intent["intent_id"], status=INTENT_CANCELLED)

        main_module._sync_engines_with_ledger({"AAPL": engine}, bot_state)

        # Order cancelled and no ledger position -> engine reset to flat!
        assert engine._in_position is False
        assert engine._entry_price == 0.0

    def test_sync_engines_with_ledger_confirms_long_when_order_filled(self, temp_state_file):
        """When an in-flight order fills and books to ledger, sync confirms long."""
        strat_cfg = _make_strategy_config("bollinger_rsi_combo", {}, ticker="AAPL")
        bot_state = BotState(state_file=temp_state_file)
        engine = SignalEngine(strat_cfg)

        engine._in_position = True
        engine._entry_price = 150.0

        intent = bot_state.create_submission_intent(
            ticker="AAPL",
            side="BUY",
            qty=10,
            order_type="market",
            strategy_name=strat_cfg.strategy.name,
            decision_price=150.0,
        )
        # Broker fill confirmed
        bot_state.update_submission_intent(intent["intent_id"], status=INTENT_FILLED)
        bot_state.record_position_open("AAPL", qty=10, entry_price=151.25)
        bot_state.set_position_high("AAPL", 152.0)

        main_module._sync_engines_with_ledger({"AAPL": engine}, bot_state)

        assert engine._in_position is True
        assert engine._entry_price == 151.25
        assert engine._peak_price == 152.0

    def test_exit_sell_reconciles_back_to_long_if_sell_order_fails(self, temp_state_file):
        """If strategy emits SELL (flipping _in_position to False), but the SELL order

        is rejected/fails, the ledger position remains open -> sync restores _in_position = True.
        """
        strat_cfg = _make_strategy_config("bollinger_rsi_combo", {}, ticker="AAPL")
        bot_state = BotState(state_file=temp_state_file)
        engine = SignalEngine(strat_cfg)

        # Ledger holds the position
        bot_state.record_position_open("AAPL", qty=10, entry_price=145.0)
        bot_state.set_position_high("AAPL", 155.0)

        # Strategy emitted SELL -> engine thought it exited
        engine._in_position = False
        engine._entry_price = 0.0

        # Sync with ledger -> ledger holds position, so ledger wins!
        main_module._sync_engines_with_ledger({"AAPL": engine}, bot_state)

        assert engine._in_position is True
        assert engine._entry_price == 145.0
        assert engine._peak_price == 155.0

    def test_risk_exit_close_flattens_engine_state_and_persists(self, temp_state_file):
        """When a position is closed by SL/TP/trailing stop in _run_exit_checks,
        the engine must be restored to flat and persisted to state.
        """
        strat_cfg = _make_strategy_config("bollinger_rsi_combo", {}, ticker="AAPL")
        bot_state = BotState(state_file=temp_state_file)
        engine = SignalEngine(strat_cfg)
        engine._in_position = True
        engine._entry_price = 150.0
        bot_state.record_position_open("AAPL", qty=10, entry_price=150.0)
        bot_state.set_position_high("AAPL", 155.0)

        pos = Position(
            symbol="AAPL",
            qty=10.0,
            side="long",
            market_value=1400.0,
            avg_entry_price=150.0,
            unrealized_pl=-100.0,
            unrealized_plpc=-0.067,
            current_price=140.0,
        )
        broker = Mock()
        broker.get_all_positions.return_value = [pos]

        market_data = Mock()
        market_data.get_current_price.return_value = 140.0

        order_manager = Mock()
        order_manager.check_exits.return_value = [
            {"ticker": "AAPL", "reason": "Stop loss hit (down 6.7%)", "current_price": 140.0}
        ]
        order_manager.close_position.return_value = {"status": "success", "order_id": "o_close_1"}
        order_manager.config.risk.commission_per_trade = 0.0

        app_config = Mock(dry_run=False)

        main_module._run_exit_checks(
            broker=broker,
            market_data=market_data,
            order_manager_map={"AAPL": order_manager},
            bot_state=bot_state,
            app_config=app_config,
            notifier=Mock(),
            global_risk=GlobalRiskManager(),
            signal_engine_map={"AAPL": engine},
        )

        assert engine._in_position is False
        assert engine._entry_price == 0.0
        persisted = bot_state.get_engine_state("AAPL")
        assert persisted["in_position"] is False


class TestMultipleStrategiesLedgerSync:
    """Assert that all stateful strategies (trend_adaptive_rsi, greenblatt_weekly,
    vwap_reversion) properly synchronize with the ledger on blocks/rejections.
    """

    def test_trend_adaptive_rsi_blocked_entry_leaves_flat(self, temp_state_file, monkeypatch):
        monkeypatch.setenv("ENABLE_BEAR_MARKET_FILTER", "false")
        strat_cfg = _make_strategy_config("trend_adaptive_rsi", {
            "rsi_period": 14,
            "trend_sma": 50,
            "trend_lookback": 5,
        }, ticker="MSFT")
        bot_state = BotState(state_file=temp_state_file)
        engine = SignalEngine(strat_cfg)

        # Force a BUY condition by setting _in_position = True as if generate_signal ran
        engine._in_position = True
        engine._entry_price = 250.0

        # Sync with ledger (empty)
        main_module._sync_engines_with_ledger({"MSFT": engine}, bot_state)

        assert engine._in_position is False
        assert engine._entry_price == 0.0

    def test_greenblatt_weekly_broker_rejection_leaves_flat(self, temp_state_file):
        strat_cfg = _make_strategy_config("greenblatt_weekly", {
            "fast_sma": 10,
            "slow_sma": 50,
            "rsi_period": 14,
        }, ticker="GOOG")
        bot_state = BotState(state_file=temp_state_file)
        engine = SignalEngine(strat_cfg)

        # Signal engine emitted BUY
        engine._in_position = True
        engine._entry_price = 120.0
        engine._peak_price = 120.0
        engine._peak_bar_ts = "2024-01-05"

        # Broker rejected order -> intent marked INTENT_REJECTED
        intent = bot_state.create_submission_intent(
            ticker="GOOG",
            side="BUY",
            qty=10,
            order_type="market",
            strategy_name=strat_cfg.strategy.name,
            decision_price=120.0,
        )
        bot_state.update_submission_intent(intent["intent_id"], status=INTENT_REJECTED)

        main_module._sync_engines_with_ledger({"GOOG": engine}, bot_state)

        assert engine._in_position is False
        assert engine._entry_price == 0.0
        assert engine._peak_price == 0.0
        assert engine._peak_bar_ts is None

    def test_vwap_reversion_blocked_entry_leaves_flat(self, temp_state_file):
        strat_cfg = _make_strategy_config("vwap_reversion", {
            "deviation_threshold": 2.0,
            "rsi_period": 14,
            "cooldown_days": 3,
        }, ticker="NVDA")
        bot_state = BotState(state_file=temp_state_file)
        engine = SignalEngine(strat_cfg)

        # Signal engine emitted BUY
        engine._in_position = True
        engine._vwap_position = 1

        main_module._sync_engines_with_ledger({"NVDA": engine}, bot_state)

        assert engine._in_position is False
        assert engine._vwap_position == 0
