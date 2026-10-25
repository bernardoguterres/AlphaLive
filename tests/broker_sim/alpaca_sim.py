"""
Controllable Alpaca REST simulator (test infrastructure - never deployed).

Speaks the subset of Alpaca's trading + market-data wire format that
AlphaLive uses, so verification runs through the REAL adapter stack:
AlpacaBroker / MarketDataFetcher -> alpaca-py -> requests -> HTTP -> here.
Point AlphaLive at it with:

    ALPACA_BASE_URL=http://127.0.0.1:<port>   (trading: /v2/...)
    ALPACA_DATA_URL=http://127.0.0.1:<port>   (data:    /v2/stocks/...)

Unlike an instant-fill fake, a submitted order stays "accepted" until a
control call changes it. Control endpoints (JSON POST under /sim/):

    /sim/price          {"symbol", "price"}                set latest trade price
    /sim/bars           {"symbol", "bars": [{t,o,h,l,c,v}]} raw daily/intraday bars
    /sim/split          {"symbol", "ratio", "date"}        split: positions x ratio,
                                                           raw bars from `date` /ratio
    /sim/fill           {"client_order_id", "qty", "price"}  execute qty more shares
    /sim/status         {"client_order_id", "status"}      rejected/canceled/expired/new/accepted
    /sim/report         {"client_order_id", "filled_qty", "filled_avg_price", "status"}
                        one-shot override of the NEXT order report (stale /
                        out-of-order / duplicate fill reports); state unchanged
    /sim/fault          {"path", "method", "mode", "count", "seconds"}
                        mode: "delay" (sleep then answer), "error500",
                        "accept_then_drop" (POST /v2/orders: create the order,
                        then sleep `seconds` so the client times out)
    /sim/clock          {"is_open"}
    /sim/reset          {"cash"}
    GET /sim/state      orders, positions, executions, cash, request log
"""

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_ASSET_ID = "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415"


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


