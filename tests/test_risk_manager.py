"""
Test Risk Manager (B5)

Tests for the RiskManager and GlobalRiskManager classes.
"""

import os
import pytest
from datetime import datetime, date

from alphalive.execution.risk_manager import RiskManager, GlobalRiskManager
from alphalive.strategy_schema import Risk, Execution


@pytest.fixture
def sample_risk_config():
    """Sample risk configuration."""
    return Risk(
        stop_loss_pct=2.0,
        take_profit_pct=5.0,
        max_position_size_pct=10.0,
        max_daily_loss_pct=3.0,
        max_open_positions=5,
        portfolio_max_positions=10,
        trailing_stop_enabled=False
    )


@pytest.fixture
def sample_execution_config():
    """Sample execution configuration."""
    return Execution(
        order_type="market",
        cooldown_bars=1
    )


@pytest.fixture
def risk_manager(sample_risk_config, sample_execution_config):
    """Create a RiskManager instance."""
    return RiskManager(
        risk_config=sample_risk_config,
        execution_config=sample_execution_config,
        strategy_name="test_strategy"
    )


def test_risk_manager_initialization(risk_manager, sample_risk_config):
    """Test RiskManager initialization."""
    assert risk_manager.strategy_name == "test_strategy"
    assert risk_manager.risk_config == sample_risk_config
    assert risk_manager.daily_pnl == 0.0
    assert risk_manager.consecutive_losses == 0
    assert risk_manager.max_consecutive_losses == 3
    assert risk_manager.trading_paused_by_circuit_breaker is False


def test_calculate_position_size(risk_manager):
    """Test position size calculation."""
    # max_position_size_pct = 10%, equity = 100000
    # Max position value = 10000
    # At $150/share, can buy 66 shares (floor of 10000/150)
    shares = risk_manager.calculate_position_size(
        ticker="AAPL",
        signal="BUY",
        current_price=150.0,
        account_equity=100000.0
    )

    assert shares == 66

    # Test with different price
    shares = risk_manager.calculate_position_size(
        ticker="AAPL",
        signal="BUY",
        current_price=200.0,
        account_equity=100000.0
    )

    assert shares == 50  # floor(10000/200)


def test_calculate_position_size_invalid_inputs(risk_manager):
    """Test position size calculation with invalid inputs."""
    # Invalid price
    shares = risk_manager.calculate_position_size(
        ticker="AAPL",
        signal="BUY",
        current_price=0.0,
        account_equity=100000.0
    )
    assert shares == 0

    # Invalid equity
    shares = risk_manager.calculate_position_size(
        ticker="AAPL",
        signal="BUY",
        current_price=150.0,
        account_equity=0.0
    )
    assert shares == 0


def test_check_stop_loss_long(risk_manager):
    """Test stop loss check for long positions."""
    # stop_loss_pct = 2.0%
    # Entry: $100, Stop: $98

    # Should NOT trigger at $98.01
    assert risk_manager.check_stop_loss(100.0, 98.01, "long") is False

    # Should trigger at $98.00
    assert risk_manager.check_stop_loss(100.0, 98.0, "long") is True

    # Should trigger at $97.00
    assert risk_manager.check_stop_loss(100.0, 97.0, "long") is True


def test_check_stop_loss_short(risk_manager):
    """Test stop loss check for short positions."""
    # stop_loss_pct = 2.0%
    # Entry: $100, Stop: $102

    # Should NOT trigger at $101.99
    assert risk_manager.check_stop_loss(100.0, 101.99, "short") is False

    # Should trigger at $102.00
    assert risk_manager.check_stop_loss(100.0, 102.0, "short") is True

    # Should trigger at $103.00
    assert risk_manager.check_stop_loss(100.0, 103.0, "short") is True


