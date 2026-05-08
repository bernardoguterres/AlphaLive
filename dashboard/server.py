"""
AlphaLive Dashboard Server

FastAPI server exposing live account, position, trade, risk, and health
data by connecting to the same Alpaca account as the running bot and
reading the bot's state file.

Usage (from project root):
    uvicorn dashboard.server:app --host 0.0.0.0 --port 8888 --reload

Required env vars:  ALPACA_API_KEY, ALPACA_SECRET_KEY
Optional env vars:  ALPACA_PAPER (default true), STATE_FILE,
                    STRATEGY_CONFIG or STRATEGY_CONFIG_DIR (enables trailing
                    stop levels in /api/positions)
"""

import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure repo root is importable when run via `uvicorn dashboard.server:app`
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

try:
    from dotenv import load_dotenv
    if Path(".env").exists():
        load_dotenv()
except ImportError:
    pass

from alphalive.broker.alpaca_broker import AlpacaBroker

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="AlphaLive Dashboard", version="1.0.0")
DASHBOARD_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# Lazy broker singleton — reconnects automatically on next request if lost
# ---------------------------------------------------------------------------

_broker: Optional[AlpacaBroker] = None
_broker_error: Optional[str] = None


def _get_broker() -> AlpacaBroker:
    global _broker, _broker_error

    if _broker is not None and _broker.connected:
        return _broker

    api_key = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")

    if not api_key or not secret_key:
        _broker_error = "ALPACA_API_KEY or ALPACA_SECRET_KEY not set"
        raise HTTPException(status_code=503, detail=_broker_error)

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
        raise HTTPException(status_code=503, detail=f"Broker error: {exc}")


# ---------------------------------------------------------------------------
# State file helper (always re-reads — no caching)
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
        logger.warning(f"Could not read state file at {path}: {exc}")
        return dict(_DEFAULT_STATE)


# ---------------------------------------------------------------------------
# Strategy config (optional) — loaded once for trailing stop / loss limit info
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
        logger.info(f"Loaded {len(_strategies)} strategies for dashboard context")
    except Exception as exc:
        logger.warning(f"Strategy config unavailable (trailing stop info disabled): {exc}")
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
    """Safely convert any datetime-like value to ISO string."""
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


def _enum_val(val) -> str:
    """Extract string value from enum or return str(val)."""
    if hasattr(val, "value"):
        return val.value
    return str(val)


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/account")
async def api_account():
    """Account equity, cash, buying_power, portfolio_value."""
    broker = _get_broker()
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
    """Open positions with unrealized P&L, entry price, and trailing stop level."""
    broker = _get_broker()
    state = _read_state()
    position_highs: Dict[str, float] = state.get("position_highs", {})
    entry_timestamps: Dict[str, str] = state.get("entry_timestamps", {})

    try:
        raw = broker.get_all_positions()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    result = []
    for p in raw:
        ts_pct = _trailing_stop_pct(p.symbol)
        peak = position_highs.get(p.symbol)
        trail_level = (peak * (1 - ts_pct / 100)) if (ts_pct and peak) else None
        result.append({
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

    return {"positions": result, "count": len(result)}


@app.get("/api/trades")
async def api_trades():
    """Today's orders from Alpaca + raw trades_today list from state file."""
    broker = _get_broker()

    orders: List[Dict] = []
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        today = date.today()
        since = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        req = GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            after=since,
            limit=50,
        )
        raw_orders = broker._retry_with_backoff(broker.trading_client.get_orders, req)
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
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(f"Order fetch failed (showing empty): {exc}")

    state = _read_state()
    return {
        "orders": orders,
        "total": len(orders),
        "state_trades_today": state.get("trades_today", []),
    }


@app.get("/api/risk")
async def api_risk():
    """Daily P&L, loss limit progress, trading paused status, dry run mode."""
    state = _read_state()
    daily_pnl: float = float(state.get("daily_pnl", 0.0))
    trading_paused = os.getenv("TRADING_PAUSED", "false").lower() in ("true", "1", "yes")
    dry_run = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")

    equity: Optional[float] = None
    loss_limit_pct = _max_daily_loss_pct()
    loss_limit_dollars: Optional[float] = None
    loss_used_pct: Optional[float] = None

    try:
        broker = _get_broker()
        a = broker.get_account()
        equity = a.equity
    except Exception:
        pass  # equity stays None; loss gauges will show N/A

    if loss_limit_pct and equity:
        loss_limit_dollars = equity * (loss_limit_pct / 100)
        if daily_pnl < 0 and loss_limit_dollars > 0:
            loss_used_pct = min(abs(daily_pnl) / loss_limit_dollars * 100, 100.0)
        else:
            loss_used_pct = 0.0

    return {
        "daily_pnl": daily_pnl,
        "trading_paused": trading_paused,
        "dry_run": dry_run,
        "equity": equity,
        "daily_loss_limit_pct": loss_limit_pct,
        "daily_loss_limit_dollars": loss_limit_dollars,
        "daily_loss_used_pct": loss_used_pct,
    }


@app.get("/api/health")
async def api_health():
    """Bot startup time, morning check status, market hours, broker connectivity."""
    state = _read_state()
    broker_ok = False
    broker_err = _broker_error
    market: Optional[Dict] = None

    try:
        broker = _get_broker()
        broker_ok = True
        raw = broker.get_market_hours()
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
        "paper_trading": os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes"),
        "server_time": datetime.now().isoformat(),
        "state_file": os.getenv("STATE_FILE", "/tmp/alphalive_state.json"),
    }


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

@app.get("/")
async def serve_index():
    html_path = DASHBOARD_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="dashboard/index.html not found")
    return FileResponse(html_path)
