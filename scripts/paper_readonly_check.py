"""Bounded, read-only Alpaca PAPER check through AlphaLive's normal entry point.

Runs ``run.py --dry-run`` twice against the paper account - a first session
across the 09:35-09:59 ET daily decision window, then a same-day restart - and
a market-data probe, and prints a redacted summary. Nothing is ever sent to
the broker except GET requests.

Run it yourself (it reads credentials; nothing secret is printed):

    cd AlphaLive
    python3 scripts/paper_readonly_check.py            # start ~09:33 ET on a trading day

Credentials come from the normal AlphaLive configuration: ``ALPACA_API_KEY`` /
``ALPACA_SECRET_KEY`` already exported in the shell, otherwise those two
values (plus the sentiment settings) from ``AlphaLive/.env`` (``--env-file``
to point elsewhere). No other variable from that file is used.

Safety (enforced here, before run.py starts):
  * the key must be a paper key (prefix "PK"); ALPACA_PAPER=false or a
    non-paper ALPACA_BASE_URL in the env file aborts the run
  * ALPACA_PAPER=true, ALPACA_BASE_URL=https://paper-api.alpaca.markets,
    ALPACA_DATA_URL unset (Alpaca's market-data host)
  * DRY_RUN=true, --dry-run and ALPHALIVE_BROKER_READ_ONLY=true: the
    transport refuses every non-GET/HEAD/OPTIONS request before it leaves the
    process (logged as BROKER MUTATION BLOCKED)
  * a fresh, empty run directory (refused if any .env exists in it or a
    parent, so AlphaLive's cwd .env loading finds nothing), disposable
    STATE_FILE, every TELEGRAM_* variable removed, file logs off
  * the health server cannot be disabled: it binds an OS-assigned port
    (HEALTH_PORT=0, PORT removed) for the duration of each session
  * sentiment settings passed through unchanged (fail policy preserved).
    AlphaLive has no default AlphaSignal address: with sentiment enabled,
    ALPHASIGNAL_URL must come from the env file or --alphasignal-url, else the
    run aborts here. At startup AlphaLive checks the address identifies as
    AlphaSignal and exits on a different service (before any broker call)
  * bounded runtime: each session is stopped with SIGTERM (then SIGKILL) at
    its deadline; the probe has a 120 s timeout; nothing is left running

Simulator mode (no credentials read) checks the mechanics against
``tests/broker_sim/alpaca_sim.py``:

    python3 -m tests.broker_sim.alpaca_sim --port 8765 &
    python3 scripts/paper_readonly_check.py --simulator http://127.0.0.1:8765
    python3 scripts/paper_readonly_check.py --simulator http://127.0.0.1:8765 \\
        --simulate-mutation-attempt     # DRY_RUN off, guard on: a BUY is refused

Dependencies: AlphaLive's requirements (alpaca-py, python-dotenv, pandas).
"""

import argparse
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ALPHALIVE = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ALPHALIVE / "configs" / "paper_check" / "SPY_bollinger_breakout.json"
ET = ZoneInfo("America/New_York")
PAPER_URL = "https://paper-api.alpaca.markets"
MAX_SESSION_SECONDS = 1800
PASSTHROUGH_VARS = (
    "ALPHASIGNAL_ENABLED",
    "ALPHASIGNAL_URL",
    "ALPHASIGNAL_API_KEY",
    "ALPHASIGNAL_TIMEOUT_SECONDS",
    "ALPHASIGNAL_FAIL_POLICY",
    "ALPHASIGNAL_SENTIMENT_THRESHOLD",
    "ENABLE_BEAR_MARKET_FILTER",
    "ALPACA_DATA_FEED",
)
# Removed from the inherited shell environment: anything that could redirect,
# widen or notify (credentials are re-added explicitly below).
_STRIP_PREFIXES = ("ALPACA_", "TELEGRAM_", "ALPHASIGNAL_", "ALPHALIVE_")
_STRIP_NAMES = (
    "DRY_RUN",
    "STATE_FILE",
    "STRATEGY_CONFIG",
    "STRATEGY_CONFIG_DIR",
    "SCREENER_UNIVERSE",
    "HEALTH_SECRET",
    "HEALTH_PORT",
    "PORT",
    "TRADING_PAUSED",
    "PERSISTENT_STORAGE",
)


