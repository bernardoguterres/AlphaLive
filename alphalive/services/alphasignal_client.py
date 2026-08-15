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
            "confidence": confidence,
            "sources": sources,
            "latency_ms": latency_ms,
            "data_available": data.get("data_available", True),
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
        - data_available is False (AlphaSignal has no ingested data for
          this ticker/period): always allow - fail open, same policy as
          before, but now explicit rather than an artifact of
          sentiment_score defaulting to 0.0. See gate_status below.
        - On timeout or any error: fail open.

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

        if not sentiment.get("data_available", True):
            logger.info(
                "AlphaSignal has no data for %s - failing open (no_data_bypass)",
                ticker,
            )
            return True, {**sentiment, "gate_status": GATE_STATUS_NO_DATA_BYPASS}

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
