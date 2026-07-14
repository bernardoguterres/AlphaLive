"""Tests for the shared retry-with-backoff helper."""

from unittest.mock import MagicMock, patch

import pytest

from alphalive.utils.retry import RetryDecision, RetryOutcome, retry_with_backoff


def test_retry_with_backoff_returns_immediately_on_success():
    func = MagicMock(return_value="ok")

    result = retry_with_backoff(func, classify=lambda e: RetryOutcome(RetryDecision.RETRY))

    assert result == "ok"
    assert func.call_count == 1


def test_retry_with_backoff_retries_then_succeeds():
    func = MagicMock(side_effect=[ValueError("transient"), "ok"])

    with patch("alphalive.utils.retry.time.sleep") as mock_sleep:
        result = retry_with_backoff(
            func, classify=lambda e: RetryOutcome(RetryDecision.RETRY), max_retries=3
        )

    assert result == "ok"
    assert func.call_count == 2
    mock_sleep.assert_called_once()


def test_retry_with_backoff_raises_after_exhausting_retries():
    func = MagicMock(side_effect=ValueError("permanent"))

    with patch("alphalive.utils.retry.time.sleep"):
        with pytest.raises(ValueError, match="permanent"):
            retry_with_backoff(
                func, classify=lambda e: RetryOutcome(RetryDecision.RETRY), max_retries=3
            )

    assert func.call_count == 3


def test_retry_with_backoff_raises_immediately_on_fatal_classification():
    func = MagicMock(side_effect=[ValueError("fatal"), "ok"])

    with patch("alphalive.utils.retry.time.sleep") as mock_sleep:
        with pytest.raises(ValueError, match="fatal"):
            retry_with_backoff(
                func, classify=lambda e: RetryOutcome(RetryDecision.FATAL), max_retries=3
            )

    # Must not have retried or slept - fatal errors bail out on the first attempt.
    assert func.call_count == 1
    mock_sleep.assert_not_called()


def test_retry_with_backoff_delay_doubles_each_attempt():
    func = MagicMock(side_effect=[ValueError("e1"), ValueError("e2"), "ok"])

    with patch("alphalive.utils.retry.time.sleep") as mock_sleep:
        retry_with_backoff(
            func,
            classify=lambda e: RetryOutcome(RetryDecision.RETRY),
            max_retries=3,
            base_delay=1.0,
            multiplier=2.0,
        )

    assert [call.args[0] for call in mock_sleep.call_args_list] == [1.0, 2.0]


def test_retry_with_backoff_honors_delay_override():
    func = MagicMock(side_effect=[ValueError("rate limited"), "ok"])

    with patch("alphalive.utils.retry.time.sleep") as mock_sleep:
        retry_with_backoff(
            func,
            classify=lambda e: RetryOutcome(RetryDecision.RETRY, delay_override=7.5),
            max_retries=3,
            base_delay=1.0,
        )

    mock_sleep.assert_called_once_with(7.5)