class Abort(SystemExit):
    def __init__(self, msg):
        print(f"ABORTED before contacting Alpaca: {msg}")
        super().__init__(2)


def read_settings(env_file: Path, simulator: str | None) -> dict:
    """Credentials + pass-through settings, from the shell or the env file."""
    if simulator:  # mechanics check only: fake paper-style key, local simulator
        return {"ALPACA_API_KEY": "PKSIMULATOR", "ALPACA_SECRET_KEY": "sim"}
    if os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"):
        source = dict(os.environ)
    elif env_file.is_file():
        from dotenv import dotenv_values

        source = dotenv_values(env_file)
    else:
        raise Abort(
            f"no ALPACA_API_KEY/ALPACA_SECRET_KEY in the shell and no {env_file}"
        )
    return {
        k: source[k]
        for k in (
            "ALPACA_API_KEY",
            "ALPACA_SECRET_KEY",
            "ALPACA_PAPER",
            "ALPACA_BASE_URL",
            *PASSTHROUGH_VARS,
        )
        if source.get(k) is not None
    }


def build_env(
    settings: dict,
    run_dir: Path,
    simulator: str | None = None,
    mutation_attempt: bool = False,
    base_env: dict | None = None,
    alphasignal_url: str | None = None,
) -> dict:
    key = settings.get("ALPACA_API_KEY") or ""
    secret = settings.get("ALPACA_SECRET_KEY") or ""
    if not key or not secret:
        raise Abort("ALPACA_API_KEY / ALPACA_SECRET_KEY not configured")
    if not key.startswith("PK"):
        raise Abort("configured key is not a paper key (expected prefix PK)")
    if (settings.get("ALPACA_PAPER") or "true").strip().lower() in ("false", "0", "no"):
        raise Abort("configuration sets ALPACA_PAPER=false")
    configured_base = settings.get("ALPACA_BASE_URL")
    if (
        configured_base
        and urlsplit(configured_base).hostname != urlsplit(PAPER_URL).hostname
    ):
        raise Abort(
            f"configured ALPACA_BASE_URL host is {urlsplit(configured_base).hostname}"
        )
    if mutation_attempt and not simulator:
        raise Abort("--simulate-mutation-attempt is only allowed with --simulator")

    base = os.environ if base_env is None else base_env
    env = {
        k: v
        for k, v in base.items()
        if not k.startswith(_STRIP_PREFIXES) and k not in _STRIP_NAMES
    }
    for k in PASSTHROUGH_VARS:
        if settings.get(k) is not None:
            env[k] = settings[k]
    env.update(
        ALPACA_API_KEY=key,
        ALPACA_SECRET_KEY=secret,
        ALPACA_PAPER="true",
        ALPACA_BASE_URL=PAPER_URL,
        DRY_RUN="true",
        ALPHALIVE_BROKER_READ_ONLY="true",
        STATE_FILE=str(run_dir / "state.json"),
        HEALTH_PORT="0",
        ENABLE_FILE_LOGS="false",
        LOG_LEVEL="INFO",
        PYTHONUNBUFFERED="1",
    )
    if alphasignal_url:
        env.update(ALPHASIGNAL_URL=alphasignal_url, ALPHASIGNAL_ENABLED="true")
    if simulator:
        env.update(ALPACA_BASE_URL=simulator, ALPACA_DATA_URL=simulator)
        if not alphasignal_url:  # broker mechanics only
            env["ALPHASIGNAL_ENABLED"] = "false"
    if mutation_attempt:  # simulator only: let AlphaLive try, the guard refuses
        env["DRY_RUN"] = "false"
    enabled = env.get("ALPHASIGNAL_ENABLED", "true").strip().lower() not in (
        "false",
        "0",
        "no",
    )
    if enabled and not env.get("ALPHASIGNAL_URL", "").strip():
        raise Abort(
            "sentiment is enabled but ALPHASIGNAL_URL is not set - pass "
            "--alphasignal-url (e.g. http://127.0.0.1:8010) or set it in the env file"
        )
    return env


