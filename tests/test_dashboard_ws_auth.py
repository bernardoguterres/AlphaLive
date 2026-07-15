"""Regression tests for audit bug 2.2 (2026-07-13): the dashboard's /ws
WebSocket endpoint bypassed HTTP Basic Auth entirely, because
@app.middleware("http") only wraps Starlette's "http" ASGI scope, never the
"websocket" scope - despite a code comment claiming coverage of "HTTP and
WebSocket upgrade". An unauthenticated client could stream full account
equity, cash, daily P&L, positions, and orders every 5 seconds.

ws_endpoint() now calls the shared _basic_auth_ok() check explicitly and
closes the connection (code 1008) before accept() if it fails.
"""

import base64
from unittest.mock import patch

import pytest
from starlette.websockets import WebSocketDisconnect

import dashboard.server as dashboard_server


def _basic_auth_header(password: str) -> str:
    token = base64.b64encode(f"anyuser:{password}".encode()).decode()
    return f"Basic {token}"


@pytest.fixture
def client_with_password(monkeypatch):
    monkeypatch.setattr(dashboard_server, "_PASSWORD", "correct-horse-battery-staple")
    from fastapi.testclient import TestClient

    return TestClient(dashboard_server.app)


@pytest.fixture
def client_no_password(monkeypatch):
    monkeypatch.setattr(dashboard_server, "_PASSWORD", "")
    from fastapi.testclient import TestClient

    return TestClient(dashboard_server.app)


class TestWebSocketAuthEnforced:
    def test_ws_rejects_connection_with_no_credentials(self, client_with_password):
        with pytest.raises(WebSocketDisconnect):
            with client_with_password.websocket_connect("/ws"):
                pass

    def test_ws_rejects_connection_with_wrong_password(self, client_with_password):
        headers = {"authorization": _basic_auth_header("wrong-password")}
        with pytest.raises(WebSocketDisconnect):
            with client_with_password.websocket_connect("/ws", headers=headers):
                pass

    def test_ws_rejects_malformed_authorization_header(self, client_with_password):
        headers = {"authorization": "Bearer some-token"}
        with pytest.raises(WebSocketDisconnect):
            with client_with_password.websocket_connect("/ws", headers=headers):
                pass

    def test_ws_accepts_connection_with_correct_credentials(self, client_with_password):
        headers = {"authorization": _basic_auth_header("correct-horse-battery-staple")}
        fake_payload = {"account": None, "positions": [], "orders": [], "risk": {}}
        with patch.object(
            dashboard_server, "_build_payload", return_value=fake_payload
        ):
            with client_with_password.websocket_connect("/ws", headers=headers) as ws:
                data = ws.receive_json()
                assert data == fake_payload

    def test_ws_allows_connection_when_no_password_configured(self, client_no_password):
        """DASHBOARD_PASSWORD unset = auth disabled entirely (documented
        no-op behavior) - must still work, this isn't a regression target."""
        fake_payload = {"account": None, "positions": [], "orders": [], "risk": {}}
        with patch.object(
            dashboard_server, "_build_payload", return_value=fake_payload
        ):
            with client_no_password.websocket_connect("/ws") as ws:
                data = ws.receive_json()
                assert data == fake_payload


class TestHttpAuthStillEnforced:
    """Sanity check that the refactor (extracting _basic_auth_ok) didn't
    regress the existing plain-HTTP auth path."""

    def test_http_route_rejects_missing_credentials(self, client_with_password):
        response = client_with_password.get("/api/health")
        assert response.status_code == 401

    def test_http_route_accepts_correct_credentials(self, client_with_password):
        headers = {"authorization": _basic_auth_header("correct-horse-battery-staple")}
        response = client_with_password.get("/api/health", headers=headers)
        assert response.status_code != 401
