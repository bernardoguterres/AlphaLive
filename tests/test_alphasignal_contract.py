"""
Tests for AlphaSignal's finalized degradation contract (objective 5 of the
2026-09-11 hardening pass): status/degraded/degradation_reason/
reliable_chunk_count/total_chunk_count on GET /sentiment/{ticker}, and the
defensive validation of impossible field combinations.

All HTTP calls are mocked (httpx.AsyncClient patched) - no network, no real
AlphaSignal service.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from alphalive.services.alphasignal_client import (
    GATE_STATUS_AVAILABLE_BLOCK,
    GATE_STATUS_AVAILABLE_PASS,
    GATE_STATUS_ERROR_BYPASS,
    GATE_STATUS_FULL_DEGRADED_BYPASS,
    GATE_STATUS_INVALID_RESPONSE_BYPASS,
    GATE_STATUS_NO_DATA_BYPASS,
    GATE_STATUS_PARTIAL_DEGRADED_BLOCK,
    GATE_STATUS_PARTIAL_DEGRADED_BYPASS,
    GATE_STATUS_PARTIAL_DEGRADED_PASS,
    AlphaSignalClient,
    run_pre_execution_checks,
)

TICKER = "AAPL"


def _mock_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


def _mock_async_client(response=None, raise_exc=None):
    client = AsyncMock()
    if raise_exc is not None:
        client.get = AsyncMock(side_effect=raise_exc)
    else:
        client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


async def _allowed(client, data, direction=2, status_code=200):
    response = _mock_response(data, status_code=status_code)
    mock_client = _mock_async_client(response)
    with patch(
        "alphalive.services.alphasignal_client.httpx.AsyncClient",
        return_value=mock_client,
    ):
        return await client.is_execution_allowed(TICKER, direction)


# ---------------------------------------------------------------------------
# Genuine positive / negative / neutral (status="ok", degraded=false)
# ---------------------------------------------------------------------------


async def test_genuine_positive_allows_long():
    client = AlphaSignalClient(sentiment_threshold=-0.3)
    allowed, sentiment = await _allowed(
        client, {"status": "ok", "degraded": False, "latest_score": 0.6, "signals": []}
    )
    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_AVAILABLE_PASS


async def test_genuine_negative_blocks_long():
    client = AlphaSignalClient(sentiment_threshold=-0.3)
    allowed, sentiment = await _allowed(
        client, {"status": "ok", "degraded": False, "latest_score": -0.9, "signals": []}
    )
    assert allowed is False
    assert sentiment["gate_status"] == GATE_STATUS_AVAILABLE_BLOCK


async def test_genuine_neutral_is_available_pass():
    client = AlphaSignalClient(sentiment_threshold=-0.3)
    allowed, sentiment = await _allowed(
        client, {"status": "ok", "degraded": False, "latest_score": 0.0, "signals": []}
    )
    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_AVAILABLE_PASS
    assert sentiment["sentiment_score"] == 0.0


# ---------------------------------------------------------------------------
# No data
# ---------------------------------------------------------------------------


async def test_status_no_data_fails_open():
    client = AlphaSignalClient()
    allowed, sentiment = await _allowed(
        client,
        {
            "status": "no_data",
            "degraded": False,
            "data_available": False,
            "latest_score": None,
        },
    )
    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_NO_DATA_BYPASS


async def test_data_available_false_without_status_field_fails_open():
    client = AlphaSignalClient()
    allowed, sentiment = await _allowed(
        client, {"data_available": False, "latest_score": None}
    )
    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_NO_DATA_BYPASS


# ---------------------------------------------------------------------------
# Partial / full degradation
# ---------------------------------------------------------------------------


async def test_full_degradation_fails_open_distinctly():
    client = AlphaSignalClient()
    allowed, sentiment = await _allowed(
        client,
        {
            "status": "degraded",
            "degraded": True,
            "degradation_reason": "full_extraction_failure",
            "latest_score": None,
            "reliable_chunk_count": 0,
            "total_chunk_count": 5,
        },
    )
    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_FULL_DEGRADED_BYPASS


async def test_valid_partial_degradation_with_blocking_score_is_evaluated_not_bypassed():
    """Established policy (2026-09-11): a structurally valid partial
    degradation (reliable_chunk_count > 0, non-null latest_score) is
    evaluated through the normal threshold policy, tagged distinctly as
    degraded - it must actually BLOCK here, not fail open."""
    client = AlphaSignalClient(sentiment_threshold=-0.3)
    allowed, sentiment = await _allowed(
        client,
        {
            "status": "degraded",
            "degraded": True,
            "degradation_reason": "partial_extraction_failure",
            "latest_score": -0.9,  # blocks under the normal threshold policy
            "reliable_chunk_count": 2,
            "total_chunk_count": 5,
        },
    )
    assert allowed is False
    assert sentiment["gate_status"] == GATE_STATUS_PARTIAL_DEGRADED_BLOCK


async def test_valid_partial_degradation_with_positive_score_allows():
    client = AlphaSignalClient(sentiment_threshold=-0.3)
    allowed, sentiment = await _allowed(
        client,
        {
            "status": "degraded",
            "degraded": True,
            "degradation_reason": "partial_extraction_failure",
            "latest_score": 0.6,
            "reliable_chunk_count": 3,
            "total_chunk_count": 5,
        },
    )
    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_PARTIAL_DEGRADED_PASS


async def test_valid_partial_degradation_with_genuine_neutral_score_allows():
    client = AlphaSignalClient(sentiment_threshold=-0.3)
    allowed, sentiment = await _allowed(
        client,
        {
            "status": "degraded",
            "degraded": True,
            "degradation_reason": "partial_extraction_failure",
            "latest_score": 0.0,
            "reliable_chunk_count": 1,
            "total_chunk_count": 4,
        },
    )
    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_PARTIAL_DEGRADED_PASS
    assert sentiment["sentiment_score"] == 0.0


async def test_partial_degradation_zero_reliable_chunks_fails_open():
    """No usable reliable evidence despite a present score - must not be
    scored, must fail open distinctly from a valid partial evaluation."""
    client = AlphaSignalClient(sentiment_threshold=-0.3)
    allowed, sentiment = await _allowed(
        client,
        {
            "status": "degraded",
            "degraded": True,
            "degradation_reason": "partial_extraction_failure",
            "latest_score": -0.9,  # present, but zero reliable chunks back it
            "reliable_chunk_count": 0,
            "total_chunk_count": 5,
        },
    )
    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_PARTIAL_DEGRADED_BYPASS


async def test_partial_degradation_null_score_fails_open():
    """A null latest_score means there is nothing to score regardless of
    degradation_reason - treated the same as full degradation (no usable
    evidence), not scored as a valid partial evaluation."""
    client = AlphaSignalClient(sentiment_threshold=-0.3)
    allowed, sentiment = await _allowed(
        client,
        {
            "status": "degraded",
            "degraded": True,
            "degradation_reason": "partial_extraction_failure",
            "latest_score": None,
            "reliable_chunk_count": 3,
            "total_chunk_count": 5,
        },
    )
    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_FULL_DEGRADED_BYPASS


async def test_partial_degradation_count_inconsistency_is_invalid_not_scored():
    """reliable_chunk_count > total_chunk_count is a contract violation
    (caught earlier by _detect_contract_issue) - must never reach the
    scoring path, regardless of how "valid-looking" the rest is."""
    client = AlphaSignalClient(sentiment_threshold=-0.3)
    allowed, sentiment = await _allowed(
        client,
        {
            "status": "degraded",
            "degraded": True,
            "degradation_reason": "partial_extraction_failure",
            "latest_score": -0.9,
            "reliable_chunk_count": 6,
            "total_chunk_count": 5,
        },
    )
    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_INVALID_RESPONSE_BYPASS


async def test_partial_degradation_is_not_reported_as_clean_approval():
    """A degraded pass must use a distinct gate_status from a clean
    AVAILABLE_PASS, and the sentiment dict must expose degraded=True -
    callers/logs must never be able to mistake this for undegraded
    evidence."""
    client = AlphaSignalClient(sentiment_threshold=-0.3)
    allowed, sentiment = await _allowed(
        client,
        {
            "status": "degraded",
            "degraded": True,
            "degradation_reason": "partial_extraction_failure",
            "latest_score": 0.5,
            "reliable_chunk_count": 2,
            "total_chunk_count": 5,
        },
    )
    assert allowed is True
    assert sentiment["gate_status"] != GATE_STATUS_AVAILABLE_PASS
    assert sentiment["gate_status"] == GATE_STATUS_PARTIAL_DEGRADED_PASS
    assert sentiment["degraded"] is True


# ---------------------------------------------------------------------------
# Timeout / 5xx / malformed JSON
# ---------------------------------------------------------------------------


async def test_timeout_fails_open():
    client = AlphaSignalClient()
    mock_client = _mock_async_client(raise_exc=httpx.TimeoutException("timed out"))
    with patch(
        "alphalive.services.alphasignal_client.httpx.AsyncClient",
        return_value=mock_client,
    ):
        allowed, sentiment = await client.is_execution_allowed(TICKER, 2)
    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_ERROR_BYPASS


async def test_5xx_response_fails_open():
    client = AlphaSignalClient()
    allowed, sentiment = await _allowed(client, {}, status_code=503)
    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_ERROR_BYPASS


async def test_malformed_json_fails_open():
    client = AlphaSignalClient()
    response = _mock_response({})
    response.json.side_effect = ValueError("not valid json")
    mock_client = _mock_async_client(response)
    with patch(
        "alphalive.services.alphasignal_client.httpx.AsyncClient",
        return_value=mock_client,
    ):
        allowed, sentiment = await client.is_execution_allowed(TICKER, 2)
    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_ERROR_BYPASS


# ---------------------------------------------------------------------------
# Impossible field combinations - defensive validation
# ---------------------------------------------------------------------------


async def test_status_ok_but_data_unavailable_is_invalid():
    client = AlphaSignalClient()
    allowed, sentiment = await _allowed(
        client,
        {
            "status": "ok",
            "degraded": False,
            "data_available": False,
            "latest_score": 0.2,
        },
    )
    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_INVALID_RESPONSE_BYPASS
    assert sentiment["contract_issue"] is not None


async def test_full_degradation_with_non_null_score_is_invalid():
    client = AlphaSignalClient()
    allowed, sentiment = await _allowed(
        client,
        {
            "status": "degraded",
            "degraded": True,
            "degradation_reason": "full_extraction_failure",
            "latest_score": 0.5,  # should be null for a full failure
        },
    )
    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_INVALID_RESPONSE_BYPASS


async def test_reliable_chunk_count_exceeds_total_is_invalid():
    client = AlphaSignalClient()
    allowed, sentiment = await _allowed(
        client,
        {
            "status": "ok",
            "degraded": False,
            "latest_score": 0.1,
            "reliable_chunk_count": 9,
            "total_chunk_count": 3,
        },
    )
    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_INVALID_RESPONSE_BYPASS


# ---------------------------------------------------------------------------
# Legacy response (no new fields at all) - unaffected
# ---------------------------------------------------------------------------


async def test_legacy_response_with_no_degradation_fields_never_scored_as_degraded():
    """A legacy AlphaSignal response has no `degraded` key at all - must
    never accidentally take the partial-degradation scoring path."""
    client = AlphaSignalClient(sentiment_threshold=-0.3)
    allowed, sentiment = await _allowed(
        client,
        {
            "latest_score": -0.9,
            "signals": [{"score": -0.9, "confidence": 0.7, "source": "x"}],
        },
    )
    assert allowed is False
    assert sentiment["gate_status"] == GATE_STATUS_AVAILABLE_BLOCK
    assert sentiment.get("degraded") is False


async def test_other_risk_controls_remain_active_after_sentiment_allows():
    """A sentiment-gate PASS (degraded or clean) never touches
    RiskManager.can_trade() - it's an entirely separate gate called
    elsewhere in the execution path. Proven here with a real RiskManager
    whose kill switch is independently active."""
    from alphalive.execution.risk_manager import RiskManager
    from alphalive.strategy_schema import Execution, Risk, SafetyLimits

    client = AlphaSignalClient(sentiment_threshold=-0.3)
    allowed, sentiment = await _allowed(
        client,
        {
            "status": "degraded",
            "degraded": True,
            "degradation_reason": "partial_extraction_failure",
            "latest_score": 0.5,
            "reliable_chunk_count": 2,
            "total_chunk_count": 5,
        },
    )
    assert allowed is True  # sentiment gate allowed it

    rm = RiskManager(
        risk_config=Risk(
            stop_loss_pct=2.0,
            take_profit_pct=5.0,
            max_position_size_pct=10.0,
            max_daily_loss_pct=3.0,
            max_open_positions=5,
            portfolio_max_positions=10,
        ),
        execution_config=Execution(
            order_type="market", limit_offset_pct=0.1, cooldown_bars=1
        ),
        strategy_name=TICKER,
        safety_limits=SafetyLimits(),
    )
    rm.trading_paused_by_circuit_breaker = True  # independent halt

    can_trade, reason = rm.can_trade(
        ticker=TICKER,
        signal="BUY",
        account_equity=100000,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    assert can_trade is False
    assert "circuit breaker" in reason.lower()


async def test_legacy_response_without_new_fields_behaves_as_before():
    client = AlphaSignalClient(sentiment_threshold=-0.3)
    allowed, sentiment = await _allowed(
        client,
        {
            "latest_score": 0.5,
            "signals": [{"score": 0.5, "confidence": 0.8, "source": "x"}],
        },
    )
    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_AVAILABLE_PASS
    assert sentiment.get("contract_issue") is None
    assert sentiment.get("degraded") is False


# ---------------------------------------------------------------------------
# Sentiment block / allow / bypass, and that bypass never touches other
# risk gates (the gate only informs is_execution_allowed's own decision -
# it has no reference to RiskManager/OrderManager at all, so this proves
# it structurally rather than by mocking those out).
# ---------------------------------------------------------------------------


async def test_sentiment_block_is_distinguishable_from_bypass():
    client = AlphaSignalClient(sentiment_threshold=-0.3)
    allowed, sentiment = await _allowed(
        client, {"status": "ok", "degraded": False, "latest_score": -0.9, "signals": []}
    )
    assert allowed is False
    assert sentiment["gate_status"] == GATE_STATUS_AVAILABLE_BLOCK


async def test_sentiment_allow_is_available_pass():
    client = AlphaSignalClient(sentiment_threshold=-0.3)
    allowed, sentiment = await _allowed(
        client, {"status": "ok", "degraded": False, "latest_score": 0.1, "signals": []}
    )
    assert allowed is True
    assert sentiment["gate_status"] == GATE_STATUS_AVAILABLE_PASS


async def test_disabled_client_bypasses_via_run_pre_execution_checks():
    allowed, pred = await run_pre_execution_checks(None, TICKER, 2)
    assert allowed is True
    assert pred["gate_status"] == "disabled_bypass"


async def test_gate_result_carries_no_broker_or_risk_state():
    """The gate's return value is a flat dict with no broker/order/risk
    references - proves structurally that a fail-open bypass here cannot
    itself short-circuit any other control (those live entirely in
    RiskManager/OrderManager, called separately)."""
    client = AlphaSignalClient()
    allowed, sentiment = await _allowed(
        client,
        {
            "status": "no_data",
            "degraded": False,
            "data_available": False,
            "latest_score": None,
        },
    )
    assert set(sentiment.keys()) <= {
        "sentiment_score",
        "latest_score",
        "confidence",
        "sources",
        "latency_ms",
        "data_available",
        "status",
        "degraded",
        "degradation_reason",
        "reliable_chunk_count",
        "total_chunk_count",
        "contract_issue",
        "gate_status",
    }
