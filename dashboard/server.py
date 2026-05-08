"""
AlphaLive Dashboard Server v2

New in v2:
- WebSocket /ws endpoint (pushes combined payload every 5s)
- GET /api/bars/{ticker}  — last N daily bars for position sparklines
- HTTP Basic Auth via DASHBOARD_PASSWORD env var (no-op when unset)

Usage (from project root):
    uvicorn dashboard.server:app --host 0.0.0.0 --port 8888 --reload

Required env vars:  ALPACA_API_KEY, ALPACA_SECRET_KEY
Optional env vars:  ALPACA_PAPER (default true), STATE_FILE,
                    STRATEGY_CONFIG or STRATEGY_CONFIG_DIR,
                    DASHBOARD_PASSWORD (enables HTTP Basic Auth)
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
from fastapi.responses import FileResponse, Response

try:
    from dotenv import load_dotenv
    if Path(".env").exists():
        load_dotenv()
except ImportError:
    pass

from alphalive.broker.alpaca_broker import AlpacaBroker

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="AlphaLive Dashboard", version="2.0.0")
DASHBOARD_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# HTTP Basic Auth middleware
# Applied to every request (HTTP and WebSocket upgrade) when DASHBOARD_PASSWORD
# is set.  Browsers that have authenticated for the page will include the
# Authorization header in the WebSocket upgrade, so auth "just works".
# ---------------------------------------------------------------------------

_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")


@app.middleware("http")
async def _basic_auth(request: Request, call_next):
    if _PASSWORD:
        auth = request.headers.get("authorization", "")
        ok = False
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8", errors="replace")
                _, _, given = decoded.partition(":")
                ok = secrets.compare_digest(given.encode(), _PASSWORD.encode())
            except Exception:
                pass
        if not ok:
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

    paper = os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")

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
    """HTTP-route wrapper — raises HTTPException on broker failure."""
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
# Payload builder (synchronous — called via run_in_executor from WS handler)
# ---------------------------------------------------------------------------

def _build_payload() -> Dict[str, Any]:
    """
    Gather account / positions / orders / risk / health into one dict.
    Fully synchronous; never raises — all errors go into error fields.
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
            "paper": os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes"),
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
            positions.append({
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
            })
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
            orders.append({
                "id": str(o.id),
                "symbol": o.symbol,
                "side": _enum_val(o.side),
                "qty": float(o.qty) if o.qty else 0.0,
                "filled_qty": float(o.filled_qty) if o.filled_qty else 0.0,
                "filled_avg_price": float(o.filled_avg_price) if o.filled_avg_price else None,
                "order_type": _enum_val(o.order_type),
                "status": _enum_val(o.status),
                "submitted_at": _dt_str(o.submitted_at),
                "filled_at": _dt_str(o.filled_at),
            })
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
        env_paused = os.getenv("TRADING_PAUSED", "false").lower() in ("true", "1", "yes")
        dash_paused = bool(state.get("dashboard_paused", False))
        out["risk"] = {
            "daily_pnl": daily_pnl,
            "trading_paused": env_paused or dash_paused,
            "trading_paused_env": env_paused,
            "trading_paused_dashboard": dash_paused,
            "dry_run": os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes"),
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
        "paper_trading": os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes"),
        "server_time": datetime.now().isoformat(),
        "state_file": os.getenv("STATE_FILE", "/tmp/alphalive_state.json"),
    }

    return out


# ---------------------------------------------------------------------------
# WebSocket endpoint — pushes full payload every 5 seconds
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
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
            "paper": os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes"),
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
        result.append({
            "symbol": p.symbol, "qty": p.qty, "side": p.side,
            "avg_entry_price": p.avg_entry_price, "current_price": p.current_price,
            "unrealized_pl": p.unrealized_pl, "unrealized_plpc": p.unrealized_plpc,
            "market_value": p.market_value, "peak_price": peak,
            "trailing_stop_pct": ts_pct, "trailing_stop_level": trail_level,
            "entry_timestamp": entry_timestamps.get(p.symbol),
        })
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
            orders.append({
                "id": str(o.id), "symbol": o.symbol,
                "side": _enum_val(o.side),
                "qty": float(o.qty) if o.qty else 0.0,
                "filled_qty": float(o.filled_qty) if o.filled_qty else 0.0,
                "filled_avg_price": float(o.filled_avg_price) if o.filled_avg_price else None,
                "order_type": _enum_val(o.order_type), "status": _enum_val(o.status),
                "submitted_at": _dt_str(o.submitted_at), "filled_at": _dt_str(o.filled_at),
            })
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(f"Order fetch failed: {exc}")

    state = _read_state()
    return {"orders": orders, "total": len(orders),
            "state_trades_today": state.get("trades_today", [])}


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
            if daily_pnl < 0 and loss_limit_dollars > 0 else 0.0
        )
    return {
        "daily_pnl": daily_pnl,
        "trading_paused": os.getenv("TRADING_PAUSED", "false").lower() in ("true", "1", "yes"),
        "dry_run": os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes"),
        "equity": equity, "daily_loss_limit_pct": loss_limit_pct,
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
        "market": market, "broker_connected": broker_ok, "broker_error": broker_err,
        "paper_trading": os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes"),
        "server_time": datetime.now().isoformat(),
        "state_file": os.getenv("STATE_FILE", "/tmp/alphalive_state.json"),
    }


@app.get("/api/bars/{ticker}")
async def api_bars(ticker: str, n: int = 20):
    """Last n daily bars for ticker. Used by position mini sparklines."""
    broker = _require_broker()
    try:
        raw = broker.get_bars(symbol=ticker.upper(), timeframe="1Day", limit=n)
        bars = [
            {"t": _dt_str(b["timestamp"]), "o": b["open"], "h": b["high"],
             "l": b["low"], "c": b["close"], "v": b["volume"]}
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

@app.post("/api/control/pause")
async def api_pause():
    """Activate dashboard kill switch — bot stops taking new signals within 30s."""
    loop = asyncio.get_event_loop()
    def _do():
        from alphalive.state import BotState
        BotState(os.getenv("STATE_FILE", "/tmp/alphalive_state.json")).set_dashboard_pause(True)
    await loop.run_in_executor(None, _do)
    logger.info("Dashboard kill switch ACTIVATED via POST /api/control/pause")
    return {"ok": True, "dashboard_paused": True}


@app.post("/api/control/resume")
async def api_resume():
    """Deactivate dashboard kill switch — bot resumes normal operation."""
    loop = asyncio.get_event_loop()
    def _do():
        from alphalive.state import BotState
        BotState(os.getenv("STATE_FILE", "/tmp/alphalive_state.json")).set_dashboard_pause(False)
    await loop.run_in_executor(None, _do)
    logger.info("Dashboard kill switch CLEARED via POST /api/control/resume")
    return {"ok": True, "dashboard_paused": False}


# ---------------------------------------------------------------------------
# Static file
# ---------------------------------------------------------------------------

@app.get("/")
async def serve_index():
    html_path = DASHBOARD_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="dashboard/index.html not found")
    return FileResponse(html_path)
