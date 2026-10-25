"""Fail-closed instrumentation: any credential read, .env load, Alpaca client or
network connection raises AND is recorded in $GUARD_LOG."""

import os
import socket

import dotenv
import dotenv.main

LOG = os.environ.get("GUARD_LOG", "/dev/null")


def _hit(what):
    with open(LOG, "a") as f:
        f.write(what + "\n")
    raise RuntimeError(f"GUARD: {what} attempted")


_SECRET = {"ALPACA_API_KEY", "ALPACA_SECRET_KEY"}
_orig_getitem = os._Environ.__getitem__


def _getitem(self, key):
    if key in _SECRET:
        _hit(f"credential read {key}")
    return _orig_getitem(self, key)


os._Environ.__getitem__ = _getitem
for name in ("connect", "connect_ex"):
    setattr(
        socket.socket, name, lambda self, *a, _n=name, **k: _hit(f"socket.{_n} {a[:1]}")
    )
socket.create_connection = lambda *a, **k: _hit(f"create_connection {a[:1]}")
socket.getaddrinfo = lambda *a, **k: _hit(f"getaddrinfo {a[:1]}")

for mod in (dotenv, dotenv.main):
    mod.load_dotenv = lambda *a, **k: _hit("dotenv.load_dotenv")
    mod.dotenv_values = lambda *a, **k: _hit("dotenv.dotenv_values")
try:
    import alpaca.common.rest as rest

    rest.RESTClient.__init__ = lambda self, *a, **k: _hit(
        "Alpaca RESTClient constructed"
    )
except Exception:
    pass
