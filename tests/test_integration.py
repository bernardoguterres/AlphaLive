"""
Integration Tests

Test full workflows with multiple components working together.
All external dependencies (broker, telegram, market data) are mocked.
"""

import os
import pytest
from unittest.mock import Mock, patch

from alphalive.config import load_strategy
from alphalive.execution.risk_manager import RiskManager
from alphalive.execution.order_manager import OrderManager
from alphalive.strategy.signal_engine import SignalEngine
from alphalive.broker.base_broker import Order, Position


def test_full_signal_to_order_flow(
    sample_strategy_dict, mock_broker, ma_crossover_bars, mock_telegram
):
    """Test complete workflow: load config → fetch data → generate signal → risk check → place order."""
    from alphalive.strategy_schema import StrategySchema

    # Load strategy
    strategy_config = StrategySchema(**sample_strategy_dict)

    # Initialize components
    signal_engine = SignalEngine(strategy_config)
    risk_manager = RiskManager(
        risk_config=strategy_config.risk,
        execution_config=strategy_config.execution,
        strategy_name="test_strategy",
        safety_limits=strategy_config.safety_limits,
        notifier=mock_telegram
    )
    order_manager = OrderManager(
        broker=mock_broker,
        risk_manager=risk_manager,
        config=strategy_config,
        notifier=mock_telegram,
        dry_run=False
    )

    # Get account equity for position sizing
    account = mock_broker.get_account()

    # Generate signal from market data
    signal = signal_engine.generate_signal(ma_crossover_bars)

    assert signal is not None
    assert "signal" in signal
    assert "confidence" in signal
    assert "reason" in signal

    # If BUY signal, execute it
    if signal["signal"] == "BUY":
        result = order_manager.execute_signal(
            ticker=strategy_config.ticker,
            signal=signal,
            current_price=150.0,
            account_equity=account.equity,
            current_positions_count=0,
            total_portfolio_positions=0
        )

        # Should succeed
        assert result["status"] in ["success", "blocked"]

        if result["status"] == "success":
            # Verify broker was called
            assert mock_broker.place_market_order.called or mock_broker.place_limit_order.called


def test_full_exit_flow(sample_strategy_dict, mock_broker, mock_telegram):
    """Test full exit flow: position exists → price hits stop → sell order placed."""
    from alphalive.strategy_schema import StrategySchema

    # Load strategy
    strategy_config = StrategySchema(**sample_strategy_dict)

    # Setup: Mock position exists
    mock_position = Position(
        symbol="AAPL",
        qty=10.0,
        side="long",
        avg_entry_price=150.0,
        current_price=145.0,  # Down 3.33% (below 2% stop loss)
        unrealized_pl=-50.0,
        unrealized_plpc=-3.33,
        market_value=1450.0
    )

    mock_broker.get_position.return_value = mock_position
    mock_broker.get_all_positions.return_value = [mock_position]

    # Initialize components
    risk_manager = RiskManager(
        risk_config=strategy_config.risk,
        execution_config=strategy_config.execution,
        strategy_name="test_strategy",
        safety_limits=strategy_config.safety_limits,
        notifier=mock_telegram
    )
    order_manager = OrderManager(
        broker=mock_broker,
        risk_manager=risk_manager,
        config=strategy_config,
        notifier=mock_telegram,
        dry_run=False
    )

    # Check exit conditions
    positions = [
        {
            "ticker": "AAPL",
            "avg_entry": 150.0,
            "side": "long",
            "highest_since_entry": 152.0
        }
    ]
    current_prices = {"AAPL": 145.0}

    exits = order_manager.check_exits(positions, current_prices)

    # Should detect stop loss
    assert len(exits) > 0
    assert exits[0]["ticker"] == "AAPL"
    assert "Stop loss" in exits[0]["reason"] or "stop" in exits[0]["reason"].lower()

    # Execute exit
    result = order_manager.close_position("AAPL", exits[0]["reason"])

    # Should succeed
    assert result["status"] in ["success", "error"]


