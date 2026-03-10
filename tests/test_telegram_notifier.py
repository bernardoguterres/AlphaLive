"""
Test Telegram Notifier (B7)

Tests for TelegramNotifier with retry logic, graceful degradation,
and background retry functionality.
"""

import time
import pytest
from unittest.mock import Mock, patch, MagicMock

from alphalive.notifications.telegram_bot import TelegramNotifier


@pytest.fixture
def mock_httpx_success():
    """Mock successful httpx response."""
    with patch('httpx.post') as mock_post:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = '{"ok": true}'
        mock_post.return_value = mock_response
        yield mock_post


@pytest.fixture
def mock_httpx_failure():
    """Mock failed httpx response."""
    with patch('httpx.post') as mock_post:
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.text = '{"ok": false, "description": "Bad Request"}'
        mock_post.return_value = mock_response
        yield mock_post


@pytest.fixture
def mock_httpx_exception():
    """Mock httpx exception."""
    with patch('httpx.post') as mock_post:
        mock_post.side_effect = Exception("Connection timeout")
        yield mock_post


def test_telegram_notifier_initialization():
    """Test TelegramNotifier initialization."""
    notifier = TelegramNotifier(
        bot_token="test_token",
        chat_id="123456",
        enabled=True
    )

    assert notifier.bot_token == "test_token"
    assert notifier.chat_id == "123456"
    assert notifier.enabled is True
    assert notifier.consecutive_failures == 0
    assert notifier.telegram_offline is False
    assert notifier.api_url == "https://api.telegram.org/bottest_token/sendMessage"


def test_telegram_notifier_disabled():
    """Test TelegramNotifier with no credentials."""
    notifier = TelegramNotifier(bot_token=None, chat_id=None)

    assert notifier.enabled is False
    assert notifier.send_message("Test") is False


def test_send_message_success(mock_httpx_success):
    """Test successful message send."""
    notifier = TelegramNotifier("test_token", "123456")

    result = notifier.send_message("Test message")

    assert result is True
    assert notifier.consecutive_failures == 0
    assert notifier.telegram_offline is False

    # Verify httpx was called correctly
    mock_httpx_success.assert_called_once()
    call_args = mock_httpx_success.call_args
    assert call_args[1]['json']['chat_id'] == "123456"
    assert call_args[1]['json']['text'] == "Test message"
    assert call_args[1]['json']['parse_mode'] == "HTML"


def test_send_message_with_retry(mock_httpx_failure):
    """Test message send with retries."""
    notifier = TelegramNotifier("test_token", "123456")

    # Mock will fail on all attempts
    result = notifier.send_message("Test message")

    assert result is False
    assert notifier.consecutive_failures == 1

    # Should have tried 3 times
    assert mock_httpx_failure.call_count == 3


def test_send_message_graceful_degradation():
    """Test graceful degradation after 3 failures."""
    notifier = TelegramNotifier("test_token", "123456")

    with patch('httpx.post') as mock_post:
        mock_post.side_effect = Exception("Connection error")

        # First failure
        result1 = notifier.send_message("Test 1")
        assert result1 is False
        assert notifier.consecutive_failures == 1
        assert notifier.telegram_offline is False

        # Second failure
        result2 = notifier.send_message("Test 2")
        assert result2 is False
        assert notifier.consecutive_failures == 2
        assert notifier.telegram_offline is False

        # Third failure - should mark offline
        result3 = notifier.send_message("Test 3")
        assert result3 is False
        assert notifier.consecutive_failures == 3
        assert notifier.telegram_offline is True


def test_send_message_background_retry():
    """Test background retry after 10 minutes."""
    notifier = TelegramNotifier("test_token", "123456")

    # Mark as offline
    notifier.telegram_offline = True
    notifier.consecutive_failures = 3
    notifier.last_retry_attempt = time.time() - 700  # 11+ minutes ago

    with patch('httpx.post') as mock_post:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        # Should attempt background retry and succeed
        result = notifier.send_message("Test")

        assert result is True
        assert notifier.telegram_offline is False
        assert notifier.consecutive_failures == 0


def test_send_message_skip_during_offline():
    """Test message skip when offline and retry not due."""
    notifier = TelegramNotifier("test_token", "123456")

    # Mark as offline recently
    notifier.telegram_offline = True
    notifier.consecutive_failures = 3
    notifier.last_retry_attempt = time.time()  # Just now

    with patch('httpx.post') as mock_post:
        # Should not attempt send
        result = notifier.send_message("Test")

        assert result is False
        mock_post.assert_not_called()


def test_send_message_restore_after_offline():
    """Test connection restoration after offline period."""
    notifier = TelegramNotifier("test_token", "123456")

    # Mark as offline
    notifier.telegram_offline = True
    notifier.consecutive_failures = 3

    with patch('httpx.post') as mock_post:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        # Force background retry by setting old timestamp
        notifier.last_retry_attempt = time.time() - 700

        result = notifier.send_message("Test")

        assert result is True
        assert notifier.telegram_offline is False
        assert notifier.consecutive_failures == 0


def test_send_startup_notification(mock_httpx_success):
    """Test startup notification."""
    notifier = TelegramNotifier("test_token", "123456")

    config = {
        "timeframe": "1Day",
        "stop_loss_pct": 2.0,
        "take_profit_pct": 5.0,
        "max_position_size_pct": 10.0,
        "max_daily_loss_pct": 3.0
    }

    notifier.send_startup_notification("ma_crossover", "AAPL", config)

    # Verify message was sent
    mock_httpx_success.assert_called_once()
    call_args = mock_httpx_success.call_args
    message = call_args[1]['json']['text']

    assert "AlphaLive Started" in message
    assert "ma_crossover" in message
    assert "AAPL" in message
    assert "2.0%" in message  # stop loss


