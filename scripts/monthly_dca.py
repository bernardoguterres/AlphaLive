"""Monthly DCA: buy a fixed dollar amount of an ETF on the 1st of each month.

Per MASTER_PLAN.md Priority 3, this IS the core real-money strategy until an
active strategy demonstrably beats buy-and-hold: automate the monthly SPY
purchase, let the additions compound.

Idempotent by design: the order's client_order_id is DCA_{ticker}_{YYYY_MM},
so Alpaca itself rejects a second buy in the same month (HTTP 409) - no state
file, safe to run from cron daily or retry after failures.

Usage:
    python scripts/monthly_dca.py                 # buy if due (1st..7th window)
    python scripts/monthly_dca.py --dry-run       # log what would happen
    python scripts/monthly_dca.py --force         # buy now regardless of date

Env vars:
    ALPACA_API_KEY / ALPACA_SECRET_KEY  (required)
    ALPACA_PAPER          default true
    DCA_AMOUNT_USD        default 100
    DCA_TICKER            default SPY
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID  (optional, for confirmations)

Cron example (weekdays at 15:35 UTC = 10:35/11:35 ET, window handles the 1st
falling on a weekend/holiday - the buy happens on the first run that succeeds):
    35 15 * * 1-5 cd /path/to/AlphaLive && python scripts/monthly_dca.py
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger("monthly_dca")
ET = ZoneInfo("America/New_York")

# The buy is "due" on the 1st, but markets close on weekends/holidays and a
# cron host can be down - accept any day in this window so the first
# successful run of the month wins (idempotency key blocks the rest).
DUE_WINDOW_DAYS = range(1, 8)


def build_client_order_id(ticker: str, now: datetime) -> str:
    return f"DCA_{ticker}_{now.strftime('%Y_%m')}"


def run(dry_run: bool = False, force: bool = False) -> int:
    from alpaca.common.exceptions import APIError
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    api_key = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        logger.error("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
        return 1

    paper = os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")
    amount = float(os.getenv("DCA_AMOUNT_USD", "100"))
    ticker = os.getenv("DCA_TICKER", "SPY").upper()

    now = datetime.now(ET)
    if not force and now.day not in DUE_WINDOW_DAYS:
        logger.info(
            f"Not due: day {now.day} outside window "
            f"{DUE_WINDOW_DAYS.start}-{DUE_WINDOW_DAYS.stop - 1} (use --force to override)"
        )
        return 0

    client_order_id = build_client_order_id(ticker, now)
    logger.info(
        f"DCA due: BUY ${amount:.2f} of {ticker} "
        f"({'paper' if paper else 'LIVE'}) | key={client_order_id}"
    )

    if dry_run:
        logger.info("[DRY RUN] No order placed")
        return 0

    trading = TradingClient(api_key=api_key, secret_key=secret_key, paper=paper)

    clock = trading.get_clock()
    if not clock.is_open:
        logger.info(
            f"Market closed (next open {clock.next_open}) - "
            f"will buy on the next in-window run"
        )
        return 0

    request = MarketOrderRequest(
        symbol=ticker,
        notional=amount,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        client_order_id=client_order_id,
    )

    try:
        order = trading.submit_order(request)
    except APIError as e:
        if getattr(e, "status_code", None) == 409 or "client_order_id" in str(e).lower():
            logger.info(f"Already bought this month ({client_order_id}) - nothing to do")
            return 0
        logger.error(f"Order failed: {e}")
        _notify(f"DCA order FAILED for {ticker}: {e}")
        return 1

    logger.info(f"Order placed: {order.id} | ${amount:.2f} {ticker} @ market")
    _notify(
        f"\U0001F4C5 Monthly DCA executed\n"
        f"BUY ${amount:.2f} of {ticker} ({'paper' if paper else 'LIVE'})\n"
        f"Order: {order.id}"
    )
    return 0


def _notify(message: str) -> None:
    """Best-effort Telegram confirmation - failures never break the buy."""
    try:
        from alphalive.notifications.telegram_bot import TelegramNotifier

        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if token and chat_id:
            TelegramNotifier(bot_token=token, chat_id=chat_id, enabled=True).send_message(
                message
            )
    except Exception as e:
        logger.warning(f"Telegram notification failed (buy unaffected): {e}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Log without ordering")
    parser.add_argument("--force", action="store_true", help="Ignore the date window")
    args = parser.parse_args()
    sys.exit(run(dry_run=args.dry_run, force=args.force))
