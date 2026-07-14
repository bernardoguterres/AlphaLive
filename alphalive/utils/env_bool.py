"""Shared boolean environment-variable parsing.

Before this module existed, each safety-critical flag (ALPACA_PAPER, DRY_RUN,
TRADING_PAUSED) had its own ad-hoc parser, in three different files, with three
different failure modes (audit 2026-07-13, Group 2 bug 2.1):

- ``value.lower() == "true"`` (run.py, health.py): a leading/trailing
  whitespace typo like ``" true "`` silently parses to False - flipping
  paper trading to live, disabling dry-run (real orders instead of logged
  ones), or making the TRADING_PAUSED kill switch fail open.
- ``value.lower() in ("true", "1", "yes", "on")`` (config.py): same
  whitespace bug, plus a broader truthy set than the risk_manager.py
  enforcement check below - meaning the dashboard/logs (reading the
  config.py value) could report "paused" while risk_manager.py's own
  kill-switch check kept trading.
- ``value.lower() in ("true", "1", "yes")`` (risk_manager.py): missing "on".

``parse_bool_env``/``read_bool_env`` replace all of these. Whitespace and
case are normalized before comparison (so `` " true " `` correctly resolves
to True instead of silently falling through to False). A value that isn't a
recognized boolean string is genuinely ambiguous - it is NOT silently
defaulted, it raises, so a malformed flag stops the bot from starting (or
skips that trading-loop iteration, since main.py's per-iteration catch-all
treats any raised exception as "do nothing this cycle") rather than quietly
running in whichever state Python's usual falsy-default happens to produce.
"""

from __future__ import annotations

import os

TRUTHY = frozenset({"true", "1", "yes", "on"})
FALSY = frozenset({"false", "0", "no", "off"})


def parse_bool_env(raw_value: str | None, *, var_name: str, default: bool) -> bool:
    """Parse a boolean environment variable's raw string value.

    Args:
        raw_value: the raw string (e.g. from ``os.environ.get(var_name)``),
            or None if the variable is unset.
        var_name: the env var's name, used only in the error message.
        default: value to use when the variable is unset or set to an empty
            string. NOT used as a fallback for malformed input - malformed
            input always raises rather than guessing.

    Returns:
        The parsed boolean.

    Raises:
        ValueError: if raw_value is set to a non-empty string that isn't one
            of the recognized truthy/falsy values (case-insensitive,
            surrounding whitespace ignored).
    """
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized == "":
        return default
    if normalized in TRUTHY:
        return True
    if normalized in FALSY:
        return False
    raise ValueError(
        f"{var_name}={raw_value!r} is not a recognized boolean value. "
        f"Expected one of {sorted(TRUTHY | FALSY)} (case-insensitive, "
        f"surrounding whitespace ignored), or unset/empty for the default "
        f"({default})."
    )


def read_bool_env(var_name: str, default: bool) -> bool:
    """Read and parse a boolean environment variable by name."""
    return parse_bool_env(os.environ.get(var_name), var_name=var_name, default=default)
