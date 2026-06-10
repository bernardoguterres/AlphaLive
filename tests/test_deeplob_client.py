"""
Tests for DeepLOBClient.

All async tests run automatically under pytest-asyncio (asyncio_mode = auto).

Coverage:
  1. test_matching_direction_above_threshold   — direction match + confidence OK → allow
  2. test_mismatched_direction                  — direction mismatch → block
  3. test_low_confidence_blocks                 — direction match but low confidence → block
  4. test_timeout_fails_open                    — asyncio.TimeoutError from predict → allow
  5. test_none_snapshot_fails_open              — lob_snapshot=None → allow (no L2 data)
  6. test_health_endpoint_reachable             — start serve.py subprocess, GET /health
"""

import asyncio
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import requests

from alphalive.services.deeplob_client import DeepLOBClient

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

LOB_40 = [float(i) for i in range(40)]  # 40-element dummy snapshot

DEEPLOB_DIR = Path(__file__).parent.parent.parent / "DeepLOB"
MODEL_PATH = DEEPLOB_DIR / "outputs" / "best_model_k10.pt"
SERVE_SCRIPT = DEEPLOB_DIR / "deeplob" / "serve.py"

HEALTH_PORT = 8002
HEALTH_URL = f"http://127.0.0.1:{HEALTH_PORT}/health"

_MODEL_MISSING = not MODEL_PATH.exists()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(threshold: float = 0.6) -> DeepLOBClient:
    return DeepLOBClient(confidence_threshold=threshold)


def _pred_dict(direction: int, confidence: float) -> dict:
    probs = [0.0, 0.0, 0.0]
    probs[direction] = confidence
    remaining = (1.0 - confidence) / 2
    for i in range(3):
        if i != direction:
            probs[i] = remaining
    return {
        "direction": direction,
        "confidence": confidence,
        "probabilities": probs,
        "latency_ms": 5.0,
    }


# ---------------------------------------------------------------------------
# Test 1 — matching direction above threshold allows execution
# ---------------------------------------------------------------------------


async def test_matching_direction_above_threshold():
    """direction=2 matches intended=2, confidence=0.75 >= 0.6 → allow."""
    client = _make_client()

    with patch.object(client, "predict", new=AsyncMock(return_value=_pred_dict(2, 0.75))):
        allowed, pred = await client.is_execution_allowed(
            lob_snapshot=LOB_40, intended_direction=2
        )

    assert allowed is True
    assert pred["direction"] == 2
    assert pred["confidence"] == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Test 2 — mismatched direction blocks execution
# ---------------------------------------------------------------------------


async def test_mismatched_direction():
    """direction=0 does not match intended=2, regardless of confidence → block."""
    client = _make_client()

    with patch.object(client, "predict", new=AsyncMock(return_value=_pred_dict(0, 0.80))):
        allowed, pred = await client.is_execution_allowed(
            lob_snapshot=LOB_40, intended_direction=2
        )

    assert allowed is False
    assert pred["direction"] == 0


# ---------------------------------------------------------------------------
# Test 3 — matching direction but low confidence blocks execution
# ---------------------------------------------------------------------------


async def test_low_confidence_blocks():
    """direction=2 matches intended=2 but confidence=0.45 < 0.6 threshold → block."""
    client = _make_client(threshold=0.6)

    with patch.object(client, "predict", new=AsyncMock(return_value=_pred_dict(2, 0.45))):
        allowed, pred = await client.is_execution_allowed(
            lob_snapshot=LOB_40, intended_direction=2
        )

    assert allowed is False
    assert pred["confidence"] == pytest.approx(0.45)


# ---------------------------------------------------------------------------
# Test 4 — timeout from predict fails open
# ---------------------------------------------------------------------------


async def test_timeout_fails_open():
    """asyncio.TimeoutError from predict must return (True, {}) — fail open."""
    client = _make_client()

    with patch.object(
        client, "predict", new=AsyncMock(side_effect=asyncio.TimeoutError)
    ):
        allowed, pred = await client.is_execution_allowed(
            lob_snapshot=LOB_40, intended_direction=2
        )

    assert allowed is True
    assert pred == {}


# ---------------------------------------------------------------------------
# Test 5 — None snapshot fails open (no L2 data available)
# ---------------------------------------------------------------------------


async def test_none_snapshot_fails_open():
    """lob_snapshot=None means no L2 feed available — must never block execution."""
    client = _make_client()

    allowed, pred = await client.is_execution_allowed(
        lob_snapshot=None, intended_direction=2
    )

    assert allowed is True
    assert pred == {}


# ---------------------------------------------------------------------------
# Test 6 — health endpoint reachable (requires outputs/best_model_k10.pt)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    _MODEL_MISSING,
    reason=f"Skipped: {MODEL_PATH} not found (model may still be training)",
)
def test_health_endpoint_reachable():
    """Start the serve.py inference server, hit /health, assert status == 'ok'."""
    proc = subprocess.Popen(
        [
            sys.executable,
            str(SERVE_SCRIPT),
            "--k", "10",
            "--checkpoint_dir", str(DEEPLOB_DIR / "outputs"),
            "--port", str(HEALTH_PORT),
        ],
        cwd=str(DEEPLOB_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        # Poll until the server is ready (up to 30 s; model load can take a moment).
        deadline = time.monotonic() + 30.0
        last_exc: Exception = RuntimeError("Server did not start in time")
        while time.monotonic() < deadline:
            try:
                resp = requests.get(HEALTH_URL, timeout=2)
                if resp.status_code == 200:
                    break
            except Exception as exc:
                last_exc = exc
            time.sleep(0.5)
        else:
            pytest.fail(f"DeepLOB server did not become healthy within 30s: {last_exc}")

        data = resp.json()
        assert data["status"] == "ok", f"Unexpected health response: {data}"
        assert "model" in data
        assert "device" in data
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
