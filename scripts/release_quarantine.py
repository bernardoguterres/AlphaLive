#!/usr/bin/env python3
"""Offline operator release for discrepancy-quarantine intents.

Stop the bot first. Lists quarantined intents; with --release INTENT_ID plus
--operator and --note, marks that intent "reconciled" after you have checked
the broker position by hand. Reads/writes only the local state file; makes no
broker or network calls.

    python scripts/release_quarantine.py --state-file state.json
    python scripts/release_quarantine.py --state-file state.json \
        --release <intent_id> --operator "Name" --note "verified at broker"
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphalive.state import INTENT_UNCERTAIN, BotState  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--state-file", required=True)
    ap.add_argument("--release")
    ap.add_argument("--operator", default="")
    ap.add_argument("--note", default="")
    args = ap.parse_args(argv)

    if not Path(args.state_file).exists():
        print(f"state file not found: {args.state_file}", file=sys.stderr)
        return 2
    state = BotState(state_file=args.state_file)
    if not args.release:
        rows = [
            i
            for i in state.list_open_intents()
            if i["status"] == INTENT_UNCERTAIN and i.get("is_quarantine")
        ]
        for i in rows:
            print(f"{i['intent_id']}  {i['ticker']} {i['side']} qty={i['qty']}  {i.get('last_error')}")
        print(f"{len(rows)} quarantined intent(s)")
        return 0
    if state.release_quarantine_intent(args.release, args.operator, args.note):
        print(f"released {args.release}")
        return 0
    print("not released: unknown intent, not a quarantine, or --operator/--note missing", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
