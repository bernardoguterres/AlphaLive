"""
HTTP transport settings shared by the Alpaca trading and market-data clients.

alpaca-py's REST client issues ``requests`` calls with NO timeout, so a
broker that accepts a connection but never answers would hang the trading
loop indefinitely. ``install_request_timeout`` mounts a default timeout on
the client's ``requests.Session`` (an alpaca-py private attribute - guarded,
and a no-op with a warning if the library layout changes). A timeout is
handled by the order path as an *ambiguous* outcome (the broker may have
accepted the order): the intent is quarantined and reconciled by
client_order_id, never blindly resubmitted under a new id.

Read-only mode (``ALPHALIVE_BROKER_READ_ONLY``, default on when ``DRY_RUN``
is true) makes the same adapter refuse every non-GET request - submit,
replace, cancel, close - before it leaves the process, independently of the
application's own dry-run checks, and logs each allowed request's method and
path (no query string, no headers).
"""

import logging
import os
import re
from collections import Counter
from urllib.parse import urlsplit

from requests.adapters import HTTPAdapter

from alphalive.utils.env_bool import read_bool_env

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0

READ_ONLY_ENV = "ALPHALIVE_BROKER_READ_ONLY"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_ID_SEGMENT = re.compile(r"/[0-9a-fA-F-]{32,36}(?=/|$)")

# (method, host, path with ids replaced) -> count, for this process
request_counts: Counter = Counter()
blocked_counts: Counter = Counter()


class BrokerMutationBlocked(RuntimeError):
    """A broker request that could change account state was refused by the
    read-only transport guard."""


def broker_read_only() -> bool:
    """Read-only unless explicitly disabled; defaults to the DRY_RUN flag."""
    dry_run = read_bool_env("DRY_RUN", default=False)
    return read_bool_env(READ_ONLY_ENV, default=dry_run)


def _request_key(request) -> tuple:
    parts = urlsplit(request.url)
    return (
        request.method.upper(),
        parts.hostname,
        _ID_SEGMENT.sub("/{id}", parts.path),
    )


def request_timeout_seconds() -> float:
    try:
        return float(
            os.environ.get("ALPACA_HTTP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
        )
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


class _TimeoutAdapter(HTTPAdapter):
    def __init__(self, timeout: float, *args, read_only: bool = False, **kwargs):
        self._timeout = timeout
        self._read_only = read_only
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        if self._read_only:
            key = _request_key(request)
            if key[0] not in _SAFE_METHODS:
                blocked_counts[key] += 1
                logger.error(
                    "BROKER MUTATION BLOCKED (read-only transport): %s %s%s",
                    *key,
                )
                raise BrokerMutationBlocked(
                    f"read-only broker transport refused {key[0]} {key[2]}"
                )
            request_counts[key] += 1
            logger.info("Broker request (read-only): %s %s%s", *key)
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self._timeout
        return super().send(request, **kwargs)


def install_request_timeout(
    client, timeout: float | None = None, read_only: bool | None = None
) -> bool:
    """Give an alpaca-py REST client a default per-request timeout, and the
    read-only guard when ``read_only`` (default: ``broker_read_only()``)."""
    timeout = timeout if timeout is not None else request_timeout_seconds()
    read_only = broker_read_only() if read_only is None else read_only
    session = getattr(client, "_session", None)
    if session is None:
        logger.warning(
            "alpaca-py client has no _session attribute - request timeout NOT "
            "installed; broker calls may block indefinitely."
        )
        if read_only:
            raise RuntimeError(
                "Broker read-only mode requested but the alpaca-py client has "
                "no _session to guard - refusing to continue."
            )
        return False
    adapter = _TimeoutAdapter(timeout, read_only=read_only)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    if read_only:
        logger.warning(
            "Broker transport is READ-ONLY: non-GET requests (submit, replace, "
            "cancel, close) are refused before they leave the process."
        )
    return True
