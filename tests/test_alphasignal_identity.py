"""AlphaSignal identity check over real local HTTP: the configured URL must
identify as AlphaSignal; a different service answering 200 is a configuration
error (blocks), while an unreachable one is an outage (fail policy decides)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from alphalive.services.alphasignal_client import (
    GATE_STATUS_ERROR_BYPASS,
    GATE_STATUS_MISCONFIGURED_BLOCK,
    IDENTITY_UNREACHABLE,
    IDENTITY_VERIFIED,
    IDENTITY_WRONG_SERVICE,
    AlphaSignalClient,
)


def _server(status, body, ctype="application/json"):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(
                body if isinstance(body, bytes) else json.dumps(body).encode()
            )

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


ALPHASIGNAL = {"service": "alphasignal", "sentiment_contract": "2", "version": "0.1.0"}


@pytest.mark.parametrize(
    "status, body, expected",
    [
        (200, ALPHASIGNAL, IDENTITY_VERIFIED),
        (200, b"<html>other app</html>", IDENTITY_WRONG_SERVICE),
        (200, {"status": "healthy", "version": "1.0"}, IDENTITY_WRONG_SERVICE),
        (200, {**ALPHASIGNAL, "sentiment_contract": "9"}, IDENTITY_WRONG_SERVICE),
        (404, {"detail": "Not Found"}, IDENTITY_WRONG_SERVICE),
        (503, {"detail": "starting"}, IDENTITY_UNREACHABLE),
    ],
    ids=["alphasignal", "html-200", "generic-json-200", "bad-contract", "404", "503"],
)
async def test_identity_classification(status, body, expected):
    srv, url = _server(status, body)
    try:
        assert (await AlphaSignalClient(base_url=url).check_identity())[0] == expected
    finally:
        srv.shutdown()


async def test_unreachable_and_unset_urls():
    srv, url = _server(200, ALPHASIGNAL)
    srv.shutdown()
    srv.server_close()
    assert (await AlphaSignalClient(base_url=url, timeout_seconds=1).check_identity())[
        0
    ] == IDENTITY_UNREACHABLE
    assert (await AlphaSignalClient(base_url="").check_identity())[
        0
    ] == IDENTITY_WRONG_SERVICE


@pytest.mark.parametrize("policy", ["open", "closed"])
async def test_wrong_service_blocks_whatever_the_policy(policy):
    srv, url = _server(200, b"<html>other app</html>")
    try:
        c = AlphaSignalClient(base_url=url, fail_policy=policy, verify_identity=True)
        allowed, pred = await c.is_execution_allowed("MSFT", 2)
        assert allowed is False
        assert pred["gate_status"] == GATE_STATUS_MISCONFIGURED_BLOCK
    finally:
        srv.shutdown()


@pytest.mark.parametrize("policy, expected", [("open", True), ("closed", False)])
async def test_outage_keeps_the_fail_policy(policy, expected):
    srv, url = _server(200, ALPHASIGNAL)
    srv.shutdown()
    srv.server_close()
    c = AlphaSignalClient(
        base_url=url, fail_policy=policy, verify_identity=True, timeout_seconds=1
    )
    allowed, pred = await c.is_execution_allowed("MSFT", 2)
    assert allowed is expected
    assert pred["gate_status"] == GATE_STATUS_ERROR_BYPASS
