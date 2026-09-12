"""
Tests for multi-strategy Telegram commands (2026-09-11 pass 2, objective 4):
/status, /config, /performance, /close_all no longer silently default to the
first configured strategy when more than one is registered.

No real Telegram messages or broker orders - broker and notifier are Mocks,
OrderManager/RiskManager are Mocks configured per test.
"""

from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from alphalive.broker.base_broker import Account, Order, Position
from alphalive.execution.risk_manager import GlobalRiskManager
from alphalive.notifications.telegram_commands import TelegramCommandListener

ET = ZoneInfo("America/New_York")


def _account():
    return Account(
        equity=100000.0,
        cash=50000.0,
        buying_power=200000.0,
        portfolio_value=100000.0,
        long_market_value=50000.0,
        short_market_value=0.0,
        daytrade_count=0,
        pattern_day_trader=False,
        account_status="ACTIVE",
    )


def _position(symbol, qty=10.0):
    return Position(
        symbol=symbol,
        qty=qty,
        side="long",
        avg_entry_price=100.0,
        current_price=101.0,
        unrealized_pl=10.0,
        unrealized_plpc=1.0,
        market_value=1010.0,
    )


def _config(ticker, strategy_name="ma_crossover"):
    cfg = Mock()
    cfg.strategy.name = strategy_name
    cfg.ticker = ticker
    cfg.timeframe = "1Day"
    cfg.risk.stop_loss_pct = 2.0
    cfg.risk.take_profit_pct = 5.0
    cfg.risk.max_position_size_pct = 10.0
    cfg.risk.max_daily_loss_pct = 3.0
    cfg.risk.max_open_positions = 5
    cfg.risk.trailing_stop_enabled = False
    cfg.risk.trailing_stop_pct = 3.0
    cfg.execution.order_type = "market"
    cfg.execution.limit_offset_pct = 0.1
    cfg.execution.cooldown_bars = 1
    return cfg


def _order_manager(ticker, daily_pnl=0.0):
    om = Mock()
    om.order_history = []
    om.close_position = Mock(
        return_value={"status": "success", "order_id": f"o_{ticker}"}
    )
    return om


def _risk_manager(daily_pnl=0.0):
    rm = Mock()
    rm.daily_pnl = daily_pnl
    rm.daily_trades = []
    rm.consecutive_losses = 0
    rm.trading_paused_manual = False
    rm.trading_paused_by_circuit_breaker = False
    rm.degraded_mode = False
    return rm


@pytest.fixture
def zero_strategy_setup():
    """No configured strategies at all - edge case."""
    broker = Mock()
    broker.paper = True
    broker.get_account = Mock(return_value=_account())
    broker.get_all_positions = Mock(return_value=[])
    notifier = Mock()
    listener = TelegramCommandListener(
        bot_token="t",
        chat_id="1",
        order_manager=Mock(),
        risk_manager=_risk_manager(),
        broker=broker,
        notifier=notifier,
        config=_config("NONE"),
        global_risk=GlobalRiskManager(),
        order_manager_map={},
        strategy_configs={},
    )
    return listener, broker, notifier


@pytest.fixture
def multi_strategy_setup():
    tickers = ["AAPL", "MSFT", "SPY"]
    order_manager_map = {t: _order_manager(t) for t in tickers}
    strategy_configs = {t: _config(t) for t in tickers}
    global_risk = GlobalRiskManager()
    risk_manager_map = {
        t: _risk_manager(daily_pnl=float(i * 10)) for i, t in enumerate(tickers)
    }
    for t, rm in risk_manager_map.items():
        global_risk.register_strategy(t, rm)

    broker = Mock()
    broker.paper = True
    broker.get_account = Mock(return_value=_account())
    broker.get_all_positions = Mock(return_value=[_position(t) for t in tickers])
    notifier = Mock()

    listener = TelegramCommandListener(
        bot_token="t",
        chat_id="1",
        order_manager=order_manager_map["AAPL"],
        risk_manager=risk_manager_map["AAPL"],
        broker=broker,
        notifier=notifier,
        config=strategy_configs["AAPL"],
        global_risk=global_risk,
        order_manager_map=order_manager_map,
        strategy_configs=strategy_configs,
    )
    return listener, broker, notifier, order_manager_map, risk_manager_map, global_risk


