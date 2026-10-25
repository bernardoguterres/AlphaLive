#!/usr/bin/env python3
"""Measure AlphaLab<->AlphaLive signal agreement on the AlphaLab-oracle fixtures.

Reports, per strategy and ticker:
  - all-bar agreement (dominated by HOLD/HOLD bars - near 100% even for
    badly divergent logic; reported only for context),
  - decision-bar agreement (bars where EITHER engine emits BUY/SELL),
  - every mismatching decision with its date,
  - the same with AlphaLive's live-only bear-market ENTRY filter enabled
    (its effect is intentional and is not a parity defect).

Inputs are produced by AlphaLab (AlphaLab/scripts/generate_signal_parity_fixtures.py);
AlphaLive is configured with the manifest's exported parameters.

Usage:  python scripts/audit/signal_parity_report.py
"""

import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)

from tests.test_multi_ticker_parity import TICKERS, measure_parity  # noqa: E402

manifest = json.loads((ROOT / "tests/fixtures/signal_parity_manifest.json").read_text())

print(
    f"{'strategy':22s} {'tkr':4s} {'bars':>4s} {'all-bar':>8s} {'decisions':>9s} "
    f"{'decision agree':>14s} {'w/ bear filter':>14s}"
)
for name, spec in manifest["strategies"].items():
    for t in TICKERS:
        os.environ["ENABLE_BEAR_MARKET_FILTER"] = "false"
        r = measure_parity(t, name, spec["alphalive_params"])
        os.environ["ENABLE_BEAR_MARKET_FILTER"] = "true"
        f = measure_parity(t, name, spec["alphalive_params"])
        print(
            f"{name:22s} {t:4s} {r['bars']:4d} {r['all_bar_agreement']:8.1%} "
            f"{r['decision_bars']:9d} {r['decision_agreement']:14.1%} "
            f"{f['decision_agreement']:14.1%}"
        )
        for m in r["mismatches"]:
            print(
                f"    MISMATCH {m['date']}: AlphaLab {m['alphalab']} vs AlphaLive {m['alphalive']}"
            )
        for m in f["mismatches"]:
            print(
                f"    bear-filter effect {m['date']}: AlphaLab {m['alphalab']} vs AlphaLive {m['alphalive']}"
            )
