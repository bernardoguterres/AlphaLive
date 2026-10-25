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
    GATE_STATUS_AVAILABLE_BLOCK,
    GATE_STATUS_AVAILABLE_PASS,
    GATE_STATUS_ERROR_BYPASS,
    GATE_STATUS_NEUTRAL_BYPASS,
    GATE_STATUS_NO_DATA_BYPASS,
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


def _sentiment_dict(
    score: float, confidence: float = 0.8, data_available: bool = True
) -> dict:
    return {
        "sentiment_score": score,
        "confidence": confidence,
        "sources": ["AAPL_10-K_2024"],
        "latency_ms": 42.0,
        "data_available": data_available,
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
    assert sentiment["gate_status"] == GATE_STATUS_AVAILABLE_PASS


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
    assert sentiment["gate_status"] == GATE_STATUS_AVAILABLE_BLOCK


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
    # No sentiment data fetched for neutral - only the gate_status marker.
    assert sentiment == {"gate_status": GATE_STATUS_NEUTRAL_BYPASS}
    mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6 - timeout fails open
# ---------------------------------------------------------------------------


async def test_timeout_fails_open():
    """asyncio.TimeoutError from get_sentiment must fail open (allowed=True)
    and be tagged error_bypass, distinguishable from a real neutral/no-data
    result (audit remediation item 3)."""
    client = _make_client()

    with patch.object(
        client, "get_sentiment", new=AsyncMock(side_effect=asyncio.TimeoutError)
    ):
        allowed, sentiment = await client.is_execution_allowed(
            TICKER, intended_direction=2
        )

    assert allowed is True
    assert sentiment == {"gate_status": GATE_STATUS_ERROR_BYPASS, "fail_policy": "open"}


# ---------------------------------------------------------------------------
# Test 8 - no-data still fails open, but is distinguishable from a real
# neutral sentiment reading (FINAL_ENGINEERING_AUDIT.md remediation item 3)
# ---------------------------------------------------------------------------


async def test_no_data_available_fails_open_and_is_tagged():
    """data_available=False (AlphaSignal has zero ingested chunks for this
    ticker/period) must still allow execution (unchanged fail-open policy)
    but must be tagged no_data_bypass, not available_pass - previously this
    was indistinguishable from a genuine neutral score of 0.0."""
    client = _make_client()
    no_data = _sentiment_dict(0.0, confidence=0.0, data_available=False)

    with patch.object(client, "get_sentiment", new=AsyncMock(return_value=no_data)):
        allowed, sentiment = await client.is_execution_allowed(
            TICKER, intended_direction=2
        )

    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_NO_DATA_BYPASS
    assert sentiment["data_available"] is False


async def test_genuine_neutral_sentiment_is_available_pass_not_no_data():
    """A real, ingested neutral score (data_available=True, score=0.0) must
    be tagged available_pass, not conflated with the no-data case above -
    this is the exact distinction the audit flagged as missing."""
    client = _make_client()
    genuine_neutral = _sentiment_dict(0.0, confidence=0.6, data_available=True)

    with patch.object(
        client, "get_sentiment", new=AsyncMock(return_value=genuine_neutral)
    ):
        allowed, sentiment = await client.is_execution_allowed(
            TICKER, intended_direction=2
        )

    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_AVAILABLE_PASS
    assert sentiment["data_available"] is True


async def test_no_data_bypass_overrides_would_be_block():
    """Even a score that would otherwise block (e.g. leftover/stale -0.9
    from a prior ticker) must fail open once data_available is False - the
    no-data path is checked before threshold comparison, deterministically,
    not left to fall out of the threshold math."""
    client = _make_client()
    no_data_but_extreme_score = _sentiment_dict(
        -0.9, confidence=0.0, data_available=False
    )

    with patch.object(
        client,
        "get_sentiment",
        new=AsyncMock(return_value=no_data_but_extreme_score),
    ):
        allowed, sentiment = await client.is_execution_allowed(
            TICKER, intended_direction=2
        )

    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_NO_DATA_BYPASS


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


# ---------------------------------------------------------------------------
# 2026-10: stale evidence + explicit failure policy
# ---------------------------------------------------------------------------

import asyncio as _asyncio  # noqa: E402

from alphalive.services.alphasignal_client import (  # noqa: E402
    GATE_STATUS_ERROR_BYPASS as _ERR,
    GATE_STATUS_STALE_BYPASS as _STALE,
    AlphaSignalClient as _Client,
    _detect_contract_issue,
    run_pre_execution_checks as _gate,
)


def _client_returning(payload, policy="open"):
    c = _Client(base_url="http://127.0.0.1:9", fail_policy=policy)

    async def fake(ticker):
        return payload

    c.get_sentiment = fake
    return c


_STALE_PAYLOAD = {
    "sentiment_score": 0.0,
    "latest_score": None,
    "data_available": True,
    "status": "stale",
    "degraded": False,
    "contract_issue": None,
}


def test_stale_evidence_is_not_evaluated_and_follows_open_policy():
    ok, pred = _asyncio.run(
        _client_returning(_STALE_PAYLOAD).is_execution_allowed("AAPL", 2)
    )
    assert (
        ok is True and pred["gate_status"] == _STALE and pred["fail_policy"] == "open"
    )


def test_closed_policy_blocks_entries_when_sentiment_cannot_be_evaluated():
    ok, pred = _asyncio.run(
        _client_returning(_STALE_PAYLOAD, "closed").is_execution_allowed("AAPL", 2)
    )
    assert ok is False and pred["gate_status"] == _STALE

    class Boom(_Client):
        async def is_execution_allowed(self, *a):
            raise RuntimeError("down")

    ok, pred = _asyncio.run(_gate(Boom(fail_policy="closed"), "AAPL", 2))
    assert ok is False and pred["gate_status"] == _ERR


def test_evaluated_scores_are_unaffected_by_fail_policy():
    payload = {
        "sentiment_score": 0.5,
        "latest_score": 0.5,
        "data_available": True,
        "status": "ok",
        "degraded": False,
        "contract_issue": None,
    }
    ok, pred = _asyncio.run(
        _client_returning(payload, "closed").is_execution_allowed("AAPL", 2)
    )
    assert ok is True and pred["gate_status"] == "available_pass"


def test_stale_with_a_score_is_a_contract_violation():
    assert _detect_contract_issue({"status": "stale"}, 0.3) is not None
    assert _detect_contract_issue({"status": "stale"}, None) is None


def test_invalid_fail_policy_is_rejected():
    with pytest.raises(ValueError):
        _Client(fail_policy="maybe")


# ---------------------------------------------------------------------------
# Threshold boundary - the comparison is strict: BUY blocked iff score < threshold
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "score, expected",
    [(-0.3, True), (-0.3001, False), (-1.0, False), (1.0, True)],
)
async def test_long_is_blocked_only_strictly_below_threshold(score, expected):
    client = _make_client()
    with patch.object(
        client, "get_sentiment", new=AsyncMock(return_value=_sentiment_dict(score))
    ):
        allowed, sentiment = await client.is_execution_allowed(
            TICKER, intended_direction=2
        )
    assert allowed is expected
    assert sentiment["gate_status"] == (
        GATE_STATUS_AVAILABLE_PASS if expected else GATE_STATUS_AVAILABLE_BLOCK
    )
