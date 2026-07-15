"""
AlphaLive Dashboard Server v2

New in v2:
- WebSocket /ws endpoint (pushes combined payload every 5s)
- GET /api/bars/{ticker} - last N daily bars for position sparklines
- HTTP Basic Auth via DASHBOARD_PASSWORD env var (no-op when unset)

Usage (from project root):
    uvicorn dashboard.server:app --host 0.0.0.0 --port 8888 --reload

Required env vars:  ALPACA_API_KEY, ALPACA_SECRET_KEY
Optional env vars:  ALPACA_PAPER (default true), STATE_FILE,
                    STRATEGY_CONFIG or STRATEGY_CONFIG_DIR,
                    DASHBOARD_PASSWORD (enables HTTP Basic Auth),
                    DASHBOARD_DEV_RELOAD (default false - dev-only browser
                    auto-refresh when dashboard/*.html changes on disk; see
                    "Dev live-reload" section below)
"""

import asyncio
import base64
import json
import logging
import os
import secrets
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response

try:
    from dotenv import load_dotenv

    if Path(".env").exists():
        load_dotenv()
except ImportError:
    pass

from alphalive.broker.alpaca_broker import AlpacaBroker
from alphalive.utils.env_bool import read_bool_env

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="AlphaLive Dashboard", version="2.0.0")
DASHBOARD_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# HTTP Basic Auth
# Applied to every plain HTTP request via the middleware below when
# DASHBOARD_PASSWORD is set. Starlette's `@app.middleware("http")` ONLY
# wraps the ASGI "http" scope - it structurally does not run for the
# "websocket" scope used by @app.websocket("/ws") below, regardless of what
# a browser's Authorization header happens to contain during the upgrade
# handshake (audit 2026-07-13 bug 2.2: a code comment here previously
# claimed this middleware covered "HTTP and WebSocket upgrade" - it never
# did, and an unauthenticated client could stream full account/position/P&L
# data from /ws even with DASHBOARD_PASSWORD set). The websocket handler
# below now checks auth explicitly, before accepting the connection.
# ---------------------------------------------------------------------------

_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")


def _basic_auth_ok(headers) -> bool:
    """Shared Basic Auth check for both plain HTTP requests and the
    websocket handshake. `headers` is any mapping with a case-insensitive
    .get() - both Starlette's Request.headers and WebSocket.headers satisfy
    this. Returns True when no password is configured (auth disabled)."""
    if not _PASSWORD:
        return True
    auth = headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8", errors="replace")
        _, _, given = decoded.partition(":")
        return secrets.compare_digest(given.encode(), _PASSWORD.encode())
    except Exception:
        return False


@app.middleware("http")
async def _basic_auth(request: Request, call_next):
    if not _basic_auth_ok(request.headers):
        return Response(
            content="Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="AlphaLive Dashboard"'},
        )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Broker singleton
# ---------------------------------------------------------------------------

_broker: Optional[AlpacaBroker] = None
_broker_error: Optional[str] = None


class _BrokerUnavailable(Exception):
    pass


def _get_broker() -> AlpacaBroker:
    """Get or reconnect broker. Raises _BrokerUnavailable on failure."""
    global _broker, _broker_error

    if _broker is not None and _broker.connected:
        return _broker

    api_key = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")

    if not api_key or not secret_key:
        _broker_error = "ALPACA_API_KEY or ALPACA_SECRET_KEY not set"
        raise _BrokerUnavailable(_broker_error)

    # Same shared parser as the bot's own ALPACA_PAPER read (audit bug 2.1) -
    # this dashboard-side read was a 4th ad-hoc implementation the audit's
    # location list missed; a malformed value here would only mislabel the
    # dashboard's own broker connection (paper vs live display), not flip
    # real trading, but it deserves the same fail-loud-on-garbage treatment
    # for consistency and to avoid ever silently misreporting live as paper.
    try:
        paper = read_bool_env("ALPACA_PAPER", default=True)
    except ValueError as exc:
        _broker_error = str(exc)
        raise _BrokerUnavailable(_broker_error) from exc

    try:
        broker = AlpacaBroker(api_key=api_key, secret_key=secret_key, paper=paper)
        broker.connect()
        _broker = broker
        _broker_error = None
        logger.info("Dashboard broker connected")
        return _broker
    except Exception as exc:
        _broker = None
        _broker_error = str(exc)
        raise _BrokerUnavailable(str(exc))