def test_check_take_profit_long(risk_manager):
    """Test take profit check for long positions."""
    # take_profit_pct = 5.0%
    # Entry: $100, Target: $105

    # Should NOT trigger at $104.99
    assert risk_manager.check_take_profit(100.0, 104.99, "long") is False

    # Should trigger at $105.00
    assert risk_manager.check_take_profit(100.0, 105.0, "long") is True

    # Should trigger at $106.00
    assert risk_manager.check_take_profit(100.0, 106.0, "long") is True


def test_check_take_profit_short(risk_manager):
    """Test take profit check for short positions."""
    # take_profit_pct = 5.0%
    # Entry: $100, Target: $95

    # Should NOT trigger at $95.01
    assert risk_manager.check_take_profit(100.0, 95.01, "short") is False

    # Should trigger at $95.00
    assert risk_manager.check_take_profit(100.0, 95.0, "short") is True

    # Should trigger at $94.00
    assert risk_manager.check_take_profit(100.0, 94.0, "short") is True


def test_check_trailing_stop_disabled(risk_manager):
    """Test trailing stop when disabled."""
    # trailing_stop_enabled = False by default
    assert risk_manager.check_trailing_stop(100.0, 110.0, 108.0, "long") is False


def test_check_trailing_stop_long(sample_risk_config, sample_execution_config):
    """Test trailing stop for long positions."""
    # Enable trailing stop
    sample_risk_config.trailing_stop_enabled = True
    sample_risk_config.trailing_stop_pct = 3.0  # 3% trailing stop

    rm = RiskManager(sample_risk_config, sample_execution_config, "test")

    # Entry: $100, High: $110, Trailing stop: $106.70 (110 * 0.97)
    # Should NOT trigger at $106.71
    assert rm.check_trailing_stop(100.0, 110.0, 106.71, "long") is False

    # Should trigger at $106.70
    assert rm.check_trailing_stop(100.0, 110.0, 106.70, "long") is True


def test_check_daily_loss_limit(risk_manager):
    """Test daily loss limit check."""
    # max_daily_loss_pct = 3.0%
    # Equity = $100000, Max loss = $3000

    # No loss yet
    assert risk_manager.check_daily_loss_limit(100000.0) is False

    # Small loss
    risk_manager.daily_pnl = -1000.0
    assert risk_manager.check_daily_loss_limit(100000.0) is False

    # At limit
    risk_manager.daily_pnl = -3000.0
    assert risk_manager.check_daily_loss_limit(100000.0) is True

    # Beyond limit
    risk_manager.daily_pnl = -4000.0
    assert risk_manager.check_daily_loss_limit(100000.0) is True


def test_check_max_positions(risk_manager):
    """Test max positions check."""
    # max_open_positions = 5

    assert risk_manager.check_max_positions(0) is True
    assert risk_manager.check_max_positions(4) is True
    assert risk_manager.check_max_positions(5) is False  # At limit
    assert risk_manager.check_max_positions(6) is False  # Beyond limit


def test_check_cooldown(risk_manager):
    """Test cooldown period check."""
    # cooldown_bars = 1

    # Never traded AAPL before
    assert risk_manager.check_cooldown("AAPL", current_bar=100) is True

    # Record trade at bar 100
    risk_manager.last_trade_bar["AAPL"] = 100

    # Same bar - should fail
    assert risk_manager.check_cooldown("AAPL", current_bar=100) is False

    # Next bar - should pass (1 bar elapsed)
    assert risk_manager.check_cooldown("AAPL", current_bar=101) is True

    # Future bar - should pass
    assert risk_manager.check_cooldown("AAPL", current_bar=105) is True


def test_can_trade_all_checks_pass(risk_manager):
    """Test can_trade when all checks pass."""
    can_trade, reason = risk_manager.can_trade(
        ticker="AAPL",
        signal="BUY",
        account_equity=100000.0,
        current_positions_count=2,
        total_portfolio_positions=5,
        current_bar=100
    )

    assert can_trade is True
    assert reason == "OK"