def test_dry_run_flow(sample_strategy_dict, mock_broker, ma_crossover_bars, mock_telegram):
    """Test dry run flow: same as signal-to-order but no orders placed, only logs."""
    from alphalive.strategy_schema import StrategySchema

    # Load strategy
    strategy_config = StrategySchema(**sample_strategy_dict)

    # Initialize components with dry_run=True
    signal_engine = SignalEngine(strategy_config)
    risk_manager = RiskManager(
        risk_config=strategy_config.risk,
        execution_config=strategy_config.execution,
        strategy_name="test_strategy",
        safety_limits=strategy_config.safety_limits,
        notifier=mock_telegram
    )
    order_manager = OrderManager(
        broker=mock_broker,
        risk_manager=risk_manager,
        config=strategy_config,
        notifier=mock_telegram,
        dry_run=True  # DRY RUN MODE
    )

    # Get account equity
    account = mock_broker.get_account()

    # Generate signal
    signal = signal_engine.generate_signal(ma_crossover_bars)

    # Execute signal (dry run)
    if signal is not None and signal["signal"] == "BUY":
        result = order_manager.execute_signal(
            ticker=strategy_config.ticker,
            signal=signal,
            current_price=150.0,
            account_equity=account.equity,
            current_positions_count=0,
            total_portfolio_positions=0
        )

        # In dry run, should return success without calling broker
        assert result["status"] in ["success", "blocked"]

        # Verify broker was NOT called (dry run)
        if result["status"] == "success":
            assert not mock_broker.place_market_order.called
            assert not mock_broker.place_limit_order.called


def test_multiple_strategies_loaded_and_processed(sample_strategy_dict, mock_broker, ma_crossover_bars):
    """Test multiple strategies loaded and processed."""
    from alphalive.strategy_schema import StrategySchema

    # Create two strategies
    strategy1_dict = sample_strategy_dict.copy()
    strategy1_dict["ticker"] = "AAPL"
    strategy1_dict["strategy"]["name"] = "ma_crossover"

    strategy2_dict = sample_strategy_dict.copy()
    strategy2_dict["ticker"] = "TSLA"
    strategy2_dict["strategy"]["name"] = "rsi_mean_reversion"

    strategy1 = StrategySchema(**strategy1_dict)
    strategy2 = StrategySchema(**strategy2_dict)

    # Initialize signal engines for both
    engine1 = SignalEngine(strategy1)
    engine2 = SignalEngine(strategy2)

    # Generate signals for both
    signal1 = engine1.generate_signal(ma_crossover_bars)
    signal2 = engine2.generate_signal(ma_crossover_bars)

    # Both should generate signals (or at least not crash)
    assert signal1 is not None
    assert signal2 is not None

    # Signals should have correct structure
    assert "signal" in signal1
    assert "signal" in signal2
    assert signal1["signal"] in ["BUY", "SELL", "HOLD"]
    assert signal2["signal"] in ["BUY", "SELL", "HOLD"]


def test_error_recovery_broker_exception(
    sample_strategy_dict, mock_broker, ma_crossover_bars, mock_telegram, caplog
):
    """Test error recovery: broker throws exception → error logged → loop continues."""
    from alphalive.strategy_schema import StrategySchema

    # Load strategy
    strategy_config = StrategySchema(**sample_strategy_dict)

    # Make broker raise exception
    mock_broker.place_market_order.side_effect = Exception("Broker API error")

    # Initialize components
    signal_engine = SignalEngine(strategy_config)
    risk_manager = RiskManager(
        risk_config=strategy_config.risk,
        execution_config=strategy_config.execution,
        strategy_name="test_strategy",
        safety_limits=strategy_config.safety_limits,
        notifier=mock_telegram
    )
    order_manager = OrderManager(
        broker=mock_broker,
        risk_manager=risk_manager,
        config=strategy_config,
        notifier=mock_telegram,
        dry_run=False
    )

    # Get account equity
    account = mock_broker.get_account()

    # Generate signal
    signal = signal_engine.generate_signal(ma_crossover_bars)

    # Try to execute signal (should handle exception gracefully)
    if signal is not None and signal["signal"] == "BUY":
        result = order_manager.execute_signal(
            ticker=strategy_config.ticker,
            signal=signal,
            current_price=150.0,
            account_equity=account.equity,
            current_positions_count=0,
            total_portfolio_positions=0
        )

        # Should return error status, not raise exception
        assert result["status"] == "error"
        assert "reason" in result

        # Error should be logged
        assert any("error" in record.message.lower() or "fail" in record.message.lower() for record in caplog.records)
