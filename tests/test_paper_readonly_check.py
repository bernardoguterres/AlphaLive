"""Safety properties of scripts/paper_readonly_check.py (no network)."""

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "paper_readonly_check.py"
_spec = importlib.util.spec_from_file_location("paper_readonly_check", _PATH)
prc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prc)

PAPER = {
    "ALPACA_API_KEY": "PKTESTKEY123",
    "ALPACA_SECRET_KEY": "s",
    "ALPHASIGNAL_URL": "http://127.0.0.1:8010",
}
SHELL = {
    "PATH": "/usr/bin",
    "ALPACA_BASE_URL": "https://api.alpaca.markets",
    "ALPACA_DATA_URL": "https://elsewhere.example",
    "TELEGRAM_BOT_TOKEN": "t",
    "TELEGRAM_CHAT_ID": "1",
    "STRATEGY_CONFIG_DIR": "/somewhere",
    "PORT": "8080",
    "DRY_RUN": "false",
    "ALPHALIVE_BROKER_READ_ONLY": "false",
}


def test_env_forces_paper_dry_run_guard_and_strips_shell_overrides(tmp_path):
    env = prc.build_env(PAPER, tmp_path, base_env=SHELL)
    assert env["ALPACA_BASE_URL"] == "https://paper-api.alpaca.markets"
    assert env["ALPACA_PAPER"] == "true"
    assert env["DRY_RUN"] == "true"
    assert env["ALPHALIVE_BROKER_READ_ONLY"] == "true"
    assert env["STATE_FILE"] == str(tmp_path / "state.json")
    assert env["HEALTH_PORT"] == "0"
    for gone in (
        "ALPACA_DATA_URL",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "STRATEGY_CONFIG_DIR",
        "PORT",
    ):
        assert gone not in env
    assert env["PATH"] == "/usr/bin"


@pytest.mark.parametrize(
    "settings",
    [
        {"ALPACA_API_KEY": "AKLIVEKEY123", "ALPACA_SECRET_KEY": "s"},
        {**PAPER, "ALPACA_PAPER": "false"},
        {**PAPER, "ALPACA_BASE_URL": "https://api.alpaca.markets"},
        {"ALPACA_API_KEY": "PKTESTKEY123"},
    ],
    ids=["live-key", "paper-false", "live-base-url", "no-secret"],
)
def test_refuses_anything_not_clearly_paper(settings, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        prc.build_env(settings, tmp_path, base_env={})
    assert exc.value.code == 2
    assert "ABORTED before contacting Alpaca" in capsys.readouterr().out


def test_mutation_attempt_mode_requires_the_simulator(tmp_path):
    with pytest.raises(SystemExit):
        prc.build_env(PAPER, tmp_path, mutation_attempt=True, base_env={})
    env = prc.build_env(
        PAPER, tmp_path, "http://127.0.0.1:1", mutation_attempt=True, base_env={}
    )
    assert env["DRY_RUN"] == "false"
    assert env["ALPHALIVE_BROKER_READ_ONLY"] == "true"  # the guard stays armed


def test_sentiment_settings_pass_through_unchanged(tmp_path):
    settings = {
        **PAPER,
        "ALPHASIGNAL_FAIL_POLICY": "closed",
        "ALPHASIGNAL_SENTIMENT_THRESHOLD": "-0.5",
    }
    env = prc.build_env(settings, tmp_path, base_env={"ALPHASIGNAL_FAIL_POLICY": "x"})
    assert env["ALPHASIGNAL_FAIL_POLICY"] == "closed"
    assert env["ALPHASIGNAL_SENTIMENT_THRESHOLD"] == "-0.5"


def test_shell_credentials_take_precedence_and_only_named_keys_are_read(
    tmp_path, monkeypatch
):
    env_file = tmp_path / ".env"
    env_file.write_text("ALPACA_API_KEY=PKFROMFILE1\nALPACA_SECRET_KEY=f\nOTHER=x\n")
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    from_file = prc.read_settings(env_file, None)
    assert from_file["ALPACA_API_KEY"] == "PKFROMFILE1"
    assert "OTHER" not in from_file
    monkeypatch.setenv("ALPACA_API_KEY", "PKFROMSHELL1")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "sh")
    assert prc.read_settings(env_file, None)["ALPACA_API_KEY"] == "PKFROMSHELL1"


def test_simulator_mode_reads_no_credentials(tmp_path):
    assert prc.read_settings(tmp_path / "missing.env", "http://127.0.0.1:1") == {
        "ALPACA_API_KEY": "PKSIMULATOR",
        "ALPACA_SECRET_KEY": "sim",
    }


def test_redaction_hides_money_ids_keys_and_quantities():
    line = (
        "Would execute BUY 17 SPY at $525.60 order 0b6dd9d0-8b9b-48a9-ba46-"
        "b9d54906e415 key PKABCDEFGH1234"
    )
    out = prc.redact(line)
    assert "525.60" not in out and "17" not in out
    assert "<id>" in out and "<key>" in out and "PKABCDEFGH1234" not in out


def test_summary_distinguishes_refused_attempts_from_no_attempt(tmp_path, capsys):
    blocked = tmp_path / "a.log"
    blocked.write_text(
        "x Broker request (read-only): GET 127.0.0.1/v2/account\n"
        "x BROKER MUTATION BLOCKED (read-only transport): POST 127.0.0.1/v2/orders\n"
    )
    none = tmp_path / "b.log"
    none.write_text("x Broker request (read-only): GET 127.0.0.1/v2/clock\n")
    assert sum(prc.summarize(blocked, "a")["refused"].values()) == 1
    assert sum(prc.summarize(none, "b")["refused"].values()) == 0
    out = capsys.readouterr().out
    assert "Mutation ATTEMPTED and refused" in out
    assert "Mutation attempts: none" in out


def test_sentiment_without_an_explicit_url_aborts(tmp_path, capsys):
    no_url = {k: v for k, v in PAPER.items() if k != "ALPHASIGNAL_URL"}
    with pytest.raises(SystemExit):
        prc.build_env(no_url, tmp_path, base_env={})
    assert "ALPHASIGNAL_URL is not set" in capsys.readouterr().out
    env = prc.build_env(
        no_url, tmp_path, base_env={}, alphasignal_url="http://127.0.0.1:8010"
    )
    assert env["ALPHASIGNAL_URL"] == "http://127.0.0.1:8010"
    off = prc.build_env(
        {**no_url, "ALPHASIGNAL_ENABLED": "false"}, tmp_path, base_env={}
    )
    assert "ALPHASIGNAL_URL" not in off