def test_can_trade_kill_switch(risk_manager, monkeypatch):
    """Test can_trade with TRADING_PAUSED env var."""
    # Set kill switch
    monkeypatch.setenv("TRADING_PAUSED", "true")

    can_trade, reason = risk_manager.can_trade(
        ticker="AAPL",
        signal="BUY",
        account_equity=100000.0,
        current_positions_count=2,
        total_portfolio_positions=5
    )

    assert can_trade is False
    assert "kill switch" in reason.lower()


def test_can_trade_daily_loss_limit(risk_manager):
    """Test can_trade with daily loss limit hit."""
    # Set daily P&L to -$3000 (3% of $100k)
    risk_manager.daily_pnl = -3000.0

    can_trade, reason = risk_manager.can_trade(
        ticker="AAPL",
        signal="BUY",
        account_equity=100000.0,
        current_positions_count=2,
        total_portfolio_positions=5
    )

    assert can_trade is False
    assert "daily loss limit" in reason.lower()


def test_can_trade_circuit_breaker(risk_manager):
    """Test can_trade with circuit breaker triggered."""
    risk_manager.trading_paused_by_circuit_breaker = True
    risk_manager.consecutive_losses = 3

    can_trade, reason = risk_manager.can_trade(
        ticker="AAPL",
        signal="BUY",
        account_equity=100000.0,
        current_positions_count=2,
        total_portfolio_positions=5
    )

    assert can_trade is False
    assert "circuit breaker" in reason.lower()


def test_can_trade_max_positions_strategy(risk_manager):
    """Test can_trade with max positions for strategy."""
    can_trade, reason = risk_manager.can_trade(
        ticker="AAPL",
        signal="BUY",
        account_equity=100000.0,
        current_positions_count=5,  # At limit
        total_portfolio_positions=5
    )

    assert can_trade is False
    assert "max positions reached for strategy" in reason.lower()


def test_can_trade_portfolio_max_positions(risk_manager):
    """Test can_trade with portfolio max positions."""
    can_trade, reason = risk_manager.can_trade(
        ticker="AAPL",
        signal="BUY",
        account_equity=100000.0,
        current_positions_count=2,
        total_portfolio_positions=10  # At portfolio limit
    )

    assert can_trade is False
    assert "portfolio max positions" in reason.lower()


def test_record_trade_win(risk_manager):
    """Test recording a winning trade."""
    # Record a loss first
    risk_manager.consecutive_losses = 2

    # Record a win
    risk_manager.record_trade("AAPL", pnl=150.0, current_bar=100)

    assert risk_manager.daily_pnl == 150.0
    assert len(risk_manager.daily_trades) == 1
    assert risk_manager.consecutive_losses == 0  # Reset on win
    assert risk_manager.trading_paused_by_circuit_breaker is False


def test_record_trade_loss(risk_manager):
    """Test recording a losing trade."""
    risk_manager.record_trade("AAPL", pnl=-100.0, current_bar=100)

    assert risk_manager.daily_pnl == -100.0
    assert len(risk_manager.daily_trades) == 1
    assert risk_manager.consecutive_losses == 1
    assert risk_manager.trading_paused_by_circuit_breaker is False


def test_record_trade_circuit_breaker_triggered(risk_manager):
    """Test circuit breaker triggering after 3 consecutive losses."""
    # Record 2 losses
    risk_manager.record_trade("AAPL", pnl=-100.0, current_bar=100)
    risk_manager.record_trade("AAPL", pnl=-50.0, current_bar=101)

    assert risk_manager.consecutive_losses == 2
    assert risk_manager.trading_paused_by_circuit_breaker is False

    # 3rd loss triggers circuit breaker
    risk_manager.record_trade("AAPL", pnl=-75.0, current_bar=102)

    assert risk_manager.consecutive_losses == 3
    assert risk_manager.trading_paused_by_circuit_breaker is True
    assert risk_manager.daily_pnl == -225.0


