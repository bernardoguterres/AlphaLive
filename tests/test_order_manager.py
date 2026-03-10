"""
Test Order Manager (B6)

Tests for OrderManager class with retry logic, duplicate prevention,
slippage checks, and exit condition checking.
"""

import pytest
from unittest.mock import Mock, MagicMock
from datetime import datetime
from zoneinfo import ZoneInfo

from alphalive.execution.order_manager import OrderManager
from alphalive.execution.risk_manager import RiskManager
from alphalive.strategy_schema import StrategySchema, Risk, Execution
from alphalive.broker.base_broker import Order

ET = ZoneInfo("America/New_York")


@pytest.fixture
def mock_broker():
    """Create a mock broker."""
    broker = Mock()

    # Mock place_market_order
    order = Order(
        id="order_123",
        symbol="AAPL",
        qty=66.0,
        side="buy",
        order_type="market",
        limit_price=None,
        status="filled",
        filled_qty=66.0,
        filled_avg_price=150.0,
        submitted_at=datetime.now(ET),
        filled_at=datetime.now(ET)
    )
    broker.place_market_order = Mock(return_value=order)
    broker.place_limit_order = Mock(return_value=order)
    broker.close_position = Mock(return_value=order)

    return broker


@pytest.fixture
def mock_risk_manager():
    """Create a mock risk manager."""
    rm = Mock()
    rm.can_trade = Mock(return_value=(True, "OK"))
    rm.calculate_position_size = Mock(return_value=66)
    rm.check_stop_loss = Mock(return_value=False)
    rm.check_take_profit = Mock(return_value=False)
    rm.check_trailing_stop = Mock(return_value=False)
    rm.risk_config = Mock()
    rm.risk_config.trailing_stop_enabled = False
    return rm


@pytest.fixture
def sample_config():
    """Create a sample strategy config."""
    return StrategySchema(
        schema_version="1.0",
        strategy={"name": "ma_crossover", "parameters": {"fast_period": 10, "slow_period": 20}},
        ticker="AAPL",
        timeframe="1Day",
        risk={
            "stop_loss_pct": 2.0,
            "take_profit_pct": 5.0,
            "max_position_size_pct": 10.0,
            "max_daily_loss_pct": 3.0,
            "max_open_positions": 5,
            "portfolio_max_positions": 10
        },
        execution={
            "order_type": "market",
            "limit_offset_pct": 0.1,
            "cooldown_bars": 1
        },
        safety_limits={},
        metadata={
            "exported_from": "AlphaLab",
            "exported_at": "2024-01-01T00:00:00Z",
            "alphalab_version": "1.0.0",
            "backtest_id": "test",
            "backtest_period": {"start": "2022-01-01", "end": "2023-12-31"},
            "performance": {
                "sharpe_ratio": 1.5,
                "sortino_ratio": 2.0,
                "total_return_pct": 25.0,
                "max_drawdown_pct": 10.0,
                "win_rate_pct": 55.0,
                "profit_factor": 1.8,
                "total_trades": 100,
                "calmar_ratio": 2.5
            }
        }
    )


@pytest.fixture
def order_manager(mock_broker, mock_risk_manager, sample_config):
    """Create an OrderManager instance."""
    return OrderManager(
        broker=mock_broker,
        risk_manager=mock_risk_manager,
        config=sample_config,
        notifier=None,
        dry_run=False
    )


def test_order_manager_initialization(order_manager, sample_config):
    """Test OrderManager initialization."""
    assert order_manager.broker is not None
    assert order_manager.risk is not None
    assert order_manager.config == sample_config
    assert order_manager.order_history == []
    assert order_manager.pending_orders == {}


def test_execute_signal_success(order_manager, mock_risk_manager, mock_broker):
    """Test successful signal execution."""
    signal = {
        "signal": "BUY",
        "confidence": 0.8,
        "reason": "MA crossover",
        "indicators": {},
        "warmup_complete": True
    }

    result = order_manager.execute_signal(
        ticker="AAPL",
        signal=signal,
        current_price=150.0,
        account_equity=100000.0,
        current_positions_count=2,
        total_portfolio_positions=5,
        current_bar=100
    )

    assert result["status"] == "success"
    assert result["order_id"] == "order_123"
    assert result["filled_qty"] == 66.0
    assert result["filled_price"] == 150.0
    assert "slippage_pct" in result

    # Verify risk check was called
    mock_risk_manager.can_trade.assert_called_once()

    # Verify position sizing was called
    mock_risk_manager.calculate_position_size.assert_called_once()

    # Verify broker was called
    mock_broker.place_market_order.assert_called_once()

    # Verify order was recorded
    assert len(order_manager.order_history) == 1