def _require_broker() -> AlpacaBroker:
    """HTTP-route wrapper - raises HTTPException on broker failure."""
    try:
        return _get_broker()
    except _BrokerUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

_DEFAULT_STATE: Dict[str, Any] = {
    "last_morning_check_date": None,
    "last_eod_summary_date": None,
    "daily_pnl": 0.0,
    "trades_today": [],
    "position_highs": {},
    "entry_timestamps": {},
    "last_startup": None,
    "last_saved": None,
    "version": "1.0",
}


def _read_state() -> Dict[str, Any]:
    path = os.getenv("STATE_FILE", "/tmp/alphalive_state.json")
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return dict(_DEFAULT_STATE)
    except Exception as exc:
        logger.warning(f"Could not read state file: {exc}")
        return dict(_DEFAULT_STATE)


# ---------------------------------------------------------------------------
# Strategy config (optional)
# ---------------------------------------------------------------------------

_strategies: Optional[List] = None


def _load_strategies() -> List:
    global _strategies
    if _strategies is not None:
        return _strategies
    config_path = os.getenv("STRATEGY_CONFIG") or os.getenv("STRATEGY_CONFIG_DIR")
    if not config_path:
        _strategies = []
        return _strategies
    try:
        from alphalive.config import load_config_path

        _strategies = load_config_path(config_path)
        logger.info(f"Loaded {len(_strategies)} strategies for dashboard")
    except Exception as exc:
        logger.warning(f"Strategy config unavailable: {exc}")
        _strategies = []
    return _strategies


def _trailing_stop_pct(ticker: str) -> Optional[float]:
    for s in _load_strategies():
        if s.ticker.upper() == ticker.upper() and s.risk.trailing_stop_enabled:
            return s.risk.trailing_stop_pct
    return None


def _max_daily_loss_pct() -> Optional[float]:
    strategies = _load_strategies()
    return strategies[0].risk.max_daily_loss_pct if strategies else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dt_str(val) -> Optional[str]:
    if val is None:
        return None
    return val.isoformat() if hasattr(val, "isoformat") else str(val)


def _enum_val(val) -> str:
    return val.value if hasattr(val, "value") else str(val)


# ---------------------------------------------------------------------------
# Payload builder (synchronous - called via run_in_executor from WS handler)
# ---------------------------------------------------------------------------