class SimState:
    def __init__(self, cash=100_000.0):
        self.lock = threading.RLock()
        self.reset(cash)

    def reset(self, cash=100_000.0):
        self.cash = float(cash)
        self.orders = {}  # id -> dict
        self.by_client = {}  # client_order_id -> id
        self.positions = {}  # symbol -> {"qty", "avg"}
        self.prices = {}
        self.bars = {}  # symbol -> list of raw bars (dicts with datetime t)
        self.splits = {}  # symbol -> list of (date, ratio)
        self.executions = []
        self.faults = []
        self.report_overrides = {}  # client_order_id -> dict
        self.is_open = True
        self.requests = []

    # ---- orders -------------------------------------------------------
    def create_order(self, body):
        coid = body.get("client_order_id") or uuid.uuid4().hex
        if coid in self.by_client:
            return None, (
                409,
                {"code": 40010001, "message": "client_order_id must be unique"},
            )
        qty = float(body["qty"])
        side = body["side"]
        sym = body["symbol"]
        if side == "sell":
            held = self.positions.get(sym, {}).get("qty", 0.0)
            if qty > held + 1e-9:
                return None, (
                    403,
                    {
                        "code": 40310000,
                        "message": "insufficient qty available for order",
                    },
                )
        else:
            px = self.prices.get(sym)
            if px is not None and qty * px > self.cash + 1e-6:
                return None, (
                    403,
                    {"code": 40310000, "message": "insufficient buying power"},
                )
        now = _now()
        oid = str(uuid.uuid4())
        order = {
            "id": oid,
            "client_order_id": coid,
            "created_at": now,
            "updated_at": now,
            "submitted_at": now,
            "filled_at": None,
            "symbol": sym,
            "qty": qty,
            "filled_qty": 0.0,
            "filled_value": 0.0,
            "side": side,
            "type": body.get("type", "market"),
            "limit_price": body.get("limit_price"),
            "time_in_force": body.get("time_in_force", "day"),
            "status": "accepted",
        }
        self.orders[oid] = order
        self.by_client[coid] = oid
        return order, None

    def fill(self, coid, qty, price):
        o = self.orders[self.by_client[coid]]
        qty = min(float(qty), o["qty"] - o["filled_qty"])
        if qty <= 0 or o["status"] in ("filled", "canceled", "rejected", "expired"):
            return o
        o["filled_qty"] += qty
        o["filled_value"] += qty * float(price)
        o["status"] = (
            "filled" if o["filled_qty"] >= o["qty"] - 1e-9 else "partially_filled"
        )
        o["updated_at"] = _now()
        if o["status"] == "filled":
            o["filled_at"] = o["updated_at"]
        sym = o["symbol"]
        pos = self.positions.setdefault(sym, {"qty": 0.0, "avg": 0.0})
        if o["side"] == "buy":
            new_qty = pos["qty"] + qty
            pos["avg"] = (pos["qty"] * pos["avg"] + qty * float(price)) / new_qty
            pos["qty"] = new_qty
            self.cash -= qty * float(price)
        else:
            pos["qty"] -= qty
            self.cash += qty * float(price)
            if pos["qty"] <= 1e-9:
                del self.positions[sym]
        self.executions.append(
            {
                "client_order_id": coid,
                "side": o["side"],
                "qty": qty,
                "price": float(price),
                "at": _iso(_now()),
            }
        )
        return o

    def order_json(self, o):
        coid = o["client_order_id"]
        ov = self.report_overrides.pop(coid, None)
        filled_qty = o["filled_qty"]
        avg = (o["filled_value"] / filled_qty) if filled_qty > 0 else None
        status = o["status"]
        if ov:
            filled_qty = float(ov.get("filled_qty", filled_qty))
            avg = ov.get("filled_avg_price", avg)
            status = ov.get("status", status)
        return {
            "id": o["id"],
            "client_order_id": coid,
            "created_at": _iso(o["created_at"]),
            "updated_at": _iso(o["updated_at"]),
            "submitted_at": _iso(o["submitted_at"]),
            "filled_at": _iso(o["filled_at"]) if o["filled_at"] else None,
            "expired_at": None,
            "canceled_at": None,
            "failed_at": None,
            "asset_id": _ASSET_ID,
            "symbol": o["symbol"],
            "asset_class": "us_equity",
            "qty": f"{o['qty']:g}",
            "filled_qty": f"{filled_qty:g}",
            "filled_avg_price": (f"{avg:.6f}" if avg else None),
            "order_class": "simple",
            "order_type": o["type"],
            "type": o["type"],
            "side": o["side"],
            "time_in_force": o["time_in_force"],
            "limit_price": o["limit_price"],
            "status": status,
            "extended_hours": False,
        }

    def position_json(self, sym):
        p = self.positions[sym]
        px = self.prices.get(sym, p["avg"])
        mv = p["qty"] * px
        cost = p["qty"] * p["avg"]
        return {
            "asset_id": _ASSET_ID,
            "symbol": sym,
            "exchange": "NASDAQ",
            "asset_class": "us_equity",
            "avg_entry_price": f"{p['avg']:.6f}",
            "qty": f"{p['qty']:g}",
            "qty_available": f"{p['qty']:g}",
            "side": "long",
            "market_value": f"{mv:.4f}",
            "cost_basis": f"{cost:.4f}",
            "unrealized_pl": f"{mv - cost:.4f}",
            "unrealized_plpc": f"{(mv - cost) / cost if cost else 0:.6f}",
            "unrealized_intraday_pl": "0",
            "unrealized_intraday_plpc": "0",
            "current_price": f"{px:.4f}",
            "lastday_price": f"{px:.4f}",
            "change_today": "0",
        }

    def account_json(self):
        lmv = sum(
            p["qty"] * self.prices.get(s, p["avg"]) for s, p in self.positions.items()
        )
        eq = self.cash + lmv
        return {
            "id": "00000000-0000-0000-0000-000000000001",
            "account_number": "SIMULATED",
            "status": "ACTIVE",
            "currency": "USD",
            "cash": f"{self.cash:.2f}",
            "equity": f"{eq:.2f}",
            "portfolio_value": f"{eq:.2f}",
            "buying_power": f"{self.cash:.2f}",
            "long_market_value": f"{lmv:.2f}",
            "short_market_value": "0",
            "daytrade_count": 0,
            "pattern_day_trader": False,
        }

    # ---- data ---------------------------------------------------------
    def bars_json(self, sym, adjustment, start, end):
        out = []
        for b in self.bars.get(sym, []):
            t = b["t"]
            if start and t < start:
                continue
            if end and t > end:
                continue
            factor = 1.0
            if adjustment in ("split", "all"):
                for d, r in self.splits.get(sym, []):
                    if t < d:
                        factor *= r
            out.append(
                {
                    "t": _iso(t),
                    "o": b["o"] / factor,
                    "h": b["h"] / factor,
                    "l": b["l"] / factor,
                    "c": b["c"] / factor,
                    "v": b["v"] * factor,
                    "n": 1,
                    "vw": b["c"] / factor,
                }
            )
        return out


