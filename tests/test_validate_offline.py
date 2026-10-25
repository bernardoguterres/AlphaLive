"""run.py --validate-only is offline: exercised through the real entry point
in a subprocess whose credential reads, .env loads, Alpaca clients and
network connections all raise and are recorded (tests/fixtures/offline_guard)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "tests" / "fixtures" / "offline_guard"
GOOD = ROOT / "configs" / "paper_check" / "SPY_bollinger_breakout.json"


def _run(tmp_path, *args):
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    (cwd / ".env").write_text("ALPACA_API_KEY=PKDUMMYDUMMY\nALPACA_SECRET_KEY=x\n")
    log = tmp_path / "guard.log"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ALPACA_", "STRATEGY_", "PERSISTENT"))
    }
    env.update(
        ALPACA_API_KEY="PKSHELLDUMMY",
        ALPACA_SECRET_KEY="x",
        GUARD_LOG=str(log),
        PYTHONPATH=str(GUARD),
    )
    p = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    hits = log.read_text() if log.exists() else ""
    return p.returncode, p.stdout + p.stderr, hits


def _variant(tmp_path, name, mutate):
    d = json.loads(GOOD.read_text())
    mutate(d)
    p = tmp_path / name
    p.write_text(json.dumps(d))
    return p


def test_valid_config_passes_without_credentials_or_network(tmp_path):
    rc, out, hits = _run(tmp_path, "--validate-only", "--config", str(GOOD))
    assert rc == 0, out
    assert "VALID" in out and hits == ""


@pytest.mark.parametrize(
    "mutate, expect",
    [
        (lambda d: d["risk"].update(drawdown_halt_enabled=True), "could not load"),
        (lambda d: d.pop("ticker"), "could not load"),
        (
            lambda d: d["risk"].update(trailing_stop_enabled=True),
            "PERSISTENT_STORAGE",
        ),
    ],
    ids=["halt-enabled", "no-ticker", "trailing-without-storage"],
)
def test_invalid_config_fails_without_credentials_or_network(tmp_path, mutate, expect):
    cfg = _variant(tmp_path, "c.json", mutate)
    rc, out, hits = _run(tmp_path, "--validate-only", "--config", str(cfg))
    assert rc == 1 and expect in out and hits == "", out


def test_guard_is_live_for_a_normal_start(tmp_path):
    rc, out, hits = _run(tmp_path, "--config", str(GOOD), "--dry-run")
    assert rc != 0 and "dotenv.load_dotenv" in hits