def test_execute_signal_blocked_by_risk(order_manager, mock_risk_manager):
    """Test signal blocked by risk manager."""
    mock_risk_manager.can_trade = Mock(return_value=(False, "Daily loss limit hit"))

    signal = {"signal": "BUY", "confidence": 0.8, "reason": "Test"}

    result = order_manager.execute_signal(
        ticker="AAPL",
        signal=signal,
        current_price=150.0,
        account_equity=100000.0,
        current_positions_count=2,
        total_portfolio_positions=5
    )

    assert result["status"] == "blocked"
    assert "Daily loss limit hit" in result["reason"]
    assert len(order_manager.order_history) == 0


def test_execute_signal_hold(order_manager):
    """Test HOLD signal is ignored."""
    signal = {"signal": "HOLD", "confidence": 0.0, "reason": "No signal"}

    result = order_manager.execute_signal(
        ticker="AAPL",
        signal=signal,
        current_price=150.0,
        account_equity=100000.0,
        current_positions_count=2,
        total_portfolio_positions=5
    )

    assert result["status"] == "blocked"
    assert "Non-actionable" in result["reason"]


def test_execute_signal_zero_position_size(order_manager, mock_risk_manager):
    """Test signal with zero position size."""
    mock_risk_manager.calculate_position_size = Mock(return_value=0)

    signal = {"signal": "BUY", "confidence": 0.8, "reason": "Test"}

    result = order_manager.execute_signal(
        ticker="AAPL",
        signal=signal,
        current_price=150.0,
        account_equity=100000.0,
        current_positions_count=2,
        total_portfolio_positions=5
    )

    assert result["status"] == "blocked"
    assert "Position size = 0" in result["reason"]


def test_duplicate_order_prevention(order_manager, mock_risk_manager, mock_broker):
    """Test duplicate order prevention."""
    signal = {"signal": "BUY", "confidence": 0.8, "reason": "Test"}

    # Place first order
    result1 = order_manager.execute_signal(
        ticker="AAPL",
        signal=signal,
        current_price=150.0,
        account_equity=100000.0,
        current_positions_count=2,
        total_portfolio_positions=5
    )

    assert result1["status"] == "success"
    assert len(order_manager.order_history) == 1

    # Try to place duplicate order immediately
    result2 = order_manager.execute_signal(
        ticker="AAPL",
        signal=signal,
        current_price=150.0,
        account_equity=100000.0,
        current_positions_count=2,
        total_portfolio_positions=5
    )

    assert result2["status"] == "blocked"
    assert "Duplicate prevention" in result2["reason"]
    assert len(order_manager.order_history) == 1  # No new order


def test_check_recent_order(order_manager):
    """Test _check_recent_order method."""
    # No orders yet
    assert order_manager._check_recent_order("AAPL", "BUY") is None

    # Add an order
    order_manager.order_history.append({
        "ticker": "AAPL",
        "side": "BUY",
        "qty": 100,
        "price": 150.0,
        "order_id": "order_123",
        "timestamp": datetime.now(ET),
        "signal_reason": "Test"
    })

    # Should find recent order
    recent = order_manager._check_recent_order("AAPL", "BUY")
    assert recent is not None
    assert recent["order_id"] == "order_123"
    assert recent["age_seconds"] < 60

    # Different ticker should not match
    assert order_manager._check_recent_order("MSFT", "BUY") is None

    # Different side should not match
    assert order_manager._check_recent_order("AAPL", "SELL") is None


def test_generate_idempotency_key(order_manager):
    """Test idempotency key generation."""
    timestamp = datetime(2026, 3, 5, 9, 35, 0, tzinfo=ET)

    key = order_manager._generate_idempotency_key("AAPL", "buy", timestamp)

    assert key == "AAPL_buy_20260305_093500"


def test_calculate_limit_price(order_manager):
    """Test limit price calculation."""
    # BUY: add offset
    buy_price = order_manager._calculate_limit_price(100.0, "BUY", 0.1)
    assert buy_price == pytest.approx(100.1)

    # SELL: subtract offset
    sell_price = order_manager._calculate_limit_price(100.0, "SELL", 0.1)
    assert sell_price == pytest.approx(99.9)


def test_check_exits_stop_loss(order_manager, mock_risk_manager):
    """Test check_exits with stop loss."""
    mock_risk_manager.check_stop_loss = Mock(return_value=True)

    positions = [
        {"ticker": "AAPL", "avg_entry": 150.0, "side": "long"}
    ]

    current_prices = {"AAPL": 147.0}

    exits = order_manager.check_exits(positions, current_prices)

    assert len(exits) == 1
    assert exits[0]["ticker"] == "AAPL"
    assert "Stop loss hit" in exits[0]["reason"]


def test_check_exits_take_profit(order_manager, mock_risk_manager):
    """Test check_exits with take profit."""
    mock_risk_manager.check_stop_loss = Mock(return_value=False)
    mock_risk_manager.check_take_profit = Mock(return_value=True)

    positions = [
        {"ticker": "AAPL", "avg_entry": 150.0, "side": "long"}
    ]

    current_prices = {"AAPL": 157.5}

    exits = order_manager.check_exits(positions, current_prices)

    assert len(exits) == 1
    assert exits[0]["ticker"] == "AAPL"
    assert "Take profit hit" in exits[0]["reason"]