def _build_payload() -> Dict[str, Any]:
    """
    Gather account / positions / orders / risk / health into one dict.
    Fully synchronous; never raises - all errors go into error fields.
    """
    out: Dict[str, Any] = {}
    state = _read_state()

    # ---- account ----
    try:
        a = _get_broker().get_account()
        out["account"] = {
            "equity": a.equity,
            "cash": a.cash,
            "buying_power": a.buying_power,
            "portfolio_value": a.portfolio_value,
            "long_market_value": a.long_market_value,
            "short_market_value": a.short_market_value,
            "daytrade_count": a.daytrade_count,
            "pattern_day_trader": a.pattern_day_trader,
            "account_status": _enum_val(a.account_status),
            "paper": read_bool_env("ALPACA_PAPER", default=True),
        }
    except Exception as exc:
        out["account"] = None
        out["account_error"] = str(exc)

    # ---- positions ----
    try:
        position_highs: Dict[str, float] = state.get("position_highs", {})
        entry_timestamps: Dict[str, str] = state.get("entry_timestamps", {})
        raw = _get_broker().get_all_positions()
        positions = []
        for p in raw:
            ts_pct = _trailing_stop_pct(p.symbol)
            peak = position_highs.get(p.symbol)
            trail_level = (peak * (1 - ts_pct / 100)) if (ts_pct and peak) else None
            positions.append(
                {
                    "symbol": p.symbol,
                    "qty": p.qty,
                    "side": p.side,
                    "avg_entry_price": p.avg_entry_price,
                    "current_price": p.current_price,
                    "unrealized_pl": p.unrealized_pl,
                    "unrealized_plpc": p.unrealized_plpc,
                    "market_value": p.market_value,
                    "peak_price": peak,
                    "trailing_stop_pct": ts_pct,
                    "trailing_stop_level": trail_level,
                    "entry_timestamp": entry_timestamps.get(p.symbol),
                }
            )
        out["positions"] = positions
    except Exception as exc:
        out["positions"] = []
        out["positions_error"] = str(exc)

    # ---- orders ----
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        today = date.today()
        since = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, after=since, limit=50)
        raw_orders = _get_broker()._retry_with_backoff(
            _get_broker().trading_client.get_orders, req
        )
        orders = []
        for o in raw_orders:
            orders.append(
                {
                    "id": str(o.id),
                    "symbol": o.symbol,
                    "side": _enum_val(o.side),
                    "qty": float(o.qty) if o.qty else 0.0,
                    "filled_qty": float(o.filled_qty) if o.filled_qty else 0.0,
                    "filled_avg_price": (
                        float(o.filled_avg_price) if o.filled_avg_price else None
                    ),
                    "order_type": _enum_val(o.order_type),
                    "status": _enum_val(o.status),
                    "submitted_at": _dt_str(o.submitted_at),
                    "filled_at": _dt_str(o.filled_at),
                }
            )
        out["orders"] = orders
    except Exception as exc:
        out["orders"] = []
        out["orders_error"] = str(exc)

    # ---- risk ----
    try:
        daily_pnl = float(state.get("daily_pnl", 0.0))
        equity = (out.get("account") or {}).get("equity")
        loss_limit_pct = _max_daily_loss_pct()
        loss_limit_dollars = loss_used_pct = None
        if loss_limit_pct and equity:
            loss_limit_dollars = equity * (loss_limit_pct / 100)
            loss_used_pct = (
                min(abs(daily_pnl) / loss_limit_dollars * 100, 100.0)
                if daily_pnl < 0 and loss_limit_dollars > 0
                else 0.0
            )
        env_paused = read_bool_env("TRADING_PAUSED", default=False)
        # dashboard_paused has its own dedicated pause file (audit bug 2.5) -
        # no longer read from the main state dict, which is only ever
        # written by the bot and would otherwise show a stale value here.
        from alphalive.state import BotState

        dash_paused = BotState(
            os.getenv("STATE_FILE", "/tmp/alphalive_state.json")
        ).check_dashboard_paused()
        out["risk"] = {
            "daily_pnl": daily_pnl,
            "trading_paused": env_paused or dash_paused,
            "trading_paused_env": env_paused,
            "trading_paused_dashboard": dash_paused,
            "dry_run": read_bool_env("DRY_RUN", default=False),
            "equity": equity,
            "daily_loss_limit_pct": loss_limit_pct,
            "daily_loss_limit_dollars": loss_limit_dollars,
            "daily_loss_used_pct": loss_used_pct,
        }
    except Exception as exc:
        out["risk"] = None
        out["risk_error"] = str(exc)

    # ---- health ----
    broker_ok = False
    broker_err = _broker_error
    market = None
    try:
        raw = _get_broker().get_market_hours()
        broker_ok = True
        market = {
            "is_open": bool(raw["is_open"]),
            "next_open": _dt_str(raw.get("next_open")),
            "next_close": _dt_str(raw.get("next_close")),
            "timestamp": _dt_str(raw.get("timestamp")),
        }
    except Exception as exc:
        broker_err = str(exc)

    out["health"] = {
        "last_startup": state.get("last_startup"),
        "last_morning_check_date": state.get("last_morning_check_date"),
        "last_eod_summary_date": state.get("last_eod_summary_date"),
        "last_saved": state.get("last_saved"),
        "market": market,
        "broker_connected": broker_ok,
        "broker_error": broker_err,
        "paper_trading": os.getenv("ALPACA_PAPER", "true").lower()
        in ("true", "1", "yes"),
        "server_time": datetime.now().isoformat(),
        "state_file": os.getenv("STATE_FILE", "/tmp/alphalive_state.json"),
        "railway_configured": bool(
            os.getenv("RAILWAY_API_TOKEN")
            and os.getenv("RAILWAY_SERVICE_ID")
            and os.getenv("RAILWAY_ENVIRONMENT_ID")
        ),
    }

    return out


# ---------------------------------------------------------------------------
# WebSocket endpoint - pushes full payload every 5 seconds
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    if not _basic_auth_ok(websocket.headers):
        # Reject before accept() - the connection is never upgraded, so no
        # data is ever streamed to an unauthenticated client. Code 1008
        # (Policy Violation) is the standard WebSocket close code for this.
        await websocket.close(code=1008)
        logger.warning(
            f"WebSocket connection rejected (auth failed): {websocket.client}"
        )
        return

    await websocket.accept()
    logger.info(f"WebSocket client connected: {websocket.client}")
    loop = asyncio.get_event_loop()
    try:
        while True:
            payload = await loop.run_in_executor(None, _build_payload)
            await websocket.send_json(payload)
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        logger.info(f"WebSocket client disconnected: {websocket.client}")
    except Exception as exc:
        logger.warning(f"WebSocket error: {exc}")
        try:
            await websocket.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# REST endpoints (kept for direct API use / backward compat)
