"""Shared helpers for the reporting scripts (performance tracker, trade
journal, weekly report): fetch the broker's filled orders and pair them into
closed trades FIFO by quantity.

Before this, each script had its own copy of two bugs (Nov 2026 audit, L8/L9):
- they requested ``QueryOrderStatus.FILLED``, which alpaca-py (0.28) does not
  have (only OPEN / CLOSED / ALL), so every run crashed with AttributeError;
- each SELL "popped" one whole BUY order and used that BUY's quantity, so
  add-on buys and partial sells produced wrong P&L and phantom open positions
  (10@100 + 5@110, sell 15@120 -> reported 200 with 1 open; truth 250, none).
"""

from typing import Any, Dict, Iterable, List, Optional

EPS = 1e-9


def fetch_filled_orders(client, after=None, until=None, limit: int = 500) -> list:
    """Orders with any filled quantity. CLOSED covers filled orders and ones
    cancelled/expired after a partial fill; unfilled ones are dropped."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    kwargs: Dict[str, Any] = {"status": QueryOrderStatus.CLOSED, "limit": limit}
    if after is not None:
        kwargs["after"] = after
    if until is not None:
        kwargs["until"] = until
    orders = client.get_orders(filter=GetOrdersRequest(**kwargs))
    return [o for o in orders if float(getattr(o, "filled_qty", 0) or 0) > 0]


def match_fills_fifo(fills: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pair fills into closed trades, FIFO by quantity, per symbol.

    Each fill is a dict with ``symbol``, ``side`` ("buy"/"sell"), ``qty``,
    ``fill_price``, ``filled_at`` and optionally ``id``. Returns one record per
    SELL that closed shares: the quantity it sold (capped at what was held),
    the volume-weighted entry price of the lots it consumed, P&L and P&L %,
    the earliest consumed lot's time as entry_time, and the buy order ids.
    A SELL with nothing open (e.g. the position predates the window) is
    skipped. Unmatched BUY quantity stays open and produces no record.
    """
    ordered = sorted(fills, key=lambda f: (f["filled_at"] is None, f["filled_at"]))
    lots: Dict[str, List[List[Any]]] = {}  # symbol -> [[qty, price, time, id], ...]
    trades: List[Dict[str, Any]] = []

    for f in ordered:
        symbol = f["symbol"]
        side = str(f["side"]).lower()
        qty = float(f["qty"])
        price = float(f["fill_price"])
        book = lots.setdefault(symbol, [])
        if side == "buy":
            book.append([qty, price, f["filled_at"], f.get("id")])
            continue
        if side != "sell" or not book:
            continue

        remaining = qty
        matched_qty = 0.0
        cost = 0.0
        entry_time: Optional[Any] = None
        buy_ids: List[Any] = []
        while remaining > EPS and book:
            lot = book[0]
            take = min(remaining, lot[0])
            matched_qty += take
            cost += take * lot[1]
            if entry_time is None:
                entry_time = lot[2]
            if lot[3] is not None and lot[3] not in buy_ids:
                buy_ids.append(lot[3])
            lot[0] -= take
            remaining -= take
            if lot[0] <= EPS:
                book.pop(0)
        if matched_qty <= EPS:
            continue

        entry_price = cost / matched_qty
        pnl = (price - entry_price) * matched_qty
        trades.append(
            {
                "symbol": symbol,
                "qty": matched_qty,
                "entry_price": entry_price,
                "exit_price": price,
                "pnl": pnl,
                "pnl_pct": ((price / entry_price) - 1) * 100 if entry_price else 0.0,
                "entry_time": entry_time,
                "exit_time": f["filled_at"],
                "buy_order_ids": buy_ids,
                "sell_order_id": f.get("id"),
            }
        )
    return trades


def open_quantities(fills: Iterable[Dict[str, Any]]) -> Dict[str, float]:
    """Quantity still open per symbol after FIFO matching (for reports)."""
    held: Dict[str, float] = {}
    for f in sorted(fills, key=lambda f: (f["filled_at"] is None, f["filled_at"])):
        sign = 1.0 if str(f["side"]).lower() == "buy" else -1.0
        held[f["symbol"]] = max(
            0.0, held.get(f["symbol"], 0.0) + sign * float(f["qty"])
        )
    return {s: q for s, q in held.items() if q > EPS}
