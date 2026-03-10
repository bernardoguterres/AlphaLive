"""
Test Telegram Command Listener

Tests for alphalive/notifications/telegram_commands.py — inbound Telegram commands.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
from zoneinfo import ZoneInfo

from alphalive.notifications.telegram_commands import TelegramCommandListener
from alphalive.broker.base_broker import Position, Account

ET = ZoneInfo("America/New_York")


@pytest.fixture
def mock_components():
    """Create mock components for command listener."""
    # Mock order manager
    order_manager = Mock()
    order_manager.order_history = [
        {
            'timestamp': datetime(2024, 3, 11, 9, 35, tzinfo=ET),
            'side': 'buy',
            'ticker': 'AAPL'
        }
    ]
    order_manager.close_position = Mock(return_value={"status": "success", "order_id": "123"})

    # Mock risk manager
    risk_manager = Mock()
    risk_manager.daily_pnl = 145.30
    risk_manager.daily_trades = [
        {'ticker': 'AAPL', 'pnl': 200.0},
        {'ticker': 'MSFT', 'pnl': -54.70}
    ]
    risk_manager.consecutive_losses = 0
    risk_manager.trading_paused_manual = False

    # Mock broker
    broker = Mock()
    broker.paper = True
    broker.get_account = Mock(return_value=Account(
        equity=100000.0,
        cash=50000.0,
        buying_power=200000.0,
        portfolio_value=100000.0,
        long_market_value=50000.0,
        short_market_value=0.0,
        daytrade_count=0,
        pattern_day_trader=False,
        account_status="ACTIVE"
    ))
    broker.get_all_positions = Mock(return_value=[
        Position(
            symbol="AAPL",
            qty=10,
            side="long",
            avg_entry_price=150.0,
            current_price=151.8,
            unrealized_pl=18.0,
            unrealized_plpc=1.2,
            market_value=1518.0
        )
    ])

    # Mock notifier
    notifier = Mock()
    notifier.send_message = Mock(return_value=True)

    # Mock config
    config = Mock()
    config.strategy.name = "ma_crossover"
    config.ticker = "AAPL"
    config.timeframe = "1Day"
    config.risk.stop_loss_pct = 2.0
    config.risk.take_profit_pct = 5.0
    config.risk.max_position_size_pct = 10.0
    config.risk.max_daily_loss_pct = 3.0
    config.risk.max_open_positions = 5
    config.risk.trailing_stop_enabled = False
    config.risk.trailing_stop_pct = 3.0
    config.execution.order_type = "market"
    config.execution.limit_offset_pct = 0.1
    config.execution.cooldown_bars = 1

    return {
        'order_manager': order_manager,
        'risk_manager': risk_manager,
        'broker': broker,
        'notifier': notifier,
        'config': config
    }


def test_status_command(mock_components):
    """Test /status command returns formatted status."""
    listener = TelegramCommandListener(
        bot_token="test_token",
        chat_id="123456",
        order_manager=mock_components['order_manager'],
        risk_manager=mock_components['risk_manager'],
        broker=mock_components['broker'],
        notifier=mock_components['notifier'],
        config=mock_components['config']
    )

    # Handle /status command
    listener._handle_command("/status")

    # Verify notifier called
    assert mock_components['notifier'].send_message.called

    # Get the message sent
    call_args = mock_components['notifier'].send_message.call_args
    message = call_args[0][0]

    # Verify message contains key info
    assert "AlphaLive Status" in message
    assert "Paper Trading" in message
    assert "ma_crossover" in message
    assert "AAPL" in message
    assert "+$145.30" in message  # Daily P&L
    assert "10 shares" in message  # Position


def test_pause_resume(mock_components):
    """Test /pause and /resume toggle trading flag."""
    listener = TelegramCommandListener(
        bot_token="test_token",
        chat_id="123456",
        order_manager=mock_components['order_manager'],
        risk_manager=mock_components['risk_manager'],
        broker=mock_components['broker'],
        notifier=mock_components['notifier'],
        config=mock_components['config']
    )

    # Initially not paused
    assert mock_components['risk_manager'].trading_paused_manual is False

    # Pause
    listener._handle_command("/pause")
    assert mock_components['risk_manager'].trading_paused_manual is True
    assert mock_components['notifier'].send_message.called

    # Verify pause message
    call_args = mock_components['notifier'].send_message.call_args
    message = call_args[0][0]
    assert "Trading Paused" in message
    assert "/resume" in message

    # Resume
    listener._handle_command("/resume")
    assert mock_components['risk_manager'].trading_paused_manual is False

    # Verify resume message
    call_args = mock_components['notifier'].send_message.call_args
    message = call_args[0][0]
    assert "Trading Resumed" in message


def test_unknown_command(mock_components):
    """Test unknown command sends /help suggestion."""
    listener = TelegramCommandListener(
        bot_token="test_token",
        chat_id="123456",
        order_manager=mock_components['order_manager'],
        risk_manager=mock_components['risk_manager'],
        broker=mock_components['broker'],
        notifier=mock_components['notifier'],
        config=mock_components['config']
    )

    # Send unknown command
    listener._handle_command("/foobar")

    # Verify notifier called
    assert mock_components['notifier'].send_message.called

    # Verify message suggests /help
    call_args = mock_components['notifier'].send_message.call_args
    message = call_args[0][0]
    assert "Unknown command" in message
    assert "/help" in message


def test_wrong_chat_ignored(mock_components):
    """Test messages from other chats are ignored."""
    listener = TelegramCommandListener(
        bot_token="test_token",
        chat_id="123456",  # Only respond to this chat
        order_manager=mock_components['order_manager'],
        risk_manager=mock_components['risk_manager'],
        broker=mock_components['broker'],
        notifier=mock_components['notifier'],
        config=mock_components['config']
    )

    # Mock getUpdates response with message from different chat
    with patch('httpx.get') as mock_get:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "ok": True,
            "result": [
                {
                    "update_id": 1,
                    "message": {
                        "chat": {"id": 999999},  # Different chat
                        "text": "/status"
                    }
                }
            ]
        }
        mock_get.return_value = mock_response

        # Start listener
        listener.start()

        # Wait briefly for poll
        import time
        time.sleep(0.1)

        # Stop listener
        listener.stop()

        # Verify notifier NOT called (message ignored)
        mock_components['notifier'].send_message.assert_not_called()


def test_close_all_confirmation_flow(mock_components):
    """Test /close_all requires confirmation."""
    listener = TelegramCommandListener(
        bot_token="test_token",
        chat_id="123456",
        order_manager=mock_components['order_manager'],
        risk_manager=mock_components['risk_manager'],
        broker=mock_components['broker'],
        notifier=mock_components['notifier'],
        config=mock_components['config']
    )

    # Send /close_all
    listener._handle_command("/close_all")

    # Verify confirmation requested
    assert listener._pending_close_all is True
    call_args = mock_components['notifier'].send_message.call_args
    message = call_args[0][0]
    assert "Close ALL Positions?" in message
    assert "/confirm_close" in message

    # Verify positions NOT closed yet
    mock_components['order_manager'].close_position.assert_not_called()

    # Send /confirm_close
    listener._handle_command("/confirm_close")

    # Verify pending flag cleared
    assert listener._pending_close_all is False

    # Verify position closed
    mock_components['order_manager'].close_position.assert_called_once_with(
        "AAPL",
        reason="Manual close via Telegram /close_all"
    )

    # Verify confirmation message sent
    call_args = mock_components['notifier'].send_message.call_args
    message = call_args[0][0]
    assert "Positions Closed" in message
    assert "AAPL" in message


def test_config_command(mock_components):
    """Test /config command returns strategy config."""
    listener = TelegramCommandListener(
        bot_token="test_token",
        chat_id="123456",
        order_manager=mock_components['order_manager'],
        risk_manager=mock_components['risk_manager'],
        broker=mock_components['broker'],
        notifier=mock_components['notifier'],
        config=mock_components['config']
    )

    # Send /config
    listener._handle_command("/config")

    # Verify notifier called
    assert mock_components['notifier'].send_message.called

    # Get message
    call_args = mock_components['notifier'].send_message.call_args
    message = call_args[0][0]

    # Verify config details
    assert "ma_crossover" in message
    assert "AAPL" in message
    assert "1Day" in message
    assert "2.0%" in message  # Stop loss
    assert "5.0%" in message  # Take profit
    assert "Trailing Stop: Off" in message


def test_performance_command(mock_components):
    """Test /performance command returns stats."""
    listener = TelegramCommandListener(
        bot_token="test_token",
        chat_id="123456",
        order_manager=mock_components['order_manager'],
        risk_manager=mock_components['risk_manager'],
        broker=mock_components['broker'],
        notifier=mock_components['notifier'],
        config=mock_components['config']
    )

    # Send /performance
    listener._handle_command("/performance")

    # Verify notifier called
    assert mock_components['notifier'].send_message.called

    # Get message
    call_args = mock_components['notifier'].send_message.call_args
    message = call_args[0][0]

    # Verify performance stats
    assert "Performance" in message
    assert "2" in message  # Total trades
    assert "1W / 1L" in message  # Win/loss
    assert "50.0%" in message  # Win rate
    assert "AAPL" in message  # Best trade
    assert "MSFT" in message  # Worst trade


def test_help_command(mock_components):
    """Test /help command lists all commands."""
    listener = TelegramCommandListener(
        bot_token="test_token",
        chat_id="123456",
        order_manager=mock_components['order_manager'],
        risk_manager=mock_components['risk_manager'],
        broker=mock_components['broker'],
        notifier=mock_components['notifier'],
        config=mock_components['config']
    )

    # Send /help
    listener._handle_command("/help")

    # Verify notifier called
    assert mock_components['notifier'].send_message.called

    # Get message
    call_args = mock_components['notifier'].send_message.call_args
    message = call_args[0][0]

    # Verify all commands listed
    assert "/status" in message
    assert "/pause" in message
    assert "/resume" in message
    assert "/close_all" in message
    assert "/config" in message
    assert "/performance" in message
    assert "/help" in message


def test_start_stop_thread(mock_components):
    """Test listener starts and stops thread correctly."""
    listener = TelegramCommandListener(
        bot_token="test_token",
        chat_id="123456",
        order_manager=mock_components['order_manager'],
        risk_manager=mock_components['risk_manager'],
        broker=mock_components['broker'],
        notifier=mock_components['notifier'],
        config=mock_components['config']
    )

    # Start listener
    listener.start()

    # Verify thread created and running
    assert listener.thread is not None
    assert listener.thread.is_alive()
    assert listener._running is True

    # Stop listener
    listener.stop()

    # Verify stopped
    assert listener._running is False