def test_reset_daily(risk_manager):
    """Test daily reset."""
    # Set some state
    risk_manager.daily_pnl = -500.0
    risk_manager.daily_trades = [{"ticker": "AAPL", "pnl": -500.0}]
    risk_manager.consecutive_losses = 2
    risk_manager.trading_paused_by_circuit_breaker = True
    risk_manager.last_reset_date = None

    # Reset
    risk_manager.reset_daily()

    assert risk_manager.daily_pnl == 0.0
    assert risk_manager.daily_trades == []
    assert risk_manager.consecutive_losses == 0
    assert risk_manager.trading_paused_by_circuit_breaker is False
    assert risk_manager.last_reset_date == date.today()


def test_global_risk_manager_initialization():
    """Test GlobalRiskManager initialization."""
    grm = GlobalRiskManager()

    assert grm.global_daily_stats["date"] == date.today()
    assert grm.global_daily_stats["total_pnl"] == 0.0
    assert grm.global_daily_stats["strategies_halted"] is False


def test_global_risk_manager_register_strategy(risk_manager):
    """Test registering strategies with GlobalRiskManager."""
    grm = GlobalRiskManager()

    grm.register_strategy("strategy_1", risk_manager)

    assert "strategy_1" in grm.strategy_managers
    assert grm.strategy_managers["strategy_1"] == risk_manager


def test_global_check_daily_loss_no_loss():
    """Test global daily loss check with no losses."""
    grm = GlobalRiskManager()

    # Create and register strategies
    rm1 = RiskManager(
        Risk(stop_loss_pct=2.0, take_profit_pct=5.0, max_position_size_pct=10.0,
             max_daily_loss_pct=3.0, max_open_positions=5, portfolio_max_positions=10),
        Execution(order_type="market", cooldown_bars=1),
        "strategy_1"
    )
    rm2 = RiskManager(
        Risk(stop_loss_pct=2.0, take_profit_pct=5.0, max_position_size_pct=10.0,
             max_daily_loss_pct=3.0, max_open_positions=5, portfolio_max_positions=10),
        Execution(order_type="market", cooldown_bars=1),
        "strategy_2"
    )

    grm.register_strategy("strategy_1", rm1)
    grm.register_strategy("strategy_2", rm2)

    # Check (should pass with no losses)
    can_trade, reason = grm.check_global_daily_loss(100000.0, max_daily_loss_pct=5.0)

    assert can_trade is True
    assert reason == "OK"


def test_global_check_daily_loss_limit_exceeded():
    """Test global daily loss check with limit exceeded."""
    grm = GlobalRiskManager()

    # Create and register strategies
    rm1 = RiskManager(
        Risk(stop_loss_pct=2.0, take_profit_pct=5.0, max_position_size_pct=10.0,
             max_daily_loss_pct=3.0, max_open_positions=5, portfolio_max_positions=10),
        Execution(order_type="market", cooldown_bars=1),
        "strategy_1"
    )
    rm2 = RiskManager(
        Risk(stop_loss_pct=2.0, take_profit_pct=5.0, max_position_size_pct=10.0,
             max_daily_loss_pct=3.0, max_open_positions=5, portfolio_max_positions=10),
        Execution(order_type="market", cooldown_bars=1),
        "strategy_2"
    )

    # Set losses
    rm1.daily_pnl = -3000.0  # -3%
    rm2.daily_pnl = -2500.0  # -2.5%
    # Total: -$5500 = -5.5% of $100k

    grm.register_strategy("strategy_1", rm1)
    grm.register_strategy("strategy_2", rm2)

    # Initialize start equity
    grm.global_daily_stats["start_equity"] = 100000.0

    # Check (should fail with 5% limit)
    can_trade, reason = grm.check_global_daily_loss(100000.0, max_daily_loss_pct=5.0)

    assert can_trade is False
    assert "GLOBAL daily loss limit exceeded" in reason
    assert grm.global_daily_stats["strategies_halted"] is True


def test_global_risk_manager_is_trading_halted():
    """Test is_trading_halted method."""
    grm = GlobalRiskManager()

    assert grm.is_trading_halted() is False

    grm.global_daily_stats["strategies_halted"] = True

    assert grm.is_trading_halted() is True