def _parse_dt(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.fromisoformat(s[:19])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def make_handler(state: SimState):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code, payload):
            body = json.dumps(payload).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _fault(self, method, path):
            with state.lock:
                for f in state.faults:
                    if (
                        f["count"] > 0
                        and f["method"] == method
                        and path.startswith(f["path"])
                    ):
                        f["count"] -= 1
                        return dict(f)
            return None

        def _route(self, method):
            u = urlparse(self.path)
            path, q = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
            with state.lock:
                state.requests.append(
                    {"method": method, "path": path, "at": _iso(_now())}
                )
            body = self._body() if method in ("POST", "PATCH") else {}

            if path.startswith("/sim/"):
                return self._control(method, path, body)

            fault = self._fault(method, path)
            if fault and fault["mode"] == "delay":
                time.sleep(fault["seconds"])
            if fault and fault["mode"] == "error500":
                return self._send(
                    500, {"code": 50000000, "message": "simulated internal error"}
                )

            with state.lock:
                if path == "/v2/account":
                    return self._send(200, state.account_json())
                if path == "/v2/clock":
                    now = _now()
                    return self._send(
                        200,
                        {
                            "timestamp": _iso(now),
                            "is_open": state.is_open,
                            "next_open": _iso(now + timedelta(hours=16)),
                            "next_close": _iso(now + timedelta(hours=6)),
                        },
                    )
                if path == "/v2/positions" and method == "GET":
                    return self._send(
                        200, [state.position_json(s) for s in state.positions]
                    )
                if path.startswith("/v2/positions/") and method == "GET":
                    sym = path.rsplit("/", 1)[1]
                    if sym not in state.positions:
                        return self._send(
                            404,
                            {"code": 40410000, "message": "position does not exist"},
                        )
                    return self._send(200, state.position_json(sym))
                if path == "/v2/orders" and method == "POST":
                    order, err = state.create_order(body)
                    if err:
                        return self._send(*err)
                    payload = state.order_json(order)
                if path == "/v2/orders" and method == "GET":
                    return self._send(
                        200, [state.order_json(o) for o in state.orders.values()]
                    )
                if path == "/v2/orders:by_client_order_id":
                    oid = state.by_client.get(q.get("client_order_id"))
                    if oid is None:
                        return self._send(
                            404, {"code": 40410000, "message": "order not found"}
                        )
                    return self._send(200, state.order_json(state.orders[oid]))
                if path.startswith("/v2/orders/") and method == "GET":
                    o = state.orders.get(path.rsplit("/", 1)[1])
                    if o is None:
                        return self._send(
                            404, {"code": 40410000, "message": "order not found"}
                        )
                    return self._send(200, state.order_json(o))
                if path.startswith("/v2/orders/") and method == "DELETE":
                    o = state.orders.get(path.rsplit("/", 1)[1])
                    if o is None:
                        return self._send(
                            404, {"code": 40410000, "message": "order not found"}
                        )
                    if o["status"] in ("accepted", "new", "partially_filled"):
                        o["status"] = "canceled"
                    return self._send(204, {})
                if path.startswith("/v2/stocks/") and path.endswith("/trades/latest"):
                    sym = path.split("/")[3]
                    px = state.prices.get(sym)
                    if px is None:
                        return self._send(404, {"message": "no trade"})
                    return self._send(
                        200,
                        {
                            "symbol": sym,
                            "trade": {
                                "t": _iso(_now()),
                                "x": "V",
                                "p": px,
                                "s": 100,
                                "c": ["@"],
                                "i": 1,
                                "z": "C",
                            },
                        },
                    )
                if path.startswith("/v2/stocks/") and path.endswith("/bars"):
                    sym = path.split("/")[3]
                    bars = state.bars_json(
                        sym,
                        q.get("adjustment", "raw"),
                        _parse_dt(q.get("start")),
                        _parse_dt(q.get("end")),
                    )
                    return self._send(
                        200, {"bars": bars, "symbol": sym, "next_page_token": None}
                    )
                if path == "/v2/stocks/bars":
                    sym = q.get("symbols")
                    bars = state.bars_json(
                        sym,
                        q.get("adjustment", "raw"),
                        _parse_dt(q.get("start")),
                        _parse_dt(q.get("end")),
                    )
                    return self._send(
                        200,
                        {"bars": {sym: bars} if bars else {}, "next_page_token": None},
                    )
                if path == "/v2/stocks/trades/latest":
                    sym = q.get("symbols")
                    px = state.prices.get(sym)
                    if px is None:
                        return self._send(404, {"message": "no trade"})
                    return self._send(
                        200,
                        {
                            "trades": {
                                sym: {
                                    "t": _iso(_now()),
                                    "x": "V",
                                    "p": px,
                                    "s": 100,
                                    "c": ["@"],
                                    "i": 1,
                                    "z": "C",
                                }
                            }
                        },
                    )
            if path == "/v2/orders" and method == "POST":
                if fault and fault["mode"] == "accept_then_drop":
                    time.sleep(fault["seconds"])  # client times out; order exists
                return self._send(200, payload)
            return self._send(404, {"message": f"sim: no route {method} {path}"})

        def _control(self, method, path, b):
            with state.lock:
                if path == "/sim/state":
                    return self._send(
                        200,
                        {
                            "cash": state.cash,
                            "positions": state.positions,
                            "orders": [
                                state.order_json(o) for o in state.orders.values()
                            ],
                            "executions": state.executions,
                            "requests": state.requests[-200:],
                        },
                    )
                if path == "/sim/price":
                    state.prices[b["symbol"]] = float(b["price"])
                elif path == "/sim/bars":
                    state.bars[b["symbol"]] = [
                        {**x, "t": _parse_dt(x["t"])} for x in b["bars"]
                    ]
                elif path == "/sim/split":
                    r = float(b["ratio"])
                    d = _parse_dt(b["date"])
                    state.splits.setdefault(b["symbol"], []).append((d, r))
                    pos = state.positions.get(b["symbol"])
                    if pos:
                        pos["qty"] *= r
                        pos["avg"] /= r
                    # raw bars from the split date onward are in post-split units
                    for bar in state.bars.get(b["symbol"], []):
                        if bar["t"] >= d:
                            for k in ("o", "h", "l", "c"):
                                bar[k] /= r
                            bar["v"] *= r
                    if b["symbol"] in state.prices:
                        state.prices[b["symbol"]] /= r
                elif path == "/sim/fill":
                    state.fill(b["client_order_id"], b["qty"], b["price"])
                elif path == "/sim/status":
                    o = state.orders[state.by_client[b["client_order_id"]]]
                    o["status"] = b["status"]
                    o["updated_at"] = _now()
                elif path == "/sim/report":
                    state.report_overrides[b["client_order_id"]] = b
                elif path == "/sim/fault":
                    state.faults.append(
                        {
                            "path": b["path"],
                            "method": b.get("method", "POST"),
                            "mode": b["mode"],
                            "count": int(b.get("count", 1)),
                            "seconds": float(b.get("seconds", 0)),
                        }
                    )
                elif path == "/sim/clock":
                    state.is_open = bool(b["is_open"])
                elif path == "/sim/reset":
                    state.reset(b.get("cash", 100_000.0))
                else:
                    return self._send(404, {"message": "unknown control"})
            return self._send(200, {"ok": True})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_DELETE(self):
            self._route("DELETE")

    return H


class AlpacaSim:
    """In-process server: `with AlpacaSim() as sim: sim.url ...`."""

    def __init__(self, port=0, cash=100_000.0):
        self.state = SimState(cash)
        self.server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(self.state))
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._t = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()

    # convenience wrappers used by tests
    def order(self, coid):
        with self.state.lock:
            return self.state.orders[self.state.by_client[coid]]

    def client_ids(self):
        with self.state.lock:
            return list(self.state.by_client)


def main():
    import argparse

    ap = argparse.ArgumentParser(
        description="Alpaca REST simulator (local testing only)"
    )
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--cash", type=float, default=100_000.0)
    a = ap.parse_args()
    sim = AlpacaSim(a.port, a.cash)
    print(f"Alpaca simulator listening on {sim.url}", flush=True)
    sim.server.serve_forever()


if __name__ == "__main__":
    main()
