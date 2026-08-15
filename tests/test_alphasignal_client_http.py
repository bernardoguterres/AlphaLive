"""
Additional AlphaSignalClient coverage: the real get_sentiment() HTTP call
(httpx mocked, no network), and run_pre_execution_checks()'s None-client
passthrough and exception-handling branches. tests/test_alphasignal_client.py
already covers the threshold/direction logic and the concurrent-gather
wiring - this file fills in what wasn't reachable from those tests.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from alphalive.services.alphasignal_client import (
    GATE_STATUS_DISABLED_BYPASS,
    GATE_STATUS_ERROR_BYPASS,
    AlphaSignalClient,
    run_pre_execution_checks,
)


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


def _mock_async_client(response):
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


async def test_get_sentiment_parses_response_and_headers():
    client = AlphaSignalClient(api_key="secret-key")
    response = _mock_response(
        {
            "latest_score": 0.4,
            "signals": [{"score": 0.4, "confidence": 0.9, "source": "AAPL_10-K"}],
        }
    )
    mock_client = _mock_async_client(response)

    with patch(
        "alphalive.services.alphasignal_client.httpx.AsyncClient",
        return_value=mock_client,
    ):
        result = await client.get_sentiment("AAPL")

    assert result["sentiment_score"] == 0.4
    assert result["confidence"] == 0.9
    assert result["sources"] == ["AAPL_10-K"]
    assert result["data_available"] is True  # field absent from response -> default
    mock_client.get.assert_called_once()
    _, kwargs = mock_client.get.call_args
    assert kwargs["headers"]["X-API-Key"] == "secret-key"


async def test_get_sentiment_no_signals_defaults_neutral():
    client = AlphaSignalClient()
    response = _mock_response({"latest_score": None, "signals": []})
    mock_client = _mock_async_client(response)

    with patch(
        "alphalive.services.alphasignal_client.httpx.AsyncClient",
        return_value=mock_client,
    ):
        result = await client.get_sentiment("AAPL")

    assert result["sentiment_score"] == 0.0
    assert result["confidence"] == 0.0
    assert result["sources"] == []


async def test_get_sentiment_parses_data_available_false():
    """The real AlphaSignal contract sends data_available=False alongside
    latest_score=None when it has zero ingested chunks for the ticker -
    must be surfaced, not silently dropped (audit remediation item 3)."""
    client = AlphaSignalClient()
    response = _mock_response(
        {"latest_score": None, "signals": [], "data_available": False}
    )
    mock_client = _mock_async_client(response)

    with patch(
        "alphalive.services.alphasignal_client.httpx.AsyncClient",
        return_value=mock_client,
    ):
        result = await client.get_sentiment("AAPL")

    assert result["sentiment_score"] == 0.0
    assert result["data_available"] is False


async def test_get_sentiment_timeout_raises_asyncio_timeout_error():
    client = AlphaSignalClient()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "alphalive.services.alphasignal_client.httpx.AsyncClient",
        return_value=mock_client,
    ):
        with pytest.raises(TimeoutError):
            await client.get_sentiment("AAPL")


async def test_get_sentiment_no_api_key_omits_header():
    client = AlphaSignalClient(api_key="")
    response = _mock_response({"latest_score": 0.1, "signals": []})
    mock_client = _mock_async_client(response)

    with patch(
        "alphalive.services.alphasignal_client.httpx.AsyncClient",
        return_value=mock_client,
    ):
        await client.get_sentiment("AAPL")

    _, kwargs = mock_client.get.call_args
    assert "X-API-Key" not in kwargs["headers"]


# ---------------------------------------------------------------------------
# run_pre_execution_checks() - None-client passthrough and error handling
# ---------------------------------------------------------------------------


async def test_run_pre_execution_checks_none_client_passthrough():
    allowed, pred = await run_pre_execution_checks(
        alphasignal_client=None,
        ticker="AAPL",
        signal_direction=2,
    )

    assert allowed is True
    assert pred == {"gate_status": GATE_STATUS_DISABLED_BYPASS}


async def test_run_pre_execution_checks_exception_fails_open():
    mock_client = MagicMock()

    async def _raise(*args, **kwargs):
        raise RuntimeError("alphasignal down")

    mock_client.is_execution_allowed = MagicMock(side_effect=lambda *a, **k: _raise())

    allowed, pred = await run_pre_execution_checks(
        alphasignal_client=mock_client,
        ticker="AAPL",
        signal_direction=2,
    )

    assert allowed is True
    assert pred == {"gate_status": GATE_STATUS_ERROR_BYPASS}