def effective_settings(env: dict) -> list[str]:
    url = env.get("ALPHASIGNAL_URL", "")
    dry = env["DRY_RUN"] == "true"
    return [
        f"broker: paper=true, trading host={urlsplit(env['ALPACA_BASE_URL']).hostname}, "
        f"data host={urlsplit(env.get('ALPACA_DATA_URL', 'https://data.alpaca.markets')).hostname}, "
        f"feed={env.get('ALPACA_DATA_FEED', 'iex')}",
        f"DRY_RUN={'true' if dry else 'false (simulator mutation-attempt check)'}, "
        "read-only transport=on, Telegram=off, health server=ephemeral port",
        f"sentiment: enabled={env.get('ALPHASIGNAL_ENABLED', 'true')}, "
        f"fail_policy={env.get('ALPHASIGNAL_FAIL_POLICY', 'open')}, "
        f"timeout={env.get('ALPHASIGNAL_TIMEOUT_SECONDS', '6.0')}s, "
        f"threshold={env.get('ALPHASIGNAL_SENTIMENT_THRESHOLD', '-0.3')}, "
        f"service={urlsplit(url).hostname}:{urlsplit(url).port}",
        f"bear-market filter={env.get('ENABLE_BEAR_MARKET_FILTER', 'true')}",
    ]


def run_session(env, run_dir, config, tag, seconds) -> Path:
    log = run_dir / f"{tag}.log"
    cmd = [sys.executable, str(ALPHALIVE / "run.py"), "--config", str(config)]
    if env["DRY_RUN"] == "true":
        cmd.append("--dry-run")
    with open(log, "w") as fh:
        proc = subprocess.Popen(
            cmd, cwd=run_dir, env=env, stdout=fh, stderr=subprocess.STDOUT
        )
        try:
            proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    return log


_MONEY = re.compile(r"\$-?[0-9][0-9,]*(\.[0-9]+)?")
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_KEYLIKE = re.compile(r"\b(PK|AK)[A-Z0-9]{8,}\b")
_QTY = re.compile(r"\b(BUY|SELL) [0-9][0-9.]*\b")
_PRIVATE = re.compile(
    r"Equity|Cash|Buying Power|Portfolio Value|Day Trade Count|Account number|account_id",
    re.I,
)
_SHOW = re.compile(
    r"Account Status|ALPACA CONNECTION|READ-ONLY|Fetched \d+ bars|Dropping incomplete|"
    r"incomplete bar|SIGNAL CHECK|SIGNAL: |Signal: |Indicators: |Would execute|Would SELL|"
    r"Dry run: no order|Trade blocked|blocked|BLOCKED|already completed today|Sentiment|"
    r"sentiment|AlphaSignal|market|Market|ERROR|CRITICAL|Traceback|drawdown_halt|predates|"
    r"Mode:|New trading day|Waiting|sleep|rejected|Order"
)


def redact(line: str) -> str:
    line = _KEYLIKE.sub("<key>", _UUID.sub("<id>", _MONEY.sub("$<redacted>", line)))
    return _QTY.sub(r"\1 <qty>", line).rstrip()


def summarize(log: Path, tag: str) -> dict:
    lines = log.read_text(errors="replace").splitlines()
    allowed, refused = Counter(), Counter()
    for ln in lines:
        m = re.search(r"Broker request \(read-only\): (\w+) (\S+)", ln)
        if m:
            path = re.sub(r"/[A-Z]{1,5}(?=/|$)", "/{symbol}", m.group(2))
            allowed[(m.group(1), path)] += 1
        m = re.search(
            r"BROKER MUTATION BLOCKED \(read-only transport\): (\w+) (\S+)", ln
        )
        if m:
            refused[(m.group(1), m.group(2))] += 1
        elif "BROKER MUTATION BLOCKED" in ln:
            refused[("?", "?")] += 1
    print(f"\n===== {tag}: {log.name} ({len(lines)} log lines)")
    print("Alpaca requests sent (all GET):")
    for (meth, path), n in sorted(allowed.items()):
        print(f"  {n:4d}  {meth} {path}")
    if refused:
        print(
            "Mutation ATTEMPTED and refused by the read-only transport (nothing sent):"
        )
        for (meth, path), n in sorted(refused.items()):
            print(f"  {n:4d}  {meth} {path}")
    else:
        print("Mutation attempts: none (no non-GET request was attempted)")
    print("Selected log lines (redacted):")
    shown = 0
    for ln in lines:
        msg = ln.split("]", 1)[-1]
        if _SHOW.search(ln) and ("Account Status" in ln or not _PRIVATE.search(msg)):
            print("  " + redact(ln)[:220])
            shown += 1
            if shown >= 60:
                print("  ... (truncated)")
                break
    return {"allowed": allowed, "refused": refused}


