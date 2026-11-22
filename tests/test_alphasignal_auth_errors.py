"""AlphaSignal HTTP error classification at the sentiment gate.

A rejected API key (401/403) used to be treated as an outage (error_bypass),
so under the default fail_policy="open" every BUY went through with the gate
silently off - even on a -0.9 score. It is a configuration error and now
blocks entries whatever the policy. A 404 (ticker not in AlphaSignal's
knowledge base) can't be evaluated: it's labelled no_data and the fail
policy decides.
"""

from unittest.mock import patch

import pytest

from alphalive.services.alphasignal_client import (
    GATE_STATUS_ERROR_BYPASS,
    GATE_STATUS_MISCONFIGURED_BLOCK,
    GATE_STATUS_NO_DATA_BYPASS,
    AlphaSignalClient,
)
from tests.test_alphasignal_client_http import _mock_async_client, _mock_response

BUY = 2


async def _gate(status_code, policy):
    client = AlphaSignalClient(
        base_url="http://alphasignal.test", api_key="k", fail_policy=policy
    )
    mock = _mock_async_client(_mock_response({"detail": "x"}, status_code=status_code))
    with patch(
        "alphalive.services.alphasignal_client.httpx.AsyncClient", return_value=mock
    ):
        return await client.is_execution_allowed("AAPL", BUY)


@pytest.mark.parametrize("code", [401, 403])
@pytest.mark.parametrize("policy", ["open", "closed"])
async def test_rejected_key_blocks_entry_whatever_the_policy(code, policy):
    allowed, pred = await _gate(code, policy)
    assert allowed is False
    assert pred["gate_status"] == GATE_STATUS_MISCONFIGURED_BLOCK
    assert pred["http_status"] == code


@pytest.mark.parametrize("policy,expected", [("open", True), ("closed", False)])
async def test_uncovered_ticker_is_no_data_and_follows_policy(policy, expected):
    allowed, pred = await _gate(404, policy)
    assert allowed is expected
    assert pred["gate_status"] == GATE_STATUS_NO_DATA_BYPASS
    assert pred["http_status"] == 404


@pytest.mark.parametrize("policy,expected", [("open", True), ("closed", False)])
async def test_server_error_is_still_an_outage(policy, expected):
    allowed, pred = await _gate(503, policy)
    assert allowed is expected
    assert pred["gate_status"] == GATE_STATUS_ERROR_BYPASS