def _last_message(notifier):
    return notifier.send_message.call_args[0][0]


# ---------------------------------------------------------------------------
# Zero, one, multiple configured strategies
# ---------------------------------------------------------------------------


def test_zero_strategies_status_does_not_crash(zero_strategy_setup):
    listener, broker, notifier = zero_strategy_setup
    listener._handle_command("/status")
    assert notifier.send_message.called
    # Falls back to the single passed-in config/risk_manager since
    # strategy_configs/order_manager_map/risk_manager_map default to a
    # single-entry dict when empty maps are explicitly passed as {}.


def test_zero_strategies_close_all_no_positions(zero_strategy_setup):
    listener, broker, notifier = zero_strategy_setup
    broker.get_all_positions.return_value = []
    listener._handle_command("/close_all")
    assert "No open positions" in _last_message(notifier)


def test_multi_strategy_status_shows_all_strategies(multi_strategy_setup):
    listener, broker, notifier, _, _, _ = multi_strategy_setup
    listener._handle_command("/status")
    message = _last_message(notifier)
    assert "AAPL" in message
    assert "MSFT" in message
    assert "SPY" in message
    assert "Strategies (3)" in message


def test_multi_strategy_status_aggregates_pnl(multi_strategy_setup):
    listener, broker, notifier, _, risk_manager_map, _ = multi_strategy_setup
    listener._handle_command("/status")
    message = _last_message(notifier)
    total = sum(rm.daily_pnl for rm in risk_manager_map.values())
    assert f"{total:.2f}" in message
    assert "Total Daily P&L" in message


# ---------------------------------------------------------------------------
# /close_all across strategies
# ---------------------------------------------------------------------------


def test_close_all_routes_each_position_to_its_own_order_manager(multi_strategy_setup):
    listener, broker, notifier, order_manager_map, _, _ = multi_strategy_setup
    listener._handle_command("/close_all")
    listener._handle_command("/confirm_close")

    for ticker, om in order_manager_map.items():
        om.close_position.assert_called_once_with(
            ticker, reason="Manual close via Telegram /close_all"
        )
    message = _last_message(notifier)
    assert "Positions Closed" in message
    assert "AAPL" in message and "MSFT" in message and "SPY" in message


def test_close_all_one_strategy_fails_reports_incomplete(multi_strategy_setup):
    listener, broker, notifier, order_manager_map, _, _ = multi_strategy_setup
    order_manager_map["MSFT"].close_position.return_value = {
        "status": "error",
        "reason": "broker rejected",
    }
    listener._handle_command("/close_all")
    listener._handle_command("/confirm_close")

    message = _last_message(notifier)
    assert "Incomplete" in message
    assert "❌ MSFT" in message
    assert "✅ AAPL" in message


def test_close_all_one_strategy_uncertain_reports_uncertain_not_closed(
    multi_strategy_setup,
):
    listener, broker, notifier, order_manager_map, _, _ = multi_strategy_setup
    order_manager_map["SPY"].close_position.return_value = {
        "status": "blocked",
        "reason": "close intent is uncertain - quarantined",
    }
    listener._handle_command("/close_all")
    listener._handle_command("/confirm_close")

    message = _last_message(notifier)
    assert "Incomplete" in message
    assert "❓ SPY" in message
    assert "Uncertain" in message
    # Must never appear as closed.
    assert "✅ SPY" not in message


def test_repeated_close_all_is_idempotent(multi_strategy_setup):
    """A second /close_all + /confirm_close cycle after a successful first
    one must not error, even though OrderManager.close_position is a Mock
    here (real idempotency is tested at the OrderManager layer in
    test_order_intent_lifecycle.py) - this proves the Telegram layer
    re-issues the same flow safely and reports based on whatever the
    OrderManager returns (e.g. "already closed")."""
    listener, broker, notifier, order_manager_map, _, _ = multi_strategy_setup
    listener._handle_command("/close_all")
    listener._handle_command("/confirm_close")
    first_calls = {
        t: om.close_position.call_count for t, om in order_manager_map.items()
    }

    # Second round - OrderManager now reports "already closed" (as the
    # real idempotent close_position would after a successful first close).
    for om in order_manager_map.values():
        om.close_position.return_value = {
            "status": "success",
            "order_id": None,
            "reason": "already closed",
        }
    listener._handle_command("/close_all")
    listener._handle_command("/confirm_close")

    for t, om in order_manager_map.items():
        assert om.close_position.call_count == first_calls[t] + 1
    message = _last_message(notifier)
    assert "Positions Closed" in message


