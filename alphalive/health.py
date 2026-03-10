"""
Health Check Endpoint

Minimal HTTP server for Railway healthcheck monitoring.
Runs on daemon thread to not block main trading loop.
"""

import os
import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


class HealthCheckHandler(BaseHTTPRequestHandler):
    """
    HTTP request handler for health checks.

    Authentication: Requires X-Health-Secret header matching HEALTH_SECRET env var.
    If HEALTH_SECRET not set, endpoint is disabled (returns 503).
    """

    # Class variables set by HealthServer
    health_data = {}
    start_time = None
    secret = None

    def log_message(self, format, *args):
        """Override to use our logger instead of printing to stderr."""
        logger.debug(f"Health check: {format % args}")

    def do_GET(self):
        """Handle GET requests to / endpoint."""
        if self.path != "/":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error": "Not found"}')
            return

        # Check if health endpoint is enabled
        if not self.secret:
            logger.debug("Health check disabled: HEALTH_SECRET not set")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Health endpoint disabled"}')
            return

        # Verify authentication
        request_secret = self.headers.get("X-Health-Secret")
        if request_secret != self.secret:
            logger.warning(
                f"Health check unauthorized: wrong secret from {self.client_address[0]}"
            )
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Unauthorized"}')
            return

        # Calculate uptime
        if self.start_time:
            uptime_seconds = (datetime.now() - self.start_time).total_seconds()
            hours = int(uptime_seconds // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            uptime = f"{hours}h {minutes}m"
        else:
            uptime = "unknown"

        # Build response payload
        payload = {
            "status": "ok",
            "uptime": uptime,
            "last_check": datetime.now(ET).isoformat(),
            **self.health_data
        }

        # Send response
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode('utf-8'))

        logger.debug(f"Health check successful from {self.client_address[0]}")


class HealthServer:
    """
    Health check HTTP server running on daemon thread.

    Usage:
        health = HealthServer(
            port=8080,
            health_data={
                "warmup_complete": True,
                "bars_loaded": 252,
                "trading_paused": False,
                "dry_run": False,
                "paper": True
            }
        )
        health.start()
    """

    def __init__(self, port: int = 8080, health_data: dict = None):
        """
        Initialize health server.

        Args:
            port: Port to listen on (default 8080)
            health_data: Dictionary with health status data
        """
        self.port = port
        self.secret = os.environ.get("HEALTH_SECRET")
        self.health_data = health_data or {}
        self.start_time = datetime.now()
        self.server = None
        self.thread = None

        # Set class variables for handler
        HealthCheckHandler.health_data = self.health_data
        HealthCheckHandler.start_time = self.start_time
        HealthCheckHandler.secret = self.secret

        if not self.secret:
            logger.warning(
                "Health endpoint disabled: HEALTH_SECRET env var not set. "
                "To enable, set HEALTH_SECRET=<random_string>"
            )
        else:
            logger.info(f"Health endpoint enabled on port {self.port}")

    def start(self):
        """Start health check server on daemon thread."""
        try:
            self.server = HTTPServer(("0.0.0.0", self.port), HealthCheckHandler)

            # Start server in daemon thread (won't block main loop)
            self.thread = threading.Thread(
                target=self.server.serve_forever,
                daemon=True,
                name="HealthCheckServer"
            )
            self.thread.start()

            if self.secret:
                logger.info(f"Health check server listening on port {self.port}")
            else:
                logger.info(
                    f"Health check server listening on port {self.port} "
                    f"(disabled - no HEALTH_SECRET)"
                )

        except Exception as e:
            logger.error(f"Failed to start health check server: {e}")

    def update_health_data(self, data: dict):
        """
        Update health data dynamically.

        Args:
            data: Dictionary with new health data
        """
        self.health_data.update(data)
        HealthCheckHandler.health_data = self.health_data

    def stop(self):
        """Stop health check server."""
        if self.server:
            self.server.shutdown()
            logger.info("Health check server stopped")


def create_health_server(config, dry_run: bool = False, paper: bool = True) -> HealthServer:
    """
    Create and start health check server.

    Args:
        config: Strategy configuration
        dry_run: Whether running in dry run mode
        paper: Whether using paper trading

    Returns:
        HealthServer instance
    """
    port = int(os.environ.get("HEALTH_PORT", "8080"))

    health_data = {
        "warmup_complete": True,  # Updated after first signal check
        "bars_loaded": 0,         # Updated after market data fetch
        "trading_paused": os.environ.get("TRADING_PAUSED", "false").lower() == "true",
        "dry_run": dry_run,
        "paper": paper,
        "strategy": config.strategy.name,
        "ticker": config.ticker,
        "timeframe": config.timeframe
    }

    health = HealthServer(port=port, health_data=health_data)
    health.start()

    return health
