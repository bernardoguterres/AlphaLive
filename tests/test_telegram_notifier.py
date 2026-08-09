"""
Test Telegram Notifier (B7)

Tests for TelegramNotifier with retry logic, graceful degradation,
and background retry functionality.

Audit bug 2.8 (2026-07-14): send_message() used to make the actual blocking
HTTP call (up to ~37s worst case: 3 retries x 10s timeout + 1s/2s/4s
backoff) directly on the caller's thread, and was called inline from the
main trading loop - a slow/down Telegram measurably blocked trading.
send_message() is now a thin, fast queue-and-return wrapper; the real
HTTP call/retry/backoff/graceful-degradation logic moved to _send_now(),
which only ever runs on a dedicated background worker thread. Tests for
that retry/backoff/degradation logic now call _send_now() directly (it's
the actual unit doing blocking work); tests for the convenience methods
(send_shutdown_notification etc.) call notifier._queue.join() after
invoking them to deterministically wait for the background worker to
finish before asserting on the httpx mock - they go through the real
public send_message() -> queue -> worker path, same as production.
"""

import time
import pytest
from unittest.mock import Mock, patch

from alphalive.notifications.telegram_bot import TelegramNotifier


@pytest.fixture
def mock_httpx_success():
    """Mock successful httpx response."""
    with patch("httpx.post") as mock_post:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = '{"ok": true}'
        mock_post.return_value = mock_response
        yield mock_post


@pytest.fixture
def mock_httpx_failure():
    """Mock failed httpx response."""
    with patch("httpx.post") as mock_post:
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.text = '{"ok": false, "description": "Bad Request"}'
        mock_post.return_value = mock_response
        yield mock_post


@pytest.fixture
def mock_httpx_exception():
    """Mock httpx exception."""
    with patch("httpx.post") as mock_post:
        mock_post.side_effect = Exception("Connection timeout")
        yield mock_post


def test_telegram_notifier_initialization():
    """Test TelegramNotifier initialization."""
    notifier = TelegramNotifier(bot_token="test_token", chat_id="123456", enabled=True)

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


# ---------------------------------------------------------------------------
# _send_now() -- the actual blocking HTTP call, retry/backoff, and graceful
# degradation logic. Only ever invoked from the background worker thread in
# production, but tested directly here (synchronously, on the test thread)
# since that's the real unit of blocking behavior.
# ---------------------------------------------------------------------------


def test_send_now_success(mock_httpx_success):
    notifier = TelegramNotifier("test_token", "123456")

    result = notifier._send_now("Test message")

    assert result is True
    assert notifier.consecutive_failures == 0
    assert notifier.telegram_offline is False

    mock_httpx_success.assert_called_once()
    call_args = mock_httpx_success.call_args
    assert call_args[1]["json"]["chat_id"] == "123456"
    assert call_args[1]["json"]["text"] == "Test message"
    assert call_args[1]["json"]["parse_mode"] == "HTML"


def test_send_now_with_retry(mock_httpx_failure):
    notifier = TelegramNotifier("test_token", "123456")

    with patch("time.sleep"):
        result = notifier._send_now("Test message")

    assert result is False
    assert notifier.consecutive_failures == 1
    assert mock_httpx_failure.call_count == 3


def test_send_now_graceful_degradation():
    notifier = TelegramNotifier("test_token", "123456")

    with patch("httpx.post") as mock_post, patch("time.sleep"):
        mock_post.side_effect = Exception("Connection error")

        result1 = notifier._send_now("Test 1")
        assert result1 is False
        assert notifier.consecutive_failures == 1
        assert notifier.telegram_offline is False

        result2 = notifier._send_now("Test 2")
        assert result2 is False
        assert notifier.consecutive_failures == 2
        assert notifier.telegram_offline is False

        result3 = notifier._send_now("Test 3")
        assert result3 is False
        assert notifier.consecutive_failures == 3
        assert notifier.telegram_offline is True


def test_send_now_background_retry():
    """Test background retry after 10 minutes."""
    notifier = TelegramNotifier("test_token", "123456")

    notifier.telegram_offline = True
    notifier.consecutive_failures = 3
    notifier.last_retry_attempt = time.time() - 700  # 11+ minutes ago

    with patch("httpx.post") as mock_post:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        result = notifier._send_now("Test")

        assert result is True
        assert notifier.telegram_offline is False
        assert notifier.consecutive_failures == 0


def test_send_now_skip_during_offline():
    """Test message skip when offline and retry not due."""
    notifier = TelegramNotifier("test_token", "123456")

    notifier.telegram_offline = True
    notifier.consecutive_failures = 3
    notifier.last_retry_attempt = time.time()  # Just now

    with patch("httpx.post") as mock_post:
        result = notifier._send_now("Test")

        assert result is False
        mock_post.assert_not_called()


def test_send_now_restore_after_offline():
    notifier = TelegramNotifier("test_token", "123456")

    notifier.telegram_offline = True
    notifier.consecutive_failures = 3

    with patch("httpx.post") as mock_post:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        notifier.last_retry_attempt = time.time() - 700

        result = notifier._send_now("Test")

        assert result is True
        assert notifier.telegram_offline is False
        assert notifier.consecutive_failures == 0


def test_exponential_backoff_timing():
    """Test exponential backoff timing (1s, 2s, 4s)."""
    notifier = TelegramNotifier("test_token", "123456")

    with patch("httpx.post") as mock_post:
        mock_response = Mock()
        mock_response.status_code = 500  # Server error
        mock_post.return_value = mock_response

        with patch("time.sleep") as mock_sleep:
            notifier._send_now("Test")

            assert mock_sleep.call_count == 2
            assert mock_sleep.call_args_list[0][0][0] == 1
            assert mock_sleep.call_args_list[1][0][0] == 2


