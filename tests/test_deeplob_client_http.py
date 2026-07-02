"""
Additional DeepLOBClient coverage for the real predict() HTTP call
(httpx mocked, no network) and the wrong-length snapshot edge case.
tests/test_deeplob_client.py already covers is_execution_allowed()'s
threshold/direction logic via a mocked predict(), and its falsy-snapshot
(None/empty-list) fail-open branch — this fills in the HTTP request/response
handling that those tests bypass, plus the distinct "truthy but wrong
length" fail-open branch.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from alphalive.services.deeplob_client import DeepLOBClient

LOB_40 = [float(i) for i in range(40)]


def _mock_response(json_data):
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


def _mock_async_client(response=None, side_effect=None):
    client = AsyncMock()
    if side_effect is not None:
        client.post = AsyncMock(side_effect=side_effect)
    else:
        client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


async def test_predict_posts_snapshot_and_adds_latency():
    client = DeepLOBClient()
    response = _mock_response({"direction": 2, "confidence": 0.8, "probabilities": [0.1, 0.1, 0.8]})
    mock_client = _mock_async_client(response)

    with patch("alphalive.services.deeplob_client.httpx.AsyncClient", return_value=mock_client):
        result = await client.predict(LOB_40)

    assert result["direction"] == 2
    assert "latency_ms" in result
    mock_client.post.assert_called_once()
    _, kwargs = mock_client.post.call_args
    assert kwargs["json"] == {"lob_snapshot": LOB_40}


async def test_predict_timeout_raises_asyncio_timeout_error():
    client = DeepLOBClient()
    mock_client = _mock_async_client(side_effect=httpx.TimeoutException("timed out"))

    with patch("alphalive.services.deeplob_client.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(TimeoutError):
            await client.predict(LOB_40)


async def test_is_execution_allowed_wrong_length_snapshot_fails_open():
    client = DeepLOBClient()
    allowed, pred = await client.is_execution_allowed(
        lob_snapshot=[1.0, 2.0], intended_direction=2
    )
    assert allowed is True
    assert pred == {}