# ---------------------------------------------------------------------------


@app.get("/api/account")
async def api_account():
    """Account equity, cash, buying_power, portfolio_value."""
    broker = _require_broker()
    try:
        a = broker.get_account()
        return {
            "equity": a.equity,
            "cash": a.cash,
            "buying_power": a.buying_power,
            "portfolio_value": a.portfolio_value,
            "long_market_value": a.long_market_value,
            "short_market_value": a.short_market_value,
            "daytrade_count": a.daytrade_count,
            "pattern_day_trader": a.pattern_day_trader,
            "account_status": _enum_val(a.account_status),
            "paper": read_bool_env("ALPACA_PAPER", default=True),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/positions")
async def api_positions():
    """Open positions with unrealized P&L and trailing stop levels."""
    broker = _require_broker()
    state = _read_state()
    position_highs = state.get("position_highs", {})
    entry_timestamps = state.get("entry_timestamps", {})
    try:
        raw = broker.get_all_positions()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    result = []
    for p in raw:
        ts_pct = _trailing_stop_pct(p.symbol)
        peak = position_highs.get(p.symbol)
        trail_level = (peak * (1 - ts_pct / 100)) if (ts_pct and peak) else None
        result.append(
            {
                "symbol": p.symbol,
                "qty": p.qty,
                "side": p.side,
                "avg_entry_price": p.avg_entry_price,
                "current_price": p.current_price,
                "unrealized_pl": p.unrealized_pl,
                "unrealized_plpc": p.unrealized_plpc,
                "market_value": p.market_value,
                "peak_price": peak,
                "trailing_stop_pct": ts_pct,
                "trailing_stop_level": trail_level,
                "entry_timestamp": entry_timestamps.get(p.symbol),
            }
        )
    return {"positions": result, "count": len(result)}


@app.get("/api/trades")
async def api_trades():
    """Today's Alpaca orders + state file trades_today."""
    broker = _require_broker()
    orders: List[Dict] = []
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        today = date.today()
        since = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, after=since, limit=50)
        raw_orders = broker._retry_with_backoff(broker.trading_client.get_orders, req)
        for o in raw_orders:
            orders.append(
                {
                    "id": str(o.id),
                    "symbol": o.symbol,
                    "side": _enum_val(o.side),
                    "qty": float(o.qty) if o.qty else 0.0,
                    "filled_qty": float(o.filled_qty) if o.filled_qty else 0.0,
                    "filled_avg_price": (
                        float(o.filled_avg_price) if o.filled_avg_price else None
                    ),
                    "order_type": _enum_val(o.order_type),
                    "status": _enum_val(o.status),
                    "submitted_at": _dt_str(o.submitted_at),
                    "filled_at": _dt_str(o.filled_at),
                }
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(f"Order fetch failed: {exc}")

    state = _read_state()
    return {
        "orders": orders,
        "total": len(orders),
        "state_trades_today": state.get("trades_today", []),
    }


@app.get("/api/risk")
async def api_risk():
    """Daily P&L, loss limit progress, paused/dry-run status."""
    state = _read_state()
    daily_pnl = float(state.get("daily_pnl", 0.0))
    equity = loss_limit_pct = loss_limit_dollars = loss_used_pct = None
    try:
        a = _require_broker().get_account()
        equity = a.equity
    except Exception:
        pass
    loss_limit_pct = _max_daily_loss_pct()
    if loss_limit_pct and equity:
        loss_limit_dollars = equity * (loss_limit_pct / 100)
        loss_used_pct = (
            min(abs(daily_pnl) / loss_limit_dollars * 100, 100.0)
            if daily_pnl < 0 and loss_limit_dollars > 0
            else 0.0
        )
    env_paused = read_bool_env("TRADING_PAUSED", default=False)
    # dashboard_paused has its own dedicated pause file (audit bug 2.5) - same
    # source _build_payload()'s WS risk block reads; this REST endpoint
    # previously only checked the env var and ignored it entirely, so a
    # dashboard-kill-switch pause was invisible to any client polling here.
    from alphalive.state import BotState

    dash_paused = BotState(
        os.getenv("STATE_FILE", "/tmp/alphalive_state.json")
    ).check_dashboard_paused()
    return {
        "daily_pnl": daily_pnl,
        "trading_paused": env_paused or dash_paused,
        "trading_paused_env": env_paused,
        "trading_paused_dashboard": dash_paused,
        "dry_run": read_bool_env("DRY_RUN", default=False),
        "equity": equity,
        "daily_loss_limit_pct": loss_limit_pct,
        "daily_loss_limit_dollars": loss_limit_dollars,
        "daily_loss_used_pct": loss_used_pct,
    }


@app.get("/api/health")
async def api_health():
    """Broker status, market hours, bot startup/check timestamps."""
    state = _read_state()
    broker_ok = False
    broker_err = _broker_error
    market = None
    try:
        raw = _require_broker().get_market_hours()
        broker_ok = True
        market = {
            "is_open": bool(raw["is_open"]),
            "next_open": _dt_str(raw.get("next_open")),
            "next_close": _dt_str(raw.get("next_close")),
            "timestamp": _dt_str(raw.get("timestamp")),
        }
    except Exception as exc:
        broker_err = str(exc)
    return {
        "last_startup": state.get("last_startup"),
        "last_morning_check_date": state.get("last_morning_check_date"),
        "last_eod_summary_date": state.get("last_eod_summary_date"),
        "last_saved": state.get("last_saved"),
        "market": market,
        "broker_connected": broker_ok,
        "broker_error": broker_err,
        "paper_trading": os.getenv("ALPACA_PAPER", "true").lower()
        in ("true", "1", "yes"),
        "server_time": datetime.now().isoformat(),
        "state_file": os.getenv("STATE_FILE", "/tmp/alphalive_state.json"),
        "railway_configured": bool(
            os.getenv("RAILWAY_API_TOKEN")
            and os.getenv("RAILWAY_SERVICE_ID")
            and os.getenv("RAILWAY_ENVIRONMENT_ID")
        ),
    }


@app.get("/api/bars/{ticker}")
async def api_bars(ticker: str, n: int = 20):
    """Last n daily bars for ticker. Used by position mini sparklines."""
    broker = _require_broker()
    try:
        raw = broker.get_bars(symbol=ticker.upper(), timeframe="1Day", limit=n)
        bars = [
            {
                "t": _dt_str(b["timestamp"]),
                "o": b["open"],
                "h": b["high"],
                "l": b["low"],
                "c": b["close"],
                "v": b["volume"],
            }
            for b in raw
        ]
        return {"ticker": ticker.upper(), "bars": bars, "count": len(bars)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Kill switch control endpoints
# ---------------------------------------------------------------------------


@app.post("/api/deploy")
async def api_deploy():
    """
    Trigger a Railway redeploy of the AlphaLive service.

    Required env vars:
        RAILWAY_API_TOKEN - Railway personal API token (Account → API Tokens)
        RAILWAY_SERVICE_ID - Service ID (Railway dashboard → service → Settings → General)
        RAILWAY_ENVIRONMENT_ID - Environment ID (Railway dashboard → environment → Settings)
    """
    token = os.getenv("RAILWAY_API_TOKEN", "")
    svc_id = os.getenv("RAILWAY_SERVICE_ID", "")
    env_id = os.getenv("RAILWAY_ENVIRONMENT_ID", "")

    if not token or not svc_id or not env_id:
        raise HTTPException(
            status_code=501,
            detail=(
                "Railway redeploy not configured. Set RAILWAY_API_TOKEN, "
                "RAILWAY_SERVICE_ID, and RAILWAY_ENVIRONMENT_ID."
            ),
        )

    import httpx

    mutation = """
    mutation serviceInstanceRedeploy($serviceId: String!, $environmentId: String!) {
      serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
    }
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://backboard.railway.app/graphql/v2",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "query": mutation,
                    "variables": {"serviceId": svc_id, "environmentId": env_id},
                },
            )

        if resp.status_code != 200:
            raise HTTPException(
                status_code=502, detail=f"Railway API returned HTTP {resp.status_code}"
            )

        data = resp.json()
        if "errors" in data:
            msgs = "; ".join(e.get("message", str(e)) for e in data["errors"])
            raise HTTPException(status_code=502, detail=f"Railway error: {msgs}")

        logger.info("Railway redeploy triggered successfully")
        return {
            "ok": True,
            "message": "Redeploy triggered - Railway will restart in ~30s",
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Railway API call failed: {exc}")


@app.get("/api/screener")
async def api_screener():
    """Return latest Greenblatt screener output. 404 if not yet generated."""
    path = os.getenv("SCREENER_OUTPUT_PATH", "screener_output.json")
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="Screener output not found - runs on 1st of each month",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/control/pause")
async def api_pause():
    """Activate dashboard kill switch - bot stops taking new signals within 30s."""
    loop = asyncio.get_event_loop()

    def _do():
        from alphalive.state import BotState

        BotState(
            os.getenv("STATE_FILE", "/tmp/alphalive_state.json")
        ).set_dashboard_pause(True)

    await loop.run_in_executor(None, _do)
    logger.info("Dashboard kill switch ACTIVATED via POST /api/control/pause")
    return {"ok": True, "dashboard_paused": True}


@app.post("/api/control/resume")
async def api_resume():
    """Deactivate dashboard kill switch - bot resumes normal operation."""
    loop = asyncio.get_event_loop()

    def _do():
        from alphalive.state import BotState

        BotState(
            os.getenv("STATE_FILE", "/tmp/alphalive_state.json")
        ).set_dashboard_pause(False)

    await loop.run_in_executor(None, _do)
    logger.info("Dashboard kill switch CLEARED via POST /api/control/resume")
    return {"ok": True, "dashboard_paused": False}


# ---------------------------------------------------------------------------
# Dev live-reload (opt-in, off by default)
#
# Vite-style "save the file, browser refreshes itself" for this plain
# static-HTML dashboard. Off unless DASHBOARD_DEV_RELOAD is set truthy, so it
# never runs in production (Railway) and adds zero overhead there. When on:
# a background task watches dashboard/*.html for changes via `watchfiles`
# (already an existing transitive dep of uvicorn[standard], no new package),
# and a tiny inline script - injected into served HTML only in this mode -
# opens a websocket to /dev/reload-ws and calls location.reload() on message.
# ---------------------------------------------------------------------------

DEV_RELOAD = os.getenv("DASHBOARD_DEV_RELOAD", "false").lower() in ("true", "1", "yes")
_reload_clients: set = set()

_RELOAD_SNIPPET = """
<script>
// Injected only when DASHBOARD_DEV_RELOAD=true - dev-only auto-refresh.
(function () {
  function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(proto + "//" + location.host + "/dev/reload-ws");
    ws.onmessage = () => location.reload();
    ws.onclose = () => setTimeout(connect, 1000);
  }
  connect();
})();
</script>
"""


def _serve_html_with_optional_reload(html_path: Path) -> Response:
    if not DEV_RELOAD:
        return FileResponse(html_path)
    html = html_path.read_text()
    html = html.replace("</body>", _RELOAD_SNIPPET + "</body>")
    return HTMLResponse(html)


@app.on_event("startup")
async def _maybe_start_dev_reload_watcher():
    if not DEV_RELOAD:
        return
    logger.info(
        "DASHBOARD_DEV_RELOAD enabled - watching dashboard/*.html for browser auto-refresh"
    )

    async def _watch():
        from watchfiles import awatch

        async for _changes in awatch(
            DASHBOARD_DIR, watch_filter=lambda change, path: path.endswith(".html")
        ):
            logger.info(
                "Dashboard HTML changed - notifying %d connected browser(s)",
                len(_reload_clients),
            )
            for ws in list(_reload_clients):
                try:
                    await ws.send_text("reload")
                except Exception:
                    _reload_clients.discard(ws)

    asyncio.create_task(_watch())


@app.websocket("/dev/reload-ws")
async def dev_reload_ws(websocket: WebSocket):
    if not DEV_RELOAD:
        await websocket.close(code=1000)
        return
    await websocket.accept()
    _reload_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _reload_clients.discard(websocket)


# ---------------------------------------------------------------------------
# Static file
# ---------------------------------------------------------------------------


@app.get("/")
async def serve_index():
    html_path = DASHBOARD_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="dashboard/index.html not found")
    return _serve_html_with_optional_reload(html_path)


@app.get("/logo.png")
async def serve_logo():
    logo_path = DASHBOARD_DIR / "logo.png"
    if not logo_path.exists():
        raise HTTPException(status_code=404, detail="logo.png not found")
    return FileResponse(logo_path)


@app.get("/design-preview")
async def serve_design_preview():
    """Experimental mock-data-only visual-direction preview. Not wired to any
    broker/API/state logic - see dashboard/design-preview.html."""
    html_path = DASHBOARD_DIR / "design-preview.html"
    if not html_path.exists():
        raise HTTPException(
            status_code=404, detail="dashboard/design-preview.html not found"
        )
    return _serve_html_with_optional_reload(html_path)
