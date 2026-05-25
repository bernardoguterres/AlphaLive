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

from alphalive.services.alphasignal_client import AlphaSignalClient, run_pre_execution_checks

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
# Test 1 — positive sentiment allows long
# ---------------------------------------------------------------------------


async def test_positive_sentiment_allows_long():
    """Score=0.5 (above -0.3 threshold) should allow a long (BUY=2)."""
    client = _make_client()

    with patch.object(client, "get_sentiment", new=AsyncMock(return_value=_sentiment_dict(0.5))):
        allowed, sentiment = await client.is_execution_allowed(TICKER, intended_direction=2)

    assert allowed is True
    assert sentiment["sentiment_score"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Test 2 — negative sentiment blocks long
# ---------------------------------------------------------------------------


async def test_negative_sentiment_blocks_long():
    """Score=-0.5 (below -0.3 threshold) should block a long (BUY=2)."""
    client = _make_client()

    with patch.object(client, "get_sentiment", new=AsyncMock(return_value=_sentiment_dict(-0.5))):
        allowed, sentiment = await client.is_execution_allowed(TICKER, intended_direction=2)

    assert allowed is False
    assert sentiment["sentiment_score"] == pytest.approx(-0.5)


# ---------------------------------------------------------------------------
# Test 3 — negative sentiment allows short
# ---------------------------------------------------------------------------


async def test_negative_sentiment_allows_short():
    """Score=-0.5 (below +0.3 inverse threshold) should allow a short (SELL=0).

    Strong negative sentiment CONFIRMS a short — do not block.
    """
    client = _make_client()

    with patch.object(client, "get_sentiment", new=AsyncMock(return_value=_sentiment_dict(-0.5))):
        allowed, sentiment = await client.is_execution_allowed(TICKER, intended_direction=0)

    assert allowed is True
    assert sentiment["sentiment_score"] == pytest.approx(-0.5)


# ---------------------------------------------------------------------------
# Test 4 — positive sentiment blocks short
# ---------------------------------------------------------------------------


async def test_positive_sentiment_blocks_short():
    """Score=0.5 (above +0.3 inverse threshold) should block a short (SELL=0).

    Strong positive sentiment contradicts a short — execution suppressed.
    """
    client = _make_client()

    with patch.object(client, "get_sentiment", new=AsyncMock(return_value=_sentiment_dict(0.5))):
        allowed, sentiment = await client.is_execution_allowed(TICKER, intended_direction=0)

    assert allowed is False
    assert sentiment["sentiment_score"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Test 5 — neutral direction always allowed
# ---------------------------------------------------------------------------


async def test_neutral_direction_always_allowed():
    """Direction=1 (HOLD/neutral) must always return True without calling get_sentiment."""
    client = _make_client()
    mock_get = AsyncMock(return_value=_sentiment_dict(-0.99))  # Worst possible score

    with patch.object(client, "get_sentiment", new=mock_get):
        allowed, sentiment = await client.is_execution_allowed(TICKER, intended_direction=1)

    assert allowed is True
    assert sentiment == {}  # No sentiment data fetched for neutral
    mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6 — timeout fails open
# ---------------------------------------------------------------------------


async def test_timeout_fails_open():
    """asyncio.TimeoutError from get_sentiment must return (True, {}) — fail open."""
    client = _make_client()

    with patch.object(
        client, "get_sentiment", new=AsyncMock(side_effect=asyncio.TimeoutError)
    ):
        allowed, sentiment = await client.is_execution_allowed(TICKER, intended_direction=2)

    assert allowed is True
    assert sentiment == {}


# ---------------------------------------------------------------------------
# Test 7 — both filters run concurrently via asyncio.gather
# ---------------------------------------------------------------------------


async def test_both_filters_run_concurrently():
    """asyncio.gather must be called with two coroutines — one per filter.

    This verifies the concurrent execution gate: both DeepLOB and
    AlphaSignal checks are submitted to the event loop together, not
    awaited sequentially.
    """
    # Mock deeplob client with an is_execution_allowed that returns a coroutine.
    mock_deeplob = MagicMock()
    deeplob_coro_sentinel = object()  # Arbitrary sentinel — not a real coroutine
    mock_deeplob.is_execution_allowed = MagicMock(return_value=deeplob_coro_sentinel)

    # Mock alphasignal client similarly.
    mock_alphasignal = MagicMock(spec=AlphaSignalClient)
    mock_alphasignal.sentiment_threshold = DEFAULT_THRESHOLD
    sentiment_coro_sentinel = object()
    mock_alphasignal.is_execution_allowed = MagicMock(return_value=sentiment_coro_sentinel)

    # Replace asyncio.gather in the client module with a spy that:
    #   1. Records the positional args (the two coroutines).
    #   2. Returns a pre-cooked result so the rest of run_pre_execution_checks
    #      can unpack without needing real awaitables.
    gathered_args: list = []

    async def spy_gather(*coros, **kwargs):  # noqa: ANN002, ANN003
        gathered_args.extend(coros)
        return [(True, {}), (True, {})]

    with patch(
        "alphalive.services.alphasignal_client.asyncio.gather",
        new=spy_gather,
    ):
        await run_pre_execution_checks(
            deeplob_client=mock_deeplob,
            alphasignal_client=mock_alphasignal,
            lob_snapshot=None,
            ticker=TICKER,
            signal_direction=2,
        )

    # asyncio.gather must have been called with exactly two coroutines.
    assert len(gathered_args) == 2, (
        f"Expected gather to be called with 2 coroutines, got {len(gathered_args)}"
    )
    # The first arg comes from deeplob, the second from alphasignal.
    assert gathered_args[0] is deeplob_coro_sentinel
    assert gathered_args[1] is sentiment_coro_sentinel