def test_check_exits_trailing_stop(order_manager, mock_risk_manager):
    """Test check_exits with trailing stop."""
    mock_risk_manager.check_stop_loss = Mock(return_value=False)
    mock_risk_manager.check_take_profit = Mock(return_value=False)
    mock_risk_manager.check_trailing_stop = Mock(return_value=True)
    mock_risk_manager.risk_config.trailing_stop_enabled = True

    positions = [
        {"ticker": "AAPL", "avg_entry": 150.0, "side": "long", "highest_since_entry": 160.0}
    ]

    current_prices = {"AAPL": 155.0}

    exits = order_manager.check_exits(positions, current_prices)

    assert len(exits) == 1
    assert exits[0]["ticker"] == "AAPL"
    assert "Trailing stop hit" in exits[0]["reason"]


def test_check_exits_no_exits(order_manager, mock_risk_manager):
    """Test check_exits with no exit conditions."""
    positions = [
        {"ticker": "AAPL", "avg_entry": 150.0, "side": "long"}
    ]

    current_prices = {"AAPL": 152.0}

    exits = order_manager.check_exits(positions, current_prices)

    assert len(exits) == 0


def test_check_exits_missing_price(order_manager, mock_risk_manager):
    """Test check_exits with missing current price."""
    positions = [
        {"ticker": "AAPL", "avg_entry": 150.0, "side": "long"}
    ]

    current_prices = {}  # No price for AAPL

    exits = order_manager.check_exits(positions, current_prices)

    assert len(exits) == 0  # Should skip position


def test_close_position(order_manager, mock_broker):
    """Test close_position method."""
    result = order_manager.close_position("AAPL", "Stop loss hit")

    assert result["status"] == "success"
    assert result["order_id"] == "order_123"
    mock_broker.close_position.assert_called_once_with("AAPL")


def test_close_position_error(order_manager, mock_broker):
    """Test close_position with error."""
    mock_broker.close_position = Mock(side_effect=Exception("Connection error"))

    result = order_manager.close_position("AAPL", "Stop loss hit")

    assert result["status"] == "error"
    assert "Connection error" in result["reason"]


def test_dry_run_mode():
    """Test dry run mode."""
    mock_broker = Mock()
    mock_risk_manager = Mock()
    mock_risk_manager.can_trade = Mock(return_value=(True, "OK"))
    mock_risk_manager.calculate_position_size = Mock(return_value=66)

    config = StrategySchema(
        schema_version="1.0",
        strategy={"name": "ma_crossover", "parameters": {"fast_period": 10, "slow_period": 20}},
        ticker="AAPL",
        timeframe="1Day",
        risk={
            "stop_loss_pct": 2.0,
            "take_profit_pct": 5.0,
            "max_position_size_pct": 10.0,
            "max_daily_loss_pct": 3.0,
            "max_open_positions": 5,
            "portfolio_max_positions": 10
        },
        execution={"order_type": "market"},
        safety_limits={},
        metadata={
            "exported_from": "Test",
            "exported_at": "2024-01-01T00:00:00Z",
            "alphalab_version": "1.0.0",
            "backtest_id": "test",
            "backtest_period": {"start": "2022-01-01", "end": "2023-12-31"},
            "performance": {
                "sharpe_ratio": 1.5,
                "sortino_ratio": 2.0,
                "total_return_pct": 25.0,
                "max_drawdown_pct": 10.0,
                "win_rate_pct": 55.0,
                "profit_factor": 1.8,
                "total_trades": 100,
                "calmar_ratio": 2.5
            }
        }
    )

    om = OrderManager(mock_broker, mock_risk_manager, config, dry_run=True)

    signal = {"signal": "BUY", "confidence": 0.8, "reason": "Test"}

    result = om.execute_signal(
        ticker="AAPL",
        signal=signal,
        current_price=150.0,
        account_equity=100000.0,
        current_positions_count=2,
        total_portfolio_positions=5
    )

    assert result["status"] == "success"
    assert "DRY_RUN" in result["order_id"]

    # Broker should NOT have been called
    mock_broker.place_market_order.assert_not_called()


def test_reset_daily(order_manager):
    """Test daily reset."""
    # Add some orders
    order_manager.order_history = [
        {"ticker": "AAPL", "side": "BUY", "order_id": "123"}
    ]
    order_manager.pending_orders = {"AAPL": "123"}

    # Reset
    order_manager.reset_daily()

    assert order_manager.order_history == []
    assert order_manager.pending_orders == {}


def test_get_order_history(order_manager):
    """Test get_order_history method."""
    order_manager.order_history = [
        {"ticker": "AAPL", "side": "BUY", "order_id": "123"}
    ]

    history = order_manager.get_order_history()

    assert len(history) == 1
    assert history[0]["ticker"] == "AAPL"

    # Should be a copy, not reference
    history.append({"ticker": "MSFT", "side": "SELL", "order_id": "456"})
    assert len(order_manager.order_history) == 1  # Original unchanged