def probe(ticker: str) -> None:
    """Market-data probe (runs in a child with the same env): completed-bar
    selection, signal price (last completed adjusted close) vs the latest raw
    trade, and timestamps. Public market data only."""
    sys.path.insert(0, str(ALPHALIVE))
    from alpaca.data.requests import StockLatestTradeRequest

    from alphalive.data.market_data import MarketDataFetcher

    md = MarketDataFetcher(
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
    )
    now = datetime.now(ET)
    bars = md.get_latest_bars(ticker, "1Day", lookback_bars=30)
    print(f"probe at {now:%Y-%m-%d %H:%M:%S} ET")
    print("last completed bars (index tz, close):")
    for ts, row in bars.tail(3).iterrows():
        close = row["close"] if "close" in row else row["Close"]
        print(f"  {ts}  close={close:.2f}")
    print(f"today's bar included: {bars.index[-1].tz_convert(ET).date() == now.date()}")
    trade = md.client.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=ticker)
    )[ticker]
    print(
        f"latest raw trade: {trade.price:.2f} at "
        f"{trade.timestamp.astimezone(ET):%Y-%m-%d %H:%M:%S} ET"
    )
    print(f"get_current_price(): {md.get_current_price(ticker):.2f}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--env-file", type=Path, default=ALPHALIVE / ".env")
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "alphalive_paper_check",
        help="parent of the per-run directory (logs + disposable state)",
    )
    ap.add_argument("--first-seconds", type=int, default=480)
    ap.add_argument("--restart-seconds", type=int, default=120)
    ap.add_argument(
        "--simulator", help="local simulator URL (mechanics check, no credentials)"
    )
    ap.add_argument("--simulate-mutation-attempt", action="store_true")
    ap.add_argument(
        "--alphasignal-url",
        help="AlphaSignal base URL for this run (overrides the env file)",
    )
    ap.add_argument("--probe", metavar="TICKER", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.probe:
        if os.environ.get("ALPHALIVE_BROKER_READ_ONLY") != "true":
            raise Abort("probe must run with the read-only transport armed")
        probe(args.probe)
        return
    for s in (args.first_seconds, args.restart_seconds):
        if not 0 < s <= MAX_SESSION_SECONDS:
            raise Abort(f"session length must be 1..{MAX_SESSION_SECONDS} s")
    if not args.config.is_file():
        raise Abort(f"strategy config not found: {args.config}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    run_dir = (
        args.out_dir / ("run_" + datetime.now(ET).strftime("%Y%m%d_%H%M%S"))
    ).resolve()
    run_dir.mkdir()
    for d in [run_dir, *run_dir.parents]:
        if (d / ".env").exists():
            raise Abort(f"a .env exists in {d}; choose another --out-dir")
    settings = read_settings(args.env_file, args.simulator)
    env = build_env(
        settings,
        run_dir,
        args.simulator,
        args.simulate_mutation_attempt,
        alphasignal_url=args.alphasignal_url,
    )
    print(f"Run directory: {run_dir}")
    print(f"Started {datetime.now(ET):%Y-%m-%d %H:%M:%S} ET")
    for s in effective_settings(env):
        print("  " + s)

    log1 = run_session(env, run_dir, args.config, "session1", args.first_seconds)
    summarize(log1, "session 1")
    time.sleep(2)
    log2 = run_session(
        env, run_dir, args.config, "session2_restart", args.restart_seconds
    )
    summarize(log2, "session 2 (same-day restart)")
    print("\n===== market-data probe (AlphaLive MarketDataFetcher, read-only)")
    ticker = __import__("json").loads(args.config.read_text())["ticker"]
    try:
        res = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--probe", ticker],
            cwd=run_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = res.stdout + (res.stderr[-1500:] if res.returncode else "")
    except subprocess.TimeoutExpired:
        out = "probe timed out after 120 s"
    for ln in out.splitlines():
        if "Broker request" in ln or "READ-ONLY" in ln:
            continue
        print("  " + redact(ln))
    print(
        f"\nFinished {datetime.now(ET):%Y-%m-%d %H:%M:%S} ET; no process left running."
    )


if __name__ == "__main__":
    main()
