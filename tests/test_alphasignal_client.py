"""
Tests for AlphaSignal sentiment client and execution gate helper.

All async tests run automatically under pytest-asyncio (asyncio_mode = auto).

Coverage:
  1. test_positive_sentiment_allows_long
  2. test_negative_sentiment_blocks_long
  3. test_negative_sentiment_allows_short
  4. test_positive_sentiment_blocks_short
  5. test_neutral_direction_always_allowed
  6. test_timeout_fails_open
  7. test_both_filters_run_concurrently
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alphalive.services.alphasignal_client import (
    AlphaSignalClient,
    run_pre_execution_checks,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TICKER = "AAPL"
DEFAULT_THRESHOLD = -0.3  # AlphaSignalClient default


def _make_client(threshold: float = DEFAULT_THRESHOLD) -> AlphaSignalClient:
    """Return a client with a mocked get_sentiment."""
    return AlphaSignalClient(sentiment_threshold=threshold)


def _sentiment_dict(score: float, confidence: float = 0.8) -> dict:
    return {
        "sentiment_score": score,
        "confidence": confidence,
        "sources": ["AAPL_10-K_2024"],
        "latency_ms": 42.0,
    }


# ---------------------------------------------------------------------------
# Test 1 - positive sentiment allows long
# ---------------------------------------------------------------------------


async def test_positive_sentiment_allows_long():
    """Score=0.5 (above -0.3 threshold) should allow a long (BUY=2)."""
    client = _make_client()

    with patch.object(
        client, "get_sentiment", new=AsyncMock(return_value=_sentiment_dict(0.5))
    ):
        allowed, sentiment = await client.is_execution_allowed(
            TICKER, intended_direction=2
        )

    assert allowed is True
    assert sentiment["sentiment_score"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Test 2 - negative sentiment blocks long
# ---------------------------------------------------------------------------


async def test_negative_sentiment_blocks_long():
    """Score=-0.5 (below -0.3 threshold) should block a long (BUY=2)."""
    client = _make_client()

    with patch.object(
        client, "get_sentiment", new=AsyncMock(return_value=_sentiment_dict(-0.5))
    ):
        allowed, sentiment = await client.is_execution_allowed(
            TICKER, intended_direction=2
        )

    assert allowed is False
    assert sentiment["sentiment_score"] == pytest.approx(-0.5)


# ---------------------------------------------------------------------------
# Test 3 - negative sentiment allows short
# ---------------------------------------------------------------------------


async def test_negative_sentiment_allows_short():
    """Score=-0.5 (below +0.3 inverse threshold) should allow a short (SELL=0).

    Strong negative sentiment CONFIRMS a short - do not block.
    """
    client = _make_client()

    with patch.object(
        client, "get_sentiment", new=AsyncMock(return_value=_sentiment_dict(-0.5))
    ):
        allowed, sentiment = await client.is_execution_allowed(
            TICKER, intended_direction=0
        )

    assert allowed is True
    assert sentiment["sentiment_score"] == pytest.approx(-0.5)


# ---------------------------------------------------------------------------
# Test 4 - positive sentiment blocks short
# ---------------------------------------------------------------------------


async def test_positive_sentiment_blocks_short():
    """Score=0.5 (above +0.3 inverse threshold) should block a short (SELL=0).

    Strong positive sentiment contradicts a short - execution suppressed.
    """
    client = _make_client()

    with patch.object(
        client, "get_sentiment", new=AsyncMock(return_value=_sentiment_dict(0.5))
    ):
        allowed, sentiment = await client.is_execution_allowed(
            TICKER, intended_direction=0
        )

    assert allowed is False
    assert sentiment["sentiment_score"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Test 5 - neutral direction always allowed
# ---------------------------------------------------------------------------


async def test_neutral_direction_always_allowed():
    """Direction=1 (HOLD/neutral) must always return True without calling get_sentiment."""
    client = _make_client()
    mock_get = AsyncMock(return_value=_sentiment_dict(-0.99))  # Worst possible score

    with patch.object(client, "get_sentiment", new=mock_get):
        allowed, sentiment = await client.is_execution_allowed(
            TICKER, intended_direction=1
        )

    assert allowed is True
    assert sentiment == {}  # No sentiment data fetched for neutral
    mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6 - timeout fails open
# ---------------------------------------------------------------------------


async def test_timeout_fails_open():
    """asyncio.TimeoutError from get_sentiment must return (True, {}) - fail open."""
    client = _make_client()

    with patch.object(
        client, "get_sentiment", new=AsyncMock(side_effect=asyncio.TimeoutError)
    ):
        allowed, sentiment = await client.is_execution_allowed(
            TICKER, intended_direction=2
        )

    assert allowed is True
    assert sentiment == {}


# ---------------------------------------------------------------------------
# Test 7 - the gate delegates to the sentiment filter and unpacks its result
# ---------------------------------------------------------------------------


async def test_gate_returns_sentiment_filter_result():
    """run_pre_execution_checks must await the sentiment filter and return
    its (allowed, prediction) result unchanged."""
    mock_alphasignal = MagicMock(spec=AlphaSignalClient)

    async def _allowed(*args, **kwargs):
        return False, {"sentiment_score": -0.9}

    mock_alphasignal.is_execution_allowed = MagicMock(
        side_effect=lambda *a, **k: _allowed()
    )

    allowed, pred = await run_pre_execution_checks(
        alphasignal_client=mock_alphasignal,
        ticker=TICKER,
        signal_direction=2,
    )

    assert allowed is False
    assert pred == {"sentiment_score": -0.9}
    mock_alphasignal.is_execution_allowed.assert_called_once_with(TICKER, 2)
