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

Auth: none required by AlphaSignal.
"""

import asyncio
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


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
        query: Optional[str] = None,  # noqa: ARG002 — reserved for future RAG context
    ) -> dict:
        """Fetch current sentiment score for a ticker.

        Calls ``GET /sentiment/{ticker}`` on the AlphaSignal service and
        normalises the response into a flat dict suitable for downstream
        threshold logic.

        Args:
            ticker: Stock ticker symbol (e.g. "AAPL").
            query: Optional context query for RAG retrieval.
                Currently unused — AlphaSignal's GET endpoint handles
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
        - On timeout or any error: fail open — return (True, {}).

        Args:
            ticker: Stock ticker symbol (e.g. "AAPL").
            intended_direction: Signal direction encoded as int.
                2 = long (BUY), 1 = neutral (HOLD), 0 = short (SELL).

        Returns:
            Tuple of (allowed: bool, sentiment_dict: dict).
            sentiment_dict is empty ({}) on errors or when direction is
            neutral; otherwise it is the full dict from get_sentiment().
        """
        # Neutral direction — no sentiment gating needed.
        if intended_direction == 1:
            return True, {}

        try:
            sentiment = await self.get_sentiment(ticker)
        except Exception as exc:
            logger.warning(
                "AlphaSignal check failed for %s (failing open): %s",
                ticker,
                exc,
            )
            return True, {}

        score: float = sentiment["sentiment_score"]

        if intended_direction == 2:  # Long — block on strongly negative sentiment.
            allowed = score >= self.sentiment_threshold
        elif intended_direction == 0:  # Short — block on strongly positive sentiment.
            allowed = score <= -self.sentiment_threshold
        else:
            allowed = True

        if not allowed:
            logger.info(
                "AlphaSignal blocking %s direction=%d: score=%.3f threshold=%.3f",
                ticker,
                intended_direction,
                score,
                self.sentiment_threshold,
            )

        return allowed, sentiment


# ---------------------------------------------------------------------------
# Concurrent execution-gate helper
# ---------------------------------------------------------------------------

async def run_pre_execution_checks(
    deeplob_client,
    alphasignal_client: AlphaSignalClient,
    lob_snapshot,
    ticker: str,
    signal_direction: int,
) -> tuple[bool, dict, bool, dict]:
    """Run DeepLOB and AlphaSignal execution filters concurrently.

    Uses ``asyncio.gather`` so both network calls happen in parallel,
    halving latency compared to sequential awaits.

    When ``deeplob_client`` is None (Integration A not yet wired), a
    passthrough coroutine is used that always returns ``(True, {})``, so
    the gather still fires with two coroutines and the AlphaSignal check
    is not skipped.

    On any exception from either coroutine (timeout, network error, etc.),
    that filter fails open — execution is allowed.

    Args:
        deeplob_client: DeepLOB client (Integration A). ``None`` = pass-
            through (always allow). Expected interface:
            ``async is_execution_allowed(lob_snapshot, signal_direction)
              -> tuple[bool, dict]``.
        alphasignal_client: Initialised ``AlphaSignalClient``.
        lob_snapshot: LOB data passed to DeepLOB. ``None`` when not yet
            available (Integration A placeholder).
        ticker: Stock ticker symbol.
        signal_direction: 2=long, 1=neutral, 0=short.

    Returns:
        ``(lob_allowed, lob_pred, sentiment_allowed, sentiment_pred)``
        where each ``*_pred`` dict is the raw filter output (empty on
        error or passthrough).
    """
    # Build DeepLOB coroutine — passthrough when client is absent.
    if deeplob_client is not None:
        lob_coro = deeplob_client.is_execution_allowed(lob_snapshot, signal_direction)
    else:
        async def _lob_passthrough() -> tuple[bool, dict]:
            return True, {}

        lob_coro = _lob_passthrough()

    sentiment_coro = alphasignal_client.is_execution_allowed(ticker, signal_direction)

    # Run both filters concurrently.
    lob_result, sentiment_result = await asyncio.gather(
        lob_coro,
        sentiment_coro,
        return_exceptions=True,
    )

    # Handle exceptions from either filter — fail open.
    if isinstance(lob_result, BaseException):
        logger.warning("DeepLOB filter raised (failing open): %s", lob_result)
        lob_allowed, lob_pred = True, {}
    else:
        lob_allowed, lob_pred = lob_result

    if isinstance(sentiment_result, BaseException):
        logger.warning("AlphaSignal filter raised (failing open): %s", sentiment_result)
        sentiment_allowed, sentiment_pred = True, {}
    else:
        sentiment_allowed, sentiment_pred = sentiment_result

    return lob_allowed, lob_pred, sentiment_allowed, sentiment_pred
