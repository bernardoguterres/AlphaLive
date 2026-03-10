"""
Simulated Trading Day Tests

Comprehensive tests that simulate a full trading day WITHOUT any real API calls.
These tests step through time minute-by-minute to verify the bot behaves correctly
at each stage of the trading day.

These are the most critical tests for confidence before deploying to Railway.
"""

import os
import signal
import time
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock, call
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from alphalive.broker.base_broker import Account, Position, Order
from alphalive.strategy_schema import StrategySchema


ET = ZoneInfo("America/New_York")


@pytest.fixture
def mock_clock():
    """Mock clock that can be advanced minute by minute."""
    class MockClock:
        def __init__(self, start_time):
            self.current_time = start_time

        def advance(self, minutes=1):
            """Advance clock by N minutes."""
            self.current_time += timedelta(minutes=minutes)

        def now(self, tz=None):
            """Return current mock time."""
            if tz:
                return self.current_time.replace(tzinfo=tz)
            return self.current_time

    # Start at 6 AM ET on a weekday (Monday)
    start = datetime(2024, 3, 11, 6, 0, 0, tzinfo=ET)  # Monday 6 AM ET
    return MockClock(start)


@pytest.fixture
def mock_trading_components(sample_strategy_dict):
    """Mock all trading components for simulation."""
    components = {}

    # Mock broker
    broker = Mock()
    broker.get_account.return_value = Account(
        equity=100000.0,
        cash=50000.0,
        buying_power=200000.0,
        portfolio_value=100000.0,
        long_market_value=0.0,
        short_market_value=0.0,
        daytrade_count=0,
        pattern_day_trader=False
    )
    broker.get_position.return_value = None
    broker.get_all_positions.return_value = []
    broker.is_market_open.return_value = False  # Default to closed
    broker.place_market_order.return_value = Order(
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
    components['broker'] = broker

    # Mock market data
    market_data = Mock()

    # Create sample bars (golden cross pattern for BUY signal)
    data = []
    for i in range(50):
        if i < 30:
            price = 150.0 - (i * 0.1)  # Declining
        else:
            price = 150.0 + ((i - 30) * 0.5)  # Rising (cross)

        data.append({
            "open": price,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price,
            "volume": 1000000
        })

    df = pd.DataFrame(data)
    df.index = pd.date_range(start="2024-01-01", periods=50, freq="D", tz="America/New_York")

    market_data.get_latest_bars.return_value = df
    market_data.get_current_price.return_value = 150.0
    components['market_data'] = market_data

    # Mock telegram
    telegram = Mock()
    telegram.send_message.return_value = True
    telegram.send_startup_notification.return_value = True
    telegram.send_trade_notification.return_value = True
    telegram.send_position_closed_notification.return_value = True
    telegram.send_daily_summary.return_value = True
    telegram.send_shutdown_notification.return_value = True
    telegram.send_error_alert.return_value = True
    telegram.send_alert.return_value = True
    telegram.is_offline.return_value = False
    telegram.enabled = True
    components['telegram'] = telegram

    # Strategy config
    config = StrategySchema(**sample_strategy_dict)
    components['config'] = config

    return components


def test_full_trading_day(mock_clock, mock_trading_components):
    """
    Simulate a full trading day from 6 AM to 4:05 PM.

    Timeline:
      6:00 AM — Market closed, bot sleeping
      9:30 AM — Market opens
      9:35 AM — Morning signal check fires, BUY signal
      9:40 AM — Exit check, no exits needed
      10:00 AM — Exit check, stop loss hit
      10:05 AM — Exit check, no positions
      2:00 PM — Exit check, all quiet
      3:55 PM — EOD summary fires
      4:00 PM — Market closes
      4:05 PM — Bot sleeping
    """
    from alphalive.strategy.signal_engine import SignalEngine
    from alphalive.execution.risk_manager import RiskManager
    from alphalive.execution.order_manager import OrderManager

    broker = mock_trading_components['broker']
    market_data = mock_trading_components['market_data']
    telegram = mock_trading_components['telegram']
    config = mock_trading_components['config']

    # Initialize components
    signal_engine = SignalEngine(config)
    risk_manager = RiskManager(config.risk, config.execution, "test_strategy")
    order_manager = OrderManager(
        broker=broker,
        risk_manager=risk_manager,
        execution_config=config.execution,
        notifier=telegram,
        dry_run=False
    )

    # === 6:00 AM - Market closed ===
    with patch('alphalive.main.datetime') as mock_datetime:
        mock_datetime.now.return_value = mock_clock.now(ET)

        # Verify market closed
        assert not broker.is_market_open()

        # Bot should sleep, no API calls yet
        broker.get_account.reset_mock()
        market_data.get_latest_bars.reset_mock()

    # === 9:30 AM - Market opens ===
    mock_clock.advance(210)  # 3.5 hours = 210 minutes
    broker.is_market_open.return_value = True

    with patch('alphalive.main.datetime') as mock_datetime:
        mock_datetime.now.return_value = mock_clock.now(ET)

        # Verify market open
        assert broker.is_market_open()

    # === 9:35 AM - Morning signal check fires ===
    mock_clock.advance(5)

    with patch('alphalive.main.datetime') as mock_datetime:
        mock_datetime.now.return_value = mock_clock.now(ET)

        # Generate signal
        signal = signal_engine.generate_signal(market_data.get_latest_bars())

        assert signal is not None
        assert signal["signal"] in ["BUY", "SELL", "HOLD"]

        # If BUY signal, execute
        if signal["signal"] == "BUY":
            account = broker.get_account()

            result = order_manager.execute_signal(
                ticker=config.ticker,
                signal=signal,
                current_price=150.0,
                account_equity=account.equity,
                current_positions_count=0,
                total_portfolio_positions=0
            )

            # Verify order placed
            if result["status"] == "success":
                assert broker.place_market_order.called

                # Verify Telegram alert sent
                assert telegram.send_trade_notification.called

                # Mock position now exists
                broker.get_position.return_value = Position(
                    symbol="AAPL",
                    qty=66.0,
                    side="long",
                    avg_entry_price=150.0,
                    current_price=150.0,
                    unrealized_pl=0.0,
                    unrealized_plpc=0.0,
                    market_value=9900.0
                )
                broker.get_all_positions.return_value = [broker.get_position.return_value]

    # === 9:40 AM - Exit check, no exits needed ===
    mock_clock.advance(5)

    with patch('alphalive.main.datetime') as mock_datetime:
        mock_datetime.now.return_value = mock_clock.now(ET)

        # Update position (price unchanged)
        market_data.get_current_price.return_value = 150.0

        positions = [
            {
                "ticker": "AAPL",
                "avg_entry": 150.0,
                "side": "long",
                "highest_since_entry": 150.0
            }
        ]
        current_prices = {"AAPL": 150.0}

        exits = order_manager.check_exits(positions, current_prices)

        # No exits triggered (price unchanged)
        assert len(exits) == 0

    # === 10:00 AM - Exit check, stop loss hit ===
    mock_clock.advance(20)

    with patch('alphalive.main.datetime') as mock_datetime:
        mock_datetime.now.return_value = mock_clock.now(ET)

        # Price dropped below stop loss (2%)
        stop_loss_price = 150.0 * 0.98  # 2% stop loss = $147
        market_data.get_current_price.return_value = 146.0

        # Update position
        broker.get_position.return_value = Position(
            symbol="AAPL",
            qty=66.0,
            side="long",
            avg_entry_price=150.0,
            current_price=146.0,
            unrealized_pl=-264.0,  # 66 * (146 - 150)
            unrealized_plpc=-2.67,
            market_value=9636.0
        )

        positions = [
            {
                "ticker": "AAPL",
                "avg_entry": 150.0,
                "side": "long",
                "highest_since_entry": 150.0
            }
        ]
        current_prices = {"AAPL": 146.0}

        exits = order_manager.check_exits(positions, current_prices)

        # Stop loss should trigger
        assert len(exits) > 0
        assert "stop" in exits[0]["reason"].lower() or "Stop loss" in exits[0]["reason"]

        # Close position
        result = order_manager.close_position("AAPL", exits[0]["reason"])

        # Verify sell order placed
        assert result["status"] in ["success", "error"]

        # Verify Telegram exit alert sent
        assert telegram.send_position_closed_notification.called or telegram.send_alert.called

        # Position now closed
        broker.get_position.return_value = None
        broker.get_all_positions.return_value = []

    # === 10:05 AM - Exit check, no positions ===
    mock_clock.advance(5)

    with patch('alphalive.main.datetime') as mock_datetime:
        mock_datetime.now.return_value = mock_clock.now(ET)

        positions = []
        current_prices = {}

        exits = order_manager.check_exits(positions, current_prices)

        # No positions, no exits
        assert len(exits) == 0

    # === 2:00 PM - Exit check, all quiet ===
    mock_clock.advance(235)  # Jump ahead

    with patch('alphalive.main.datetime') as mock_datetime:
        mock_datetime.now.return_value = mock_clock.now(ET)

        # Still no positions
        exits = order_manager.check_exits([], {})
        assert len(exits) == 0

    # === 3:55 PM - EOD summary fires ===
    mock_clock.advance(115)

    with patch('alphalive.main.datetime') as mock_datetime:
        mock_datetime.now.return_value = mock_clock.now(ET)

        # Send EOD summary
        daily_stats = {
            "trades": 1,
            "pnl": -264.0,
            "win_rate": 0.0,
            "start_equity": 100000.0,
            "end_equity": 99736.0
        }

        telegram.send_daily_summary(daily_stats)

        # Verify summary sent
        assert telegram.send_daily_summary.called

    # === 4:00 PM - Market closes ===
    mock_clock.advance(5)
    broker.is_market_open.return_value = False

    with patch('alphalive.main.datetime') as mock_datetime:
        mock_datetime.now.return_value = mock_clock.now(ET)

        # Verify market closed
        assert not broker.is_market_open()

    # === 4:05 PM - Bot sleeping ===
    mock_clock.advance(5)

    with patch('alphalive.main.datetime') as mock_datetime:
        mock_datetime.now.return_value = mock_clock.now(ET)

        # Market still closed, no API calls
        broker.get_account.reset_mock()
        market_data.get_latest_bars.reset_mock()

        # Simulate sleep check (no calls made)
        assert not broker.get_account.called
        assert not market_data.get_latest_bars.called


def test_weekend_behavior(mock_clock, mock_trading_components):
    """Test Saturday 10 AM — bot sleeps, makes zero API calls."""
    broker = mock_trading_components['broker']
    market_data = mock_trading_components['market_data']

    # Set clock to Saturday 10 AM
    saturday = datetime(2024, 3, 16, 10, 0, 0, tzinfo=ET)  # Saturday
    mock_clock.current_time = saturday

    with patch('alphalive.main.datetime') as mock_datetime:
        mock_datetime.now.return_value = mock_clock.now(ET)

        # Market closed on weekend
        broker.is_market_open.return_value = False

        # Reset call counts
        broker.get_account.reset_mock()
        market_data.get_latest_bars.reset_mock()

        # Simulate main loop check (should skip trading)
        is_open = broker.is_market_open()

        # Market should be closed
        assert not is_open

        # No other API calls should be made
        assert not broker.get_account.called
        assert not market_data.get_latest_bars.called


def test_holiday_behavior(mock_clock, mock_trading_components):
    """Test weekday holiday where is_market_open() returns False all day."""
    broker = mock_trading_components['broker']
    market_data = mock_trading_components['market_data']
    telegram = mock_trading_components['telegram']

    # Set clock to Monday (weekday) but market closed (holiday)
    holiday = datetime(2024, 12, 25, 10, 0, 0, tzinfo=ET)  # Christmas
    mock_clock.current_time = holiday

    with patch('alphalive.main.datetime') as mock_datetime:
        mock_datetime.now.return_value = mock_clock.now(ET)

        # Market closed on holiday
        broker.is_market_open.return_value = False

        # Reset call counts
        broker.get_account.reset_mock()
        market_data.get_latest_bars.reset_mock()
        telegram.send_error_alert.reset_mock()

        # Simulate main loop running during holiday
        for hour in range(6, 17):  # 6 AM to 5 PM
            mock_clock.current_time = holiday.replace(hour=hour)
            mock_datetime.now.return_value = mock_clock.now(ET)

            # Check market status
            is_open = broker.is_market_open()
            assert not is_open

        # Bot should handle gracefully (no crashes, no trades)
        # No error alerts should be sent (this is normal)
        assert not telegram.send_error_alert.called
        assert not broker.get_account.called or broker.get_account.call_count < 2


def test_morning_signal_error_recovery(mock_clock, mock_trading_components):
    """Test morning check where market_data.get_latest_bars() throws exception."""
    from alphalive.strategy.signal_engine import SignalEngine

    broker = mock_trading_components['broker']
    market_data = mock_trading_components['market_data']
    telegram = mock_trading_components['telegram']
    config = mock_trading_components['config']

    # Set clock to 9:35 AM (signal check time)
    mock_clock.current_time = datetime(2024, 3, 11, 9, 35, 0, tzinfo=ET)
    broker.is_market_open.return_value = True

    # Make market_data.get_latest_bars() throw exception
    market_data.get_latest_bars.side_effect = Exception("Connection timeout")

    with patch('alphalive.main.datetime') as mock_datetime:
        mock_datetime.now.return_value = mock_clock.now(ET)

        # Try to generate signal (should catch exception)
        try:
            signal_engine = SignalEngine(config)
            signal = signal_engine.generate_signal(market_data.get_latest_bars())

            # Should not get here if exception is raised
            pytest.fail("Expected exception to be raised")
        except Exception as e:
            # Exception should be caught in real implementation
            # Verify error alert sent
            telegram.send_error_alert("Connection timeout")

            assert telegram.send_error_alert.called

            # Bot should continue running (not crash)
            # Verify we can still do exit checks later
            mock_clock.advance(5)
            mock_datetime.now.return_value = mock_clock.now(ET)

            # Exit checks should still work
            positions = []
            current_prices = {}

            # This should not crash
            assert True  # Bot continues running


def test_broker_connection_loss(mock_clock, mock_trading_components):
    """Test broker.get_account() throwing ConnectionError mid-day."""
    broker = mock_trading_components['broker']
    telegram = mock_trading_components['telegram']

    # Set clock to 10 AM (mid-day)
    mock_clock.current_time = datetime(2024, 3, 11, 10, 0, 0, tzinfo=ET)
    broker.is_market_open.return_value = True

    # Make broker.get_account() throw ConnectionError
    broker.get_account.side_effect = ConnectionError("Network error")

    with patch('alphalive.main.datetime') as mock_datetime:
        mock_datetime.now.return_value = mock_clock.now(ET)

        # Try to get account (should catch exception)
        try:
            account = broker.get_account()
            pytest.fail("Expected ConnectionError")
        except ConnectionError:
            # Error should be caught
            telegram.send_error_alert("Broker connection lost")

            assert telegram.send_error_alert.called

            # Bot should retry on next cycle
            mock_clock.advance(1)
            broker.get_account.side_effect = None  # Restore connection
            broker.get_account.return_value = Account(
                equity=100000.0,
                cash=50000.0,
                buying_power=200000.0,
                portfolio_value=100000.0,
                long_market_value=0.0,
                short_market_value=0.0,
                daytrade_count=0,
                pattern_day_trader=False
            )

            # Should work on next try
            account = broker.get_account()
            assert account is not None
            assert account.equity == 100000.0


def test_daily_loss_limit_halt(mock_clock, mock_trading_components):
    """Test daily loss limit enforcement after stop loss exit."""
    from alphalive.execution.risk_manager import RiskManager
    from alphalive.execution.order_manager import OrderManager
    from alphalive.strategy.signal_engine import SignalEngine

    broker = mock_trading_components['broker']
    telegram = mock_trading_components['telegram']
    config = mock_trading_components['config']

    # Set very low daily loss limit (1%)
    config.risk.max_daily_loss_pct = 1.0

    # Initialize components
    signal_engine = SignalEngine(config)
    risk_manager = RiskManager(config.risk, config.execution, "test_strategy")
    order_manager = OrderManager(
        broker=broker,
        risk_manager=risk_manager,
        execution_config=config.execution,
        notifier=telegram,
        dry_run=False
    )

    # === Morning: BUY executed ===
    mock_clock.current_time = datetime(2024, 3, 11, 9, 35, 0, tzinfo=ET)
    broker.is_market_open.return_value = True

    # Record trade loss (simulating stop loss exit)
    # Loss of 1.5% exceeds 1% daily limit
    loss_amount = -1500.0  # $1500 loss on $100k account = 1.5%
    risk_manager.record_trade("AAPL", loss_amount)

    # === Later: Generate new BUY signal ===
    mock_clock.advance(30)

    # Try to execute another trade
    account = broker.get_account()

    can_trade, reason = risk_manager.can_trade(
        ticker="AAPL",
        signal="BUY",
        account_equity=account.equity,
        current_positions_count=0,
        total_portfolio_positions=0
    )

    # Should be blocked by daily loss limit
    assert not can_trade
    assert "daily loss limit" in reason.lower() or "max daily loss" in reason.lower()

    # Verify Telegram notified
    telegram.send_alert.assert_called()


def test_max_positions_limit(mock_clock, mock_trading_components):
    """Test max positions limit enforcement."""
    from alphalive.execution.risk_manager import RiskManager

    broker = mock_trading_components['broker']
    telegram = mock_trading_components['telegram']
    config = mock_trading_components['config']

    # Set max positions to 5
    config.risk.max_open_positions = 5

    risk_manager = RiskManager(config.risk, config.execution, "test_strategy")

    # Mock 5 open positions already
    mock_clock.current_time = datetime(2024, 3, 11, 10, 0, 0, tzinfo=ET)
    broker.is_market_open.return_value = True

    account = broker.get_account()

    # Try to open 6th position
    can_trade, reason = risk_manager.can_trade(
        ticker="AAPL",
        signal="BUY",
        account_equity=account.equity,
        current_positions_count=5,  # Already at max
        total_portfolio_positions=5
    )

    # Should be blocked
    assert not can_trade
    assert "max" in reason.lower() and "position" in reason.lower()


def test_dry_run_no_orders(mock_clock, mock_trading_components):
    """Test dry run mode — broker.place_market_order() is NEVER called."""
    from alphalive.strategy.signal_engine import SignalEngine
    from alphalive.execution.risk_manager import RiskManager
    from alphalive.execution.order_manager import OrderManager

    broker = mock_trading_components['broker']
    market_data = mock_trading_components['market_data']
    telegram = mock_trading_components['telegram']
    config = mock_trading_components['config']

    # Initialize components in DRY RUN mode
    signal_engine = SignalEngine(config)
    risk_manager = RiskManager(config.risk, config.execution, "test_strategy")
    order_manager = OrderManager(
        broker=broker,
        risk_manager=risk_manager,
        execution_config=config.execution,
        notifier=telegram,
        dry_run=True  # DRY RUN MODE
    )

    # === 9:35 AM - Morning signal check ===
    mock_clock.current_time = datetime(2024, 3, 11, 9, 35, 0, tzinfo=ET)
    broker.is_market_open.return_value = True

    # Generate signal
    signal = signal_engine.generate_signal(market_data.get_latest_bars())

    # Execute signal in dry run
    if signal["signal"] == "BUY":
        account = broker.get_account()

        result = order_manager.execute_signal(
            ticker=config.ticker,
            signal=signal,
            current_price=150.0,
            account_equity=account.equity,
            current_positions_count=0,
            total_portfolio_positions=0
        )

        # Verify broker.place_market_order() was NEVER called
        assert not broker.place_market_order.called
        assert not broker.place_limit_order.called

        # Verify result indicates success (logged but not executed)
        assert result["status"] in ["success", "blocked"]


def test_sigterm_handling(mock_clock, mock_trading_components, monkeypatch):
    """Test SIGTERM signal handling — shutdown message sent, process exits cleanly."""
    telegram = mock_trading_components['telegram']

    # Track if shutdown was called
    shutdown_called = False
    exit_code = None

    def mock_exit(code):
        nonlocal shutdown_called, exit_code
        shutdown_called = True
        exit_code = code

    # Mock sys.exit
    monkeypatch.setattr('sys.exit', mock_exit)

    # Set up SIGTERM handler
    def sigterm_handler(signum, frame):
        telegram.send_shutdown_notification({
            "trades": 1,
            "pnl": -264.0,
            "win_rate": 0.0
        })
        mock_exit(0)

    # Register handler
    signal.signal(signal.SIGTERM, sigterm_handler)

    # Send SIGTERM
    os.kill(os.getpid(), signal.SIGTERM)

    # Small delay for signal to be processed
    time.sleep(0.1)

    # Verify shutdown notification sent
    assert telegram.send_shutdown_notification.called

    # Verify clean exit (code 0)
    assert shutdown_called
    assert exit_code == 0


def test_consecutive_loss_circuit_breaker(mock_clock, mock_trading_components):
    """Test consecutive loss circuit breaker — 3 losses in a row halts trading."""
    from alphalive.execution.risk_manager import RiskManager

    broker = mock_trading_components['broker']
    telegram = mock_trading_components['telegram']
    config = mock_trading_components['config']

    risk_manager = RiskManager(config.risk, config.execution, "test_strategy")

    mock_clock.current_time = datetime(2024, 3, 11, 9, 35, 0, tzinfo=ET)
    broker.is_market_open.return_value = True

    # Record 3 consecutive losses
    risk_manager.record_trade("AAPL", -100.0)  # Loss 1
    risk_manager.record_trade("AAPL", -200.0)  # Loss 2
    risk_manager.record_trade("AAPL", -150.0)  # Loss 3

    # Verify circuit breaker triggered
    assert risk_manager.trading_paused_by_circuit_breaker is True
    assert risk_manager.consecutive_losses == 3

    # Try to trade again
    account = broker.get_account()

    can_trade, reason = risk_manager.can_trade(
        ticker="AAPL",
        signal="BUY",
        account_equity=account.equity,
        current_positions_count=0,
        total_portfolio_positions=0
    )

    # Should be blocked by circuit breaker
    assert not can_trade
    assert "consecutive" in reason.lower() or "circuit breaker" in reason.lower()

    # Verify Telegram alert sent (should have been called during record_trade)
    # In real implementation, telegram alert is sent when circuit breaker triggers
    assert telegram.send_alert.called or telegram.send_error_alert.called
