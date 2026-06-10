"""DeepLOB LOB-prediction client for AlphaLive execution gate.

Queries the DeepLOB inference REST endpoint before order placement and
gates execution based on whether the predicted mid-price direction matches
the strategy's intended direction with sufficient confidence.
"""

import asyncio
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class DeepLOBClient:
    """Client for the DeepLOB prediction REST endpoint.

    Args:
        base_url: URL of the DeepLOB inference server.
            Default: from config (DEEPLOB_URL env var).
        confidence_threshold: Minimum prediction confidence to allow
            execution. Default 0.6.
        timeout_seconds: Request timeout. Default 2.0 — must not
            block AlphaLive's execution loop.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8001",
        confidence_threshold: float = 0.6,
        timeout_seconds: float = 2.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.confidence_threshold = confidence_threshold
        self.timeout_seconds = timeout_seconds

    async def predict(
        self,
        lob_snapshot: list[float],
    ) -> dict:
        """Get mid-price direction prediction for a LOB snapshot.

        Args:
            lob_snapshot: 40 LOB features in FI-2010 order
                (ask_price_L1, ask_vol_L1, ... bid_vol_L10).

        Returns:
            Dict with keys:
                direction: int — 0=down, 1=stationary, 2=up
                confidence: float — max softmax probability
                probabilities: list[float] — [p_down, p_stat, p_up]
                latency_ms: float — round-trip time in milliseconds

        Raises:
            asyncio.TimeoutError: If the request exceeds timeout_seconds.
            httpx.HTTPStatusError: On non-2xx responses.
            httpx.RequestError: On network-level errors.
        """
        url = f"{self.base_url}/predict"
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(url, json={"lob_snapshot": lob_snapshot})
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            raise asyncio.TimeoutError(
                f"DeepLOB timed out after {self.timeout_seconds}s"
            ) from exc

        data["latency_ms"] = (time.monotonic() - t0) * 1000.0
        return data

    async def is_execution_allowed(
        self,
        lob_snapshot: Optional[list[float]],
        intended_direction: int,
    ) -> tuple[bool, dict]:
        """Check whether DeepLOB confirms execution.

        Returns True only if:
        - lob_snapshot is not None and has exactly 40 features
        - predicted direction matches intended_direction
        - confidence >= self.confidence_threshold
        - request completes within timeout

        If lob_snapshot is None or empty (no L2 feed available):
            fail open — return (True, {}).
        On timeout or connection error:
            fail open — return (True, {}).

        Never block execution due to inference unavailability.

        Args:
            lob_snapshot: 40 LOB features, or None when no L2 data feed
                is configured (Alpaca free tier does not provide L2).
            intended_direction: 0=short, 1=neutral, 2=long.

        Returns:
            Tuple of (allowed: bool, prediction_dict: dict).
            prediction_dict is empty on timeout/error/no-data.
        """
        if not lob_snapshot or len(lob_snapshot) != 40:
            return True, {}

        try:
            prediction = await self.predict(lob_snapshot)
        except Exception as exc:
            logger.warning("DeepLOB check failed (failing open): %s", exc)
            return True, {}

        direction: int = prediction["direction"]
        confidence: float = prediction["confidence"]
        allowed = direction == intended_direction and confidence >= self.confidence_threshold

        if not allowed:
            logger.info(
                "DeepLOB blocking execution: predicted_direction=%d intended_direction=%d "
                "confidence=%.3f threshold=%.3f | prediction=%s",
                direction,
                intended_direction,
                confidence,
                self.confidence_threshold,
                prediction,
            )

        return allowed, prediction
