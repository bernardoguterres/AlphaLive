"""Greenblatt Magic Formula screener for AlphaLive production use.

Runs on the 1st of each month and writes a candidates JSON file.
AlphaLive's main loop reads this file to decide which tickers to monitor.

Usage (called from main.py):
    screener = FundamentalScreener(universe=MY_UNIVERSE, output_path="screener_output.json")
    candidates = screener.run_if_due()  # Returns list only if 1st of month
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


@dataclass
class ScreenerResult:
    ticker: str
    company_name: str
    sector: str
    earnings_yield: float
    return_on_equity: float
    pe_ratio: float
    market_cap_b: float
    debt_to_equity: float
    combined_rank: int


class FundamentalScreener:
    """Monthly Greenblatt screener for AlphaLive.

    Ranks a universe of stocks by earnings yield (1/PE) + ROE, outputs
    the top N candidates as a JSON file for AlphaLive to monitor.

    Args:
        universe: Ticker symbols to screen.
        output_path: Where to write screener_output.json.
        top_n: How many candidates to select.
        min_market_cap_b: Minimum market cap in billions.
        max_debt_to_equity: Maximum D/E ratio.
        request_delay: Seconds between yfinance calls.
    """

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_if_due(self) -> list[ScreenerResult] | None:
        """Run the screen only on the 1st of the month.

        Returns the candidate list if the screen ran, None otherwise.
        Writes output_path regardless so the main loop always has a valid file.
        """
        today = date.today()
        if today.day != 1:
            logger.debug(f"Screener not due (today is {today}, runs on 1st of month)")
            return None

        logger.info(f"Monthly screener due — screening {len(self.universe)} tickers")
        return self.run()

    def run(self) -> list[ScreenerResult]:
        """Run the full screen immediately regardless of date."""
        raw = self._fetch_all()
        qualified = self._filter(raw)
        ranked = self._rank(qualified)
        candidates = ranked[: self.top_n]

        self._save(candidates)
        logger.info(
            f"Screener complete: {len(self.universe)} fetched → "
            f"{len(qualified)} qualified → {len(candidates)} selected"
        )
        return candidates

    def load_candidates(self) -> list[str]:
        """Load the tickers from the last saved screener output.

        Returns an empty list if no output file exists yet.
        """
        if not self.output_path.exists():
            logger.warning(f"No screener output at {self.output_path} — returning empty list")
            return []
        try:
            data = json.loads(self.output_path.read_text())
            return [c["ticker"] for c in data.get("candidates", [])]
        except Exception as exc:
            logger.error(f"Failed to load screener output: {exc}")
            return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
            info = yf.Ticker(ticker).info
            if not info or info.get("regularMarketPrice") is None:
                return None

            pe = info.get("trailingPE")
            roe = info.get("returnOnEquity")
            market_cap = info.get("marketCap") or 0
            # yfinance debtToEquity is a percentage (e.g. 79.5 = 0.795×), normalise to ratio
            raw_dte = info.get("debtToEquity") or 0.0
            dte = raw_dte / 100.0

            if pe is None or pe <= 0 or roe is None:
                return None

            return ScreenerResult(
                ticker=ticker,
                company_name=info.get("shortName", ticker),
                sector=info.get("sector", "Unknown"),
                earnings_yield=1.0 / pe,
                return_on_equity=roe,
                pe_ratio=pe,
                market_cap_b=market_cap / 1e9,
                debt_to_equity=dte,
                combined_rank=0,
            )
        except Exception as exc:
            logger.warning(f"{ticker}: fetch failed — {exc}")
            return None

    def _filter(self, results: list[ScreenerResult]) -> list[ScreenerResult]:
        return [
            r for r in results
            if r.market_cap_b >= self.min_market_cap_b
            and r.debt_to_equity <= self.max_debt_to_equity
        ]

    def _rank(self, results: list[ScreenerResult]) -> list[ScreenerResult]:
        by_ey = sorted(results, key=lambda r: r.earnings_yield, reverse=True)
        ey_ranks = {r.ticker: i + 1 for i, r in enumerate(by_ey)}

        by_roe = sorted(results, key=lambda r: r.return_on_equity, reverse=True)
        roe_ranks = {r.ticker: i + 1 for i, r in enumerate(by_roe)}

        for r in results:
            r.combined_rank = ey_ranks[r.ticker] + roe_ranks[r.ticker]

        return sorted(results, key=lambda r: r.combined_rank)

    def _save(self, candidates: list[ScreenerResult]) -> None:
        output = {
            "screened_at": datetime.now(ET).isoformat(),
            "universe_size": len(self.universe),
            "candidates": [asdict(c) for c in candidates],
        }
        self.output_path.write_text(json.dumps(output, indent=2))
        logger.info(f"Screener output saved to {self.output_path}")
