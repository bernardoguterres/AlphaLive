"""Greenblatt Magic Formula screener for AlphaLive (informational).

Runs once per calendar month (main.py's _run_monthly_screener) and writes a
candidates JSON file shown on the dashboard and summarised on Telegram. It
does NOT choose what the bot trades - each deployed strategy config names
its own ticker.

Formula (aligned with AlphaLab's FundamentalScreener, 2026-10 - this module
previously ranked 1/trailing-P/E + ROE under the same name, the proxy
AlphaLab retired as not Greenblatt's method):
  Earnings Yield    = EBIT / Enterprise Value
  Return on Capital = EBIT / (Net Working Capital + Net PPE)
  combined_rank     = EY rank + ROC rank (1 = best each; ties broken by
                      universe order), lower is better.
Financial Services / Utilities are excluded; EBIT, invested capital and EV
must be positive; market-cap and debt/equity filters as AlphaLab.
Independently implemented (not imported from AlphaLab) and checked against
hand-computed values in tests/test_fundamental_screener.py.

Limitation (shared with AlphaLab): yfinance gives only current fundamentals.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

EXCLUDED_SECTORS = {"Financial Services", "Financials", "Utilities"}
EBIT_ROWS = ["EBIT", "Total Operating Income As Reported", "Operating Income"]
NWC_ROWS = ["Working Capital"]
CURRENT_ASSET_ROWS = ["Current Assets", "Total Current Assets"]
CURRENT_LIAB_ROWS = ["Current Liabilities", "Total Current Liabilities"]
NET_PPE_ROWS = ["Net PPE", "Net Property Plant Equipment"]


def latest_value(statement, rows: list[str]):
    """Most recent (first-column) finite value of the first matching row."""
    if statement is None or not hasattr(statement, "index"):
        return None
    for name in rows:
        if name not in statement.index:
            continue
        try:
            series = statement.loc[name]
            value = float(series.iloc[0] if hasattr(series, "iloc") else series)
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(value):
            return value
    return None


@dataclass
class ScreenerResult:
    ticker: str
    company_name: str
    sector: str
    earnings_yield: float  # EBIT / EV
    return_on_capital: float  # EBIT / (NWC + Net PPE)
    ebit: float
    enterprise_value: float
    invested_capital: float
    market_cap_b: float
    debt_to_equity: float
    earnings_yield_rank: int = 0
    roc_rank: int = 0
    combined_rank: int = 0


class FundamentalScreener:
    """Monthly Magic Formula screen (see module docstring)."""

    def __init__(
        self,
        universe: list[str],
        output_path: str = "screener_output.json",
        top_n: int = 15,
        min_market_cap_b: float = 1.0,
        max_debt_to_equity: float = 2.0,
        request_delay: float = 0.3,
    ):
        self.universe = universe
        self.output_path = Path(output_path)
        self.top_n = top_n
        self.min_market_cap_b = min_market_cap_b
        self.max_debt_to_equity = max_debt_to_equity
        self.request_delay = request_delay

    def run(self) -> list[ScreenerResult]:
        """Run the full screen immediately regardless of date."""
        raw = self._fetch_all()
        qualified = self._filter(raw)
        ranked = self._rank(qualified)
        candidates = ranked[: self.top_n]
        self._save(candidates)
        logger.info(
            f"Screener complete: {len(self.universe)} fetched -> "
            f"{len(qualified)} qualified -> {len(candidates)} selected"
        )
        return candidates

    def load_candidates(self) -> list[str]:
        """Tickers from the last saved output (empty if none)."""
        if not self.output_path.exists():
            logger.warning(
                f"No screener output at {self.output_path} - returning empty list"
            )
            return []
        try:
            data = json.loads(self.output_path.read_text())
            return [c["ticker"] for c in data.get("candidates", [])]
        except Exception as exc:
            logger.error(f"Failed to load screener output: {exc}")
            return []

    def _fetch_all(self) -> list[ScreenerResult]:
        results = []
        for i, ticker in enumerate(self.universe):
            result = self._fetch_one(ticker)
            if result is not None:
                results.append(result)
            if i < len(self.universe) - 1:
                time.sleep(self.request_delay)
        return results

    def _fetch_one(self, ticker: str) -> ScreenerResult | None:
        try:
            t = yf.Ticker(ticker)
            info = t.info
            if not info or info.get("regularMarketPrice") is None:
                return None
            return self.evaluate(ticker, info, t.income_stmt, t.balance_sheet)
        except Exception as exc:
            logger.warning(f"{ticker}: fetch failed - {exc}")
            return None

    @staticmethod
    def evaluate(
        ticker: str, info: dict, income_stmt, balance_sheet
    ) -> ScreenerResult | None:
        """Compute the two factors for one company, or None if unrankable."""
        sector = info.get("sector", "Unknown")
        if sector in EXCLUDED_SECTORS:
            return None
        ebit = latest_value(income_stmt, EBIT_ROWS)
        ev = info.get("enterpriseValue")
        nwc = latest_value(balance_sheet, NWC_ROWS)
        if nwc is None:
            ca = latest_value(balance_sheet, CURRENT_ASSET_ROWS)
            cl = latest_value(balance_sheet, CURRENT_LIAB_ROWS)
            nwc = ca - cl if ca is not None and cl is not None else None
        ppe = latest_value(balance_sheet, NET_PPE_ROWS)
        if None in (ebit, ev, nwc, ppe):
            return None
        invested = nwc + ppe
        if ebit <= 0 or invested <= 0 or ev <= 0:
            return None
        return ScreenerResult(
            ticker=ticker,
            company_name=info.get("shortName", ticker),
            sector=sector,
            earnings_yield=ebit / ev,
            return_on_capital=ebit / invested,
            ebit=ebit,
            enterprise_value=float(ev),
            invested_capital=invested,
            market_cap_b=(info.get("marketCap") or 0) / 1e9,
            # yfinance debtToEquity is a percentage (79.5 = 0.795x)
            debt_to_equity=(info.get("debtToEquity") or 0.0) / 100.0,
        )

    def _filter(self, results: list[ScreenerResult]) -> list[ScreenerResult]:
        return [
            r
            for r in results
            if r.market_cap_b >= self.min_market_cap_b
            and r.debt_to_equity <= self.max_debt_to_equity
        ]

    @staticmethod
    def _rank(results: list[ScreenerResult]) -> list[ScreenerResult]:
        def ranks(key):
            order = sorted(range(len(results)), key=lambda i: (-key(results[i]), i))
            out = [0] * len(results)
            for pos, i in enumerate(order, start=1):
                out[i] = pos
            return out

        ey = ranks(lambda r: r.earnings_yield)
        roc = ranks(lambda r: r.return_on_capital)
        for r, a, b in zip(results, ey, roc):
            r.earnings_yield_rank, r.roc_rank = a, b
            r.combined_rank = a + b
        return sorted(results, key=lambda r: r.combined_rank)

    def _save(self, candidates: list[ScreenerResult]) -> None:
        output = {
            "screened_at": datetime.now(ET).isoformat(),
            "universe_size": len(self.universe),
            "candidates": [asdict(c) for c in candidates],
        }
        self.output_path.write_text(json.dumps(output, indent=2))
        logger.info(f"Screener output saved to {self.output_path}")