def test_send_shutdown_notification(mock_httpx_success):
    """Test shutdown notification."""
    notifier = TelegramNotifier("test_token", "123456")

    stats = {
        "trades": 5,
        "pnl": 450.0,
        "win_rate": 60.0
    }

    notifier.send_shutdown_notification(stats)

    mock_httpx_success.assert_called_once()
    call_args = mock_httpx_success.call_args
    message = call_args[1]['json']['text']

    assert "AlphaLive Stopped" in message
    assert "5" in message  # trades
    assert "450.00" in message  # pnl


def test_send_trade_notification(mock_httpx_success):
    """Test trade notification."""
    notifier = TelegramNotifier("test_token", "123456")

    notifier.send_trade_notification(
        ticker="AAPL",
        side="BUY",
        qty=66,
        price=150.0,
        reason="MA crossover"
    )

    mock_httpx_success.assert_called_once()
    call_args = mock_httpx_success.call_args
    message = call_args[1]['json']['text']

    assert "BUY" in message
    assert "AAPL" in message
    assert "66" in message
    assert "150.00" in message
    assert "MA crossover" in message
    assert "🟢" in message  # Buy emoji


def test_send_position_closed_notification(mock_httpx_success):
    """Test position closed notification."""
    notifier = TelegramNotifier("test_token", "123456")

    notifier.send_position_closed_notification(
        ticker="AAPL",
        qty=66,
        entry_price=150.0,
        exit_price=157.5,
        pnl=495.0,
        pnl_pct=5.0,
        reason="Take profit hit"
    )

    mock_httpx_success.assert_called_once()
    call_args = mock_httpx_success.call_args
    message = call_args[1]['json']['text']

    assert "Position Closed" in message
    assert "AAPL" in message
    assert "150.00" in message  # entry
    assert "157.50" in message  # exit
    assert "495.00" in message  # pnl
    assert "+5.00%" in message  # pnl_pct
    assert "Take profit hit" in message
    assert "💰" in message  # Profit emoji


def test_send_error_alert(mock_httpx_success):
    """Test error alert."""
    notifier = TelegramNotifier("test_token", "123456")

    notifier.send_error_alert("Connection timeout")

    mock_httpx_success.assert_called_once()
    call_args = mock_httpx_success.call_args
    message = call_args[1]['json']['text']

    assert "AlphaLive Error" in message
    assert "Connection timeout" in message
    assert "⚠️" in message


def test_send_alert(mock_httpx_success):
    """Test generic alert."""
    notifier = TelegramNotifier("test_token", "123456")

    notifier.send_alert("High slippage detected")

    mock_httpx_success.assert_called_once()
    call_args = mock_httpx_success.call_args
    message = call_args[1]['json']['text']

    assert "Alert" in message
    assert "High slippage detected" in message
    assert "🔔" in message


def test_send_daily_summary(mock_httpx_success):
    """Test daily summary."""
    notifier = TelegramNotifier("test_token", "123456")

    stats = {
        "trades": 5,
        "pnl": 450.0,
        "win_rate": 60.0,
        "start_equity": 100000.0,
        "end_equity": 100450.0
    }

    notifier.send_daily_summary(stats)

    mock_httpx_success.assert_called_once()
    call_args = mock_httpx_success.call_args
    message = call_args[1]['json']['text']

    assert "Daily Summary" in message
    assert "5" in message  # trades
    assert "450.00" in message  # pnl
    assert "60.0%" in message  # win rate
    assert "100000.00" in message  # start equity
    assert "100450.00" in message  # end equity
    assert "📈" in message  # Positive pnl emoji


def test_is_offline():
    """Test is_offline method."""
    notifier = TelegramNotifier("test_token", "123456")

    assert notifier.is_offline() is False

    notifier.telegram_offline = True
    assert notifier.is_offline() is True


def test_exponential_backoff_timing():
    """Test exponential backoff timing (1s, 2s, 4s)."""
    notifier = TelegramNotifier("test_token", "123456")

    with patch('httpx.post') as mock_post:
        mock_response = Mock()
        mock_response.status_code = 500  # Server error
        mock_post.return_value = mock_response

        with patch('time.sleep') as mock_sleep:
            notifier.send_message("Test")

            # Should have slept twice (between 3 attempts)
            assert mock_sleep.call_count == 2
            # First sleep: 1s, second sleep: 2s
            assert mock_sleep.call_args_list[0][0][0] == 1
            assert mock_sleep.call_args_list[1][0][0] == 2


def test_http_timeout():
    """Test HTTP request timeout."""
    notifier = TelegramNotifier("test_token", "123456")

    with patch('httpx.post') as mock_post:
        notifier.send_message("Test")

        # Verify timeout is set to 10 seconds
        call_args = mock_post.call_args
        assert call_args[1]['timeout'] == 10.0


def test_parse_mode():
    """Test parse_mode parameter."""
    notifier = TelegramNotifier("test_token", "123456")

    with patch('httpx.post') as mock_post:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        # Test HTML (default)
        notifier.send_message("Test", parse_mode="HTML")
        assert mock_post.call_args[1]['json']['parse_mode'] == "HTML"

        # Test Markdown
        notifier.send_message("Test", parse_mode="Markdown")
        assert mock_post.call_args[1]['json']['parse_mode'] == "Markdown"