# ---------------------------------------------------------------------------
# Targeted and untargeted /config, /performance
# ---------------------------------------------------------------------------


def test_multi_strategy_config_untargeted_returns_per_strategy_summary(
    multi_strategy_setup,
):
    listener, broker, notifier, _, _, _ = multi_strategy_setup
    listener._handle_command("/config")
    message = _last_message(notifier)
    assert "Strategy Configurations (3)" in message
    assert "AAPL" in message and "MSFT" in message and "SPY" in message


def test_multi_strategy_config_targeted_returns_single_detail(multi_strategy_setup):
    listener, broker, notifier, _, _, _ = multi_strategy_setup
    listener._handle_command("/config AAPL")
    message = _last_message(notifier)
    assert "Strategy Configuration" in message
    assert "AAPL" in message
    assert "Stop Loss" in message
    # Should not be the compact multi-summary form.
    assert "Strategy Configurations" not in message


def test_multi_strategy_config_unknown_ticker(multi_strategy_setup):
    listener, broker, notifier, _, _, _ = multi_strategy_setup
    listener._handle_command("/config TSLA")
    message = _last_message(notifier)
    assert "Unknown strategy ticker" in message


def test_multi_strategy_performance_untargeted_returns_per_strategy_summary(
    multi_strategy_setup,
):
    listener, broker, notifier, _, risk_manager_map, _ = multi_strategy_setup
    for rm in risk_manager_map.values():
        rm.daily_trades = [{"ticker": "X", "pnl": 5.0}]
    listener._handle_command("/performance")
    message = _last_message(notifier)
    assert "Performance (3 strategies)" in message
    assert "AAPL" in message and "MSFT" in message and "SPY" in message


def test_multi_strategy_performance_targeted(multi_strategy_setup):
    listener, broker, notifier, _, risk_manager_map, _ = multi_strategy_setup
    risk_manager_map["MSFT"].daily_trades = [
        {"ticker": "MSFT", "pnl": 20.0},
        {"ticker": "MSFT", "pnl": -5.0},
    ]
    listener._handle_command("/performance MSFT")
    message = _last_message(notifier)
    assert "Performance (MSFT)" in message
    assert "2" in message  # total trades


def test_single_strategy_config_stays_untargeted(mock_components_single):
    """Backward compat: exactly one configured strategy behaves exactly
    like before - /config with no args shows it directly."""
    listener, notifier = mock_components_single
    listener._handle_command("/config")
    message = _last_message(notifier)
    assert "Strategy Configuration" in message
    assert "Strategy Configurations" not in message


@pytest.fixture
def mock_components_single():
    broker = Mock()
    broker.paper = True
    broker.get_account = Mock(return_value=_account())
    broker.get_all_positions = Mock(return_value=[])
    notifier = Mock()
    listener = TelegramCommandListener(
        bot_token="t",
        chat_id="1",
        order_manager=_order_manager("AAPL"),
        risk_manager=_risk_manager(),
        broker=broker,
        notifier=notifier,
        config=_config("AAPL"),
    )
    return listener, notifier


# ---------------------------------------------------------------------------
# Command response accuracy: never silently defaults to first strategy
# ---------------------------------------------------------------------------


def test_status_never_silently_shows_only_first_strategy(multi_strategy_setup):
    listener, broker, notifier, _, _, _ = multi_strategy_setup
    listener._handle_command("/status")
    message = _last_message(notifier)
    # All three must be mentioned, not just AAPL (the "first" one this
    # listener was also constructed with legacy single params for).
    for ticker in ("AAPL", "MSFT", "SPY"):
        assert ticker in message