def test_http_timeout():
    """Test HTTP request timeout."""
    notifier = TelegramNotifier("test_token", "123456")

    with patch("httpx.post") as mock_post:
        notifier._send_now("Test")

        call_args = mock_post.call_args
        assert call_args[1]["timeout"] == 10.0


def test_parse_mode():
    """Test parse_mode parameter."""
    notifier = TelegramNotifier("test_token", "123456")

    with patch("httpx.post") as mock_post:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        notifier._send_now("Test", parse_mode="HTML")
        assert mock_post.call_args[1]["json"]["parse_mode"] == "HTML"

        notifier._send_now("Test", parse_mode="Markdown")
        assert mock_post.call_args[1]["json"]["parse_mode"] == "Markdown"


def test_is_offline():
    """Test is_offline method."""
    notifier = TelegramNotifier("test_token", "123456")

    assert notifier.is_offline() is False

    notifier.telegram_offline = True
    assert notifier.is_offline() is True


# ---------------------------------------------------------------------------
# send_message() -- the public, non-blocking API. Goes through the real
# queue -> background worker path; tests call notifier._queue.join() to
# deterministically wait for the worker before asserting on httpx.
# ---------------------------------------------------------------------------


def test_send_message_queues_and_returns_immediately_without_waiting_for_http(
    mock_httpx_success,
):
    """Core bug 2.8 regression: send_message() must return before the
    actual HTTP call even happens, not after it completes."""
    notifier = TelegramNotifier("test_token", "123456")

    def _slow_post(*args, **kwargs):
        time.sleep(2.0)
        response = Mock()
        response.status_code = 200
        return response

    mock_httpx_success.side_effect = _slow_post

    start = time.monotonic()
    result = notifier.send_message("Test message")
    elapsed = time.monotonic() - start

    assert result is True
    assert elapsed < 0.5, (
        f"send_message() took {elapsed:.2f}s - it must return almost "
        f"immediately, not wait for the (slow) HTTP call"
    )

    notifier._queue.join()  # let the background worker actually send it
    mock_httpx_success.assert_called_once()


def test_send_message_delivers_via_background_worker(mock_httpx_success):
    notifier = TelegramNotifier("test_token", "123456")

    notifier.send_message("Test message")
    notifier._queue.join()

    mock_httpx_success.assert_called_once()
    call_args = mock_httpx_success.call_args
    assert call_args[1]["json"]["text"] == "Test message"


def test_send_message_queue_full_drops_message_without_blocking():
    notifier = TelegramNotifier("test_token", "123456")
    # Fill the queue without a worker draining it, by feeding it directly.
    for _ in range(notifier._queue.maxsize):
        notifier._queue.put_nowait(("filler", "HTML"))

    result = notifier.send_message("One too many")

    assert result is False


def test_send_message_disabled_does_not_enqueue():
    notifier = TelegramNotifier(bot_token=None, chat_id=None)

    result = notifier.send_message("Test")

    assert result is False
    assert notifier._queue.qsize() == 0


# ---------------------------------------------------------------------------
# Convenience methods -- all funnel through the public send_message(), so
# each test waits on notifier._queue.join() before asserting on the httpx
# mock.
# ---------------------------------------------------------------------------


def test_send_shutdown_notification(mock_httpx_success):
    """Test shutdown notification."""
    notifier = TelegramNotifier("test_token", "123456")

    stats = {"trades": 5, "pnl": 450.0, "win_rate": 60.0}

    notifier.send_shutdown_notification(stats)
    notifier._queue.join()

    mock_httpx_success.assert_called_once()
    call_args = mock_httpx_success.call_args
    message = call_args[1]["json"]["text"]

    assert "AlphaLive Stopped" in message
    assert "5" in message  # trades
    assert "450.00" in message  # pnl


def test_send_trade_notification(mock_httpx_success):
    """Test trade notification."""
    notifier = TelegramNotifier("test_token", "123456")

    notifier.send_trade_notification(
        ticker="AAPL", side="BUY", qty=66, price=150.0, reason="MA crossover"
    )
    notifier._queue.join()

    mock_httpx_success.assert_called_once()
    call_args = mock_httpx_success.call_args
    message = call_args[1]["json"]["text"]

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
        reason="Take profit hit",
    )
    notifier._queue.join()

    mock_httpx_success.assert_called_once()
    call_args = mock_httpx_success.call_args
    message = call_args[1]["json"]["text"]

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
    notifier._queue.join()

    mock_httpx_success.assert_called_once()
    call_args = mock_httpx_success.call_args
    message = call_args[1]["json"]["text"]

    assert "AlphaLive Error" in message
    assert "Connection timeout" in message
    assert "⚠️" in message


def test_send_alert(mock_httpx_success):
    """Test generic alert."""
    notifier = TelegramNotifier("test_token", "123456")

    notifier.send_alert("High slippage detected")
    notifier._queue.join()

    mock_httpx_success.assert_called_once()
    call_args = mock_httpx_success.call_args
    message = call_args[1]["json"]["text"]

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
        "end_equity": 100450.0,
    }

    notifier.send_daily_summary(stats)
    notifier._queue.join()

    mock_httpx_success.assert_called_once()
    call_args = mock_httpx_success.call_args
    message = call_args[1]["json"]["text"]

    assert "Daily Summary" in message
    assert "5" in message  # trades
    assert "450.00" in message  # pnl
    assert "60.0%" in message  # win rate
    assert "100000.00" in message  # start equity
    assert "100450.00" in message  # end equity
    assert "📈" in message  # Positive pnl emoji
