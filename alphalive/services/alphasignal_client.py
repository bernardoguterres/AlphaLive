"""
AlphaSignal Sentiment Client

Fetches sentiment scores from the AlphaSignal financial RAG REST API
and gates order execution based on those scores.

Real API endpoint used: GET /sentiment/{ticker}
  Response schema (SentimentResponse):
    ticker       str
    signals      list[SentimentSignal]
      score      float [-1.0, 1.0]
      confidence float [0.0, 1.0]
      source     str
      ...
    latest_score float | None   # top-level convenience field
    latency_ms   int

Auth: X-API-Key header, sent when ALPHASIGNAL_API_KEY is set
(required once AlphaSignal is deployed with auth enabled; same env var name
on both services).
"""

import asyncio
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Gate outcome, exposed as sentiment_pred["gate_status"] (FINAL_ENGINEERING_AUDIT.md
# remediation item 3, 2026-08-15). Previously latest_score=None (AlphaSignal has no
# ingested data for this ticker/period) and a genuinely neutral score both silently
# normalised to sentiment_score=0.0, making "no data" and "confirmed neutral"
# indistinguishable downstream. The fail-open policy itself is unchanged - all bypass
# paths still return allowed=True - this only makes *why* observable in logs/state.
GATE_STATUS_AVAILABLE_PASS = "available_pass"  # data available, sentiment allows
GATE_STATUS_AVAILABLE_BLOCK = "available_block"  # data available, sentiment blocks
GATE_STATUS_NO_DATA_BYPASS = "no_data_bypass"  # AlphaSignal has no data - fail open
GATE_STATUS_ERROR_BYPASS = (
    "error_bypass"  # timeout/network/unexpected error - fail open
)
GATE_STATUS_NEUTRAL_BYPASS = "neutral_bypass"  # HOLD direction - gate not consulted
GATE_STATUS_DISABLED_BYPASS = "disabled_bypass"  # no client configured - gate off

# Added for AlphaSignal's finalized degradation contract (status/degraded/
# degradation_reason/reliable_chunk_count/total_chunk_count/signals[].reliable
# on GET /sentiment/{ticker}). All additive/optional fields - a legacy
# response missing them is treated exactly as before (GATE_STATUS_AVAILABLE_*
# or GATE_STATUS_NO_DATA_BYPASS). See AlphaSignalClient.get_sentiment's
# docstring for the full field semantics this client now distinguishes.
GATE_STATUS_FULL_DEGRADED_BYPASS = (
    "full_degraded_bypass"  # full_extraction_failure / latest_score null - fail open
)
GATE_STATUS_PARTIAL_DEGRADED_BYPASS = (
    "partial_degraded_bypass"  # partial degradation with no usable reliable evidence
    # (zero reliable chunks, or an unrecognized degradation_reason) - fail open
)
GATE_STATUS_PARTIAL_DEGRADED_PASS = (
    "partial_degraded_pass"  # valid partial degradation, reliable score evaluated,
    # sentiment allows - degraded evidence, NOT a clean/undegraded approval
)
GATE_STATUS_PARTIAL_DEGRADED_BLOCK = (
    "partial_degraded_block"  # valid partial degradation, reliable score evaluated,
    # sentiment blocks
)
GATE_STATUS_INVALID_RESPONSE_BYPASS = (
    "invalid_response_bypass"  # impossible field combination - fail open
)

# Partial-degradation product policy (established 2026-09-11): a
# structurally valid partially-degraded response - status="degraded",
# degradation_reason="partial_extraction_failure", at least one reliable
# chunk (reliable_chunk_count > 0), reliable_chunk_count <= total_chunk_count,
# and a non-null latest_score - is evaluated through the SAME
# threshold/confidence policy as an undegraded response (see
# is_execution_allowed). The result is tagged GATE_STATUS_PARTIAL_DEGRADED_PASS
# or _BLOCK, never the plain AVAILABLE_PASS/_BLOCK statuses, so a degraded
# evaluation is never logged or reported as a clean approval. A partial
# response that fails any of those conditions (zero reliable chunks, a null
# score, or an unrecognized degradation_reason) has no usable evidence to
# score and fails open instead (GATE_STATUS_PARTIAL_DEGRADED_BYPASS) - same
# as full degradation. Count inconsistencies (reliable > total) and other
# impossible combinations are caught earlier by _detect_contract_issue and
# fail open as GATE_STATUS_INVALID_RESPONSE_BYPASS, never scored.


def _detect_contract_issue(data: dict, latest_score: Optional[float]) -> Optional[str]:
    """Defensively check a /sentiment/{ticker} response for internally
    impossible field combinations. Returns a short categorical reason
    string (safe to log - never echoes the raw response body) or None.

    All three checks only fire when the relevant new fields are actually
    present, so a legacy response (which omits them entirely) is never
    flagged - this exists to catch a genuinely self-contradictory response
    from a service claiming to implement the finalized contract, not to
    penalize an older one that predates it.
    """
    status = data.get("status")
    data_available = data.get("data_available")
    degradation_reason = data.get("degradation_reason")
    reliable_chunk_count = data.get("reliable_chunk_count")
    total_chunk_count = data.get("total_chunk_count")

    if status == "ok" and data_available is False:
        return "status=ok but data_available=false"
    if degradation_reason == "full_extraction_failure" and latest_score is not None:
        return "degradation_reason=full_extraction_failure but latest_score is not null"
    if (
        reliable_chunk_count is not None
        and total_chunk_count is not None
        and reliable_chunk_count > total_chunk_count
    ):
        return "reliable_chunk_count exceeds total_chunk_count"
    return None


class AlphaSignalClient:
    """Client for the AlphaSignal financial RAG sentiment API.

    Args:
        base_url: AlphaSignal service URL. From config.
        api_key: Auth key if required. From config.
        timeout_seconds: Request timeout. Default 3.0.
        sentiment_threshold: Score below which execution is suppressed.
            Default -0.3. Range assumed [-1.0, 1.0].
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: str = "",
        timeout_seconds: float = 3.0,
        sentiment_threshold: float = -0.3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.sentiment_threshold = sentiment_threshold

    async def get_sentiment(
        self,
        ticker: str,
        query: Optional[str] = None,
    ) -> dict:
        """Fetch current sentiment score for a ticker.

        Calls ``GET /sentiment/{ticker}`` on the AlphaSignal service and
        normalises the response into a flat dict suitable for downstream
        threshold logic.

        Args:
            ticker: Stock ticker symbol (e.g. "AAPL").
            query: Optional context query for RAG retrieval.
                Currently unused - AlphaSignal's GET endpoint handles
                retrieval internally. Defaults to
                "{ticker} recent news sentiment" for documentation
                purposes. Reserved for a future POST /query integration.

        Returns:
            Dict with keys:
                sentiment_score (float): Aggregate score in [-1.0, 1.0].
                    Defaults to 0.0 (neutral) when AlphaSignal has no
                    ingested documents for the ticker.
                confidence (float): Confidence of the most-recent signal
                    in [0.0, 1.0]. 0.0 when no signals are available.
                sources (list[str]): Source identifiers of the retrieved
                    document chunks used to produce the scores.
                latency_ms (float): Round-trip time in milliseconds.
                data_available (bool): AlphaSignal's own
                    SentimentResponse.data_available flag - False means
                    AlphaSignal has zero ingested chunks for this ticker/
                    date range, so sentiment_score's 0.0 is a fallback, not
                    a genuine neutral reading. Defaults to True if the
                    response omits the field (older/mocked responses),
                    matching the pre-2026-08-15 behaviour for those callers.
                status (str | None): AlphaSignal's "ok" | "no_data" |
                    "degraded", or None on a legacy response that omits it.
                degraded (bool): True if AlphaSignal flags this response as
                    degraded (partial or full extraction failure). False
                    (not just absent) on legacy responses - matches the
                    pre-degradation-contract behaviour of trusting the
                    score at face value.
                degradation_reason (str | None): "partial_extraction_failure"
                    | "full_extraction_failure" | None.
                reliable_chunk_count / total_chunk_count (int | None): as
                    returned by AlphaSignal, unvalidated beyond the
                    defensive checks in `contract_issue`.
                contract_issue (str | None): set when the response contains
                    an internally-impossible combination of the fields
                    above (e.g. status="ok" with data_available=false).
                    When set, callers must not trust sentiment_score -
                    is_execution_allowed() fails open with
                    GATE_STATUS_INVALID_RESPONSE_BYPASS rather than acting
                    on a self-contradictory response.

        Raises:
            asyncio.TimeoutError: If the request exceeds timeout_seconds.
            httpx.HTTPStatusError: On non-2xx responses.
            httpx.RequestError: On network-level errors.
        """
        url = f"{self.base_url}/sentiment/{ticker}"
        headers: dict[str, str] = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            # Re-raise as asyncio.TimeoutError so callers can handle uniformly.
            raise asyncio.TimeoutError(
                f"AlphaSignal timed out after {self.timeout_seconds}s for {ticker}"
            ) from exc

        latency_ms = (time.monotonic() - t0) * 1000.0

        # latest_score is the convenience top-level field on SentimentResponse.
        latest_score: Optional[float] = data.get("latest_score")
        sentiment_score = latest_score if latest_score is not None else 0.0

        signals: list[dict] = data.get("signals", [])
        confidence: float = signals[0]["confidence"] if signals else 0.0
        sources: list[str] = [s["source"] for s in signals]

        return {
            "sentiment_score": sentiment_score,
            "latest_score": latest_score,  # raw, possibly-None value (sentiment_score
            # defaults it to 0.0 for backward-compat callers - the degraded-response
            # branch in is_execution_allowed needs to tell "genuinely null" apart from
            # "genuinely 0.0", which sentiment_score alone can no longer do)
            "confidence": confidence,
            "sources": sources,
            "latency_ms": latency_ms,
            "data_available": data.get("data_available", True),
            "status": data.get("status"),
            "degraded": bool(data.get("degraded", False)),
            "degradation_reason": data.get("degradation_reason"),
            "reliable_chunk_count": data.get("reliable_chunk_count"),
            "total_chunk_count": data.get("total_chunk_count"),
            "contract_issue": _detect_contract_issue(data, latest_score),
        }

    async def is_execution_allowed(
        self,
        ticker: str,
        intended_direction: int,
    ) -> tuple[bool, dict]:
        """Check whether sentiment confirms execution.

        Suppresses execution only on strong negative sentiment (for longs)
        or strong positive sentiment (for shorts). Neutral direction is
        always allowed.

        Blocking rules (threshold default = -0.3):
        - intended_direction == 2 (long): block if sentiment_score
          < self.sentiment_threshold (strongly negative → do not buy).
        - intended_direction == 0 (short): block if sentiment_score
          > -self.sentiment_threshold (strongly positive → do not short).
        - intended_direction == 1 (neutral/flat): always allow.
        - data_available is False, or status == "no_data" (AlphaSignal has
          no ingested data for this ticker/period): always allow - fail
          open, same policy as before, but now explicit rather than an
          artifact of sentiment_score defaulting to 0.0. See gate_status
          below.
        - degraded is True with degradation_reason="full_extraction_failure"
          (or any degraded response with a null latest_score): always
          allow - fail open (GATE_STATUS_FULL_DEGRADED_BYPASS).
        - degraded is True with degradation_reason="partial_extraction_failure"
          AND at least one reliable chunk AND a non-null latest_score: the
          reliable evidence IS evaluated through the normal threshold
          policy (established 2026-09-11 policy) - result tagged
          GATE_STATUS_PARTIAL_DEGRADED_PASS/_BLOCK, distinct from a clean
          AVAILABLE_PASS/_BLOCK so a degraded evaluation is never reported
          as undegraded.
        - degraded is True but with no usable reliable evidence (zero
          reliable chunks, or an unrecognized degradation_reason): fail
          open (GATE_STATUS_PARTIAL_DEGRADED_BYPASS).
        - The response contains an internally impossible field combination
          (contract_issue set): always allow - fail open
          (GATE_STATUS_INVALID_RESPONSE_BYPASS). A self-contradictory
          response is not trustworthy enough to act on in either
          direction.
        - On timeout or any error: fail open.

        A fail-open bypass here only means "sentiment did not block this
        order" - it never bypasses risk_manager.can_trade() (position
        limits, drawdown controls, kill switches), order-manager duplicate/
        idempotency checks, or broker reconciliation. Those are independent
        gates evaluated elsewhere in the execution path.

        Args:
            ticker: Stock ticker symbol (e.g. "AAPL").
            intended_direction: Signal direction encoded as int.
                2 = long (BUY), 1 = neutral (HOLD), 0 = short (SELL).

        Returns:
            Tuple of (allowed: bool, sentiment_dict: dict). sentiment_dict
            always carries a "gate_status" key (one of the GATE_STATUS_*
            constants module-level) so callers/logs can distinguish real
            neutral sentiment from a lack of data or an error, even though
            all three currently share the same fail-open `allowed=True`
            outcome. Empty except for gate_status on neutral direction or
            error; the full get_sentiment() dict plus gate_status otherwise.
        """
        # Neutral direction - no sentiment gating needed.
        if intended_direction == 1:
            return True, {"gate_status": GATE_STATUS_NEUTRAL_BYPASS}

        try:
            sentiment = await self.get_sentiment(ticker)
        except Exception as exc:
            logger.warning(
                "AlphaSignal check failed for %s (failing open): %s",
                ticker,
                exc,
            )
            return True, {"gate_status": GATE_STATUS_ERROR_BYPASS}

        contract_issue = sentiment.get("contract_issue")
        if contract_issue is not None:
            logger.warning(
                "AlphaSignal response for %s failed a defensive contract check "
                "(%s) - failing open (invalid_response_bypass). Not logging the "
                "raw response body.",
                ticker,
                contract_issue,
            )
            return True, {
                **sentiment,
                "gate_status": GATE_STATUS_INVALID_RESPONSE_BYPASS,
            }

        if (
            not sentiment.get("data_available", True)
            or sentiment.get("status") == "no_data"
        ):
            logger.info(
                "AlphaSignal has no data for %s - failing open (no_data_bypass)",
                ticker,
            )
            return True, {**sentiment, "gate_status": GATE_STATUS_NO_DATA_BYPASS}

        if sentiment.get("degraded"):
            reason = sentiment.get("degradation_reason")
            raw_score = sentiment.get("latest_score")
            reliable_count = sentiment.get("reliable_chunk_count")
            total_count = sentiment.get("total_chunk_count")

            valid_partial = (
                reason == "partial_extraction_failure"
                and reliable_count is not None
                and reliable_count > 0
                and raw_score is not None
            )

            if valid_partial:
                # Established policy (2026-09-11): score the reliable
                # evidence through the normal threshold/confidence policy,
                # but tag the result distinctly - never a clean approval.
                score: float = sentiment["sentiment_score"]
                if intended_direction == 2:
                    allowed = score >= self.sentiment_threshold
                elif intended_direction == 0:
                    allowed = score <= -self.sentiment_threshold
                else:
                    allowed = True
                gate_status = (
                    GATE_STATUS_PARTIAL_DEGRADED_PASS
                    if allowed
                    else GATE_STATUS_PARTIAL_DEGRADED_BLOCK
                )
                logger.info(
                    "AlphaSignal partially degraded for %s (%s/%s reliable "
                    "chunks) - evaluating reliable evidence under normal "
                    "threshold policy: score=%.3f threshold=%.3f -> %s "
                    "(degraded evidence, not a clean approval)",
                    ticker,
                    reliable_count,
                    total_count,
                    score,
                    self.sentiment_threshold,
                    gate_status,
                )
                return allowed, {**sentiment, "gate_status": gate_status}

            if reason == "full_extraction_failure" or raw_score is None:
                logger.info(
                    "AlphaSignal fully degraded (or no usable score) for %s "
                    "(%s) - failing open (full_degraded_bypass)",
                    ticker,
                    reason,
                )
                return True, {
                    **sentiment,
                    "gate_status": GATE_STATUS_FULL_DEGRADED_BYPASS,
                }

            # partial_extraction_failure with no usable reliable evidence
            # (zero reliable chunks despite a non-null score), or an
            # unrecognized-but-truthy degradation_reason - insufficient
            # evidence to score, fail open distinctly from full degradation.
            logger.info(
                "AlphaSignal degraded for %s (%s, %s reliable chunks) with "
                "no usable reliable evidence - failing open "
                "(partial_degraded_bypass)",
                ticker,
                reason,
                reliable_count,
            )
            return True, {
                **sentiment,
                "gate_status": GATE_STATUS_PARTIAL_DEGRADED_BYPASS,
            }

        score: float = sentiment["sentiment_score"]

        if intended_direction == 2:  # Long - block on strongly negative sentiment.
            allowed = score >= self.sentiment_threshold
        elif intended_direction == 0:  # Short - block on strongly positive sentiment.
            allowed = score <= -self.sentiment_threshold
        else:
            allowed = True

        gate_status = (
            GATE_STATUS_AVAILABLE_PASS if allowed else GATE_STATUS_AVAILABLE_BLOCK
        )

        if not allowed:
            logger.info(
                "AlphaSignal blocking %s direction=%d: score=%.3f threshold=%.3f",
                ticker,
                intended_direction,
                score,
                self.sentiment_threshold,
            )

        return allowed, {**sentiment, "gate_status": gate_status}


# ---------------------------------------------------------------------------
# Concurrent execution-gate helper
# ---------------------------------------------------------------------------


async def run_pre_execution_checks(
    alphasignal_client: Optional[AlphaSignalClient],
    ticker: str,
    signal_direction: int,
) -> tuple[bool, dict]:
    """Run the pre-execution filter gate before placing an order.

    Currently a single arm (AlphaSignal sentiment), but kept as a gather
    over a coroutine list so a future second filter (e.g. an execution-
    timing model) slots in as one more coroutine with fail-open handling
    for free. The DeepLOB arm that used to live here was removed
    2026-07-10 along with the DeepLOB project.

    On any exception (timeout, network error, etc.) the filter fails
    open - execution is allowed.

    Args:
        alphasignal_client: Initialised ``AlphaSignalClient``, or None
            (passthrough - always allow).
        ticker: Stock ticker symbol.
        signal_direction: 2=long, 1=neutral, 0=short.

    Returns:
        ``(sentiment_allowed, sentiment_pred)`` where ``sentiment_pred``
        is the raw filter output (empty except for "gate_status" on error
        or passthrough - see AlphaSignalClient.is_execution_allowed's
        GATE_STATUS_* constants).
    """
    if alphasignal_client is None:
        return True, {"gate_status": GATE_STATUS_DISABLED_BYPASS}

    (sentiment_result,) = await asyncio.gather(
        alphasignal_client.is_execution_allowed(ticker, signal_direction),
        return_exceptions=True,
    )

    if isinstance(sentiment_result, BaseException):
        logger.warning("AlphaSignal filter raised (failing open): %s", sentiment_result)
        return True, {"gate_status": GATE_STATUS_ERROR_BYPASS}

    return sentiment_result
