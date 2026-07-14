"""Regression tests for audit bug 2.1 (2026-07-13): three inconsistent
boolean-env-var parsers, each of which silently parsed whitespace-padded
truthy values (e.g. " true ") to False - flipping ALPACA_PAPER to live
trading, DRY_RUN to real orders, and TRADING_PAUSED to a fail-open kill
switch. Consolidated into alphalive.utils.env_bool.parse_bool_env /
read_bool_env, used by run.py, config.py, risk_manager.py, and health.py.
"""

import os

import pytest

from alphalive.utils.env_bool import parse_bool_env, read_bool_env


TRUTHY_INPUTS = [
    "true", "True", "TRUE", " true ", "true ", " true",
    "1", "yes", "Yes", "on", "ON",
]

FALSY_INPUTS = [
    "false", "False", "FALSE", " false ", "false ",
    "0", "no", "No", "off", "OFF",
]


@pytest.mark.parametrize("raw", TRUTHY_INPUTS)
def test_truthy_values_parse_true_regardless_of_whitespace_or_case(raw):
    assert parse_bool_env(raw, var_name="TEST_VAR", default=False) is True


@pytest.mark.parametrize("raw", FALSY_INPUTS)
def test_falsy_values_parse_false_regardless_of_whitespace_or_case(raw):
    assert parse_bool_env(raw, var_name="TEST_VAR", default=True) is False


def test_none_returns_default():
    assert parse_bool_env(None, var_name="TEST_VAR", default=True) is True
    assert parse_bool_env(None, var_name="TEST_VAR", default=False) is False


def test_empty_string_returns_default():
    assert parse_bool_env("", var_name="TEST_VAR", default=True) is True
    assert parse_bool_env("", var_name="TEST_VAR", default=False) is False


def test_garbage_raises_instead_of_silently_defaulting():
    """The core of bug 2.1: a malformed/ambiguous value must never silently
    resolve to a boolean (in either direction) - it must fail loudly."""
    with pytest.raises(ValueError, match="TRADING_PAUSED"):
        parse_bool_env("garbage", var_name="TRADING_PAUSED", default=False)


def test_whitespace_padded_true_does_not_silently_become_false():
    """The exact bug reported by the audit: ALPACA_PAPER=" true " (or
    DRY_RUN/TRADING_PAUSED) previously parsed to False via a bare
    `.lower() == "true"` comparison, since the untrimmed string never
    equals the literal "true"."""
    assert parse_bool_env(" true ", var_name="ALPACA_PAPER", default=True) is True
    assert parse_bool_env("true ", var_name="DRY_RUN", default=False) is True
    assert parse_bool_env(" true", var_name="TRADING_PAUSED", default=False) is True


def test_read_bool_env_reads_from_environment(monkeypatch):
    monkeypatch.setenv("SOME_TEST_FLAG", " TRUE ")
    assert read_bool_env("SOME_TEST_FLAG", default=False) is True

    monkeypatch.delenv("SOME_TEST_FLAG", raising=False)
    assert read_bool_env("SOME_TEST_FLAG", default=False) is False


def test_read_bool_env_raises_on_garbage(monkeypatch):
    monkeypatch.setenv("SOME_TEST_FLAG", "maybe")
    with pytest.raises(ValueError):
        read_bool_env("SOME_TEST_FLAG", default=False)


class TestCriticalFlagIntegration:
    """Bug 2.1's three specific named call sites: ALPACA_PAPER (run.py,
    config.py), DRY_RUN (run.py, config.py), TRADING_PAUSED
    (config.py, risk_manager.py, health.py) must all use the shared parser
    and resolve whitespace-padded/malformed input consistently.
    """

    def test_alpaca_paper_whitespace_does_not_silently_enable_live_trading(
        self, monkeypatch
    ):
        monkeypatch.setenv("ALPACA_PAPER", " true ")
        assert read_bool_env("ALPACA_PAPER", default=True) is True

    def test_dry_run_whitespace_does_not_silently_place_real_orders(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", " true ")
        assert read_bool_env("DRY_RUN", default=False) is True

    def test_trading_paused_whitespace_does_not_fail_open(self, monkeypatch):
        monkeypatch.setenv("TRADING_PAUSED", "true ")
        assert read_bool_env("TRADING_PAUSED", default=False) is True

    def test_trading_paused_on_is_consistently_accepted(self, monkeypatch):
        """Previously config.py's display parser accepted "on" as truthy
        while risk_manager.py's enforcement check did not - meaning the
        dashboard/logs could say "paused" while the bot kept trading. Both
        now go through the same parser."""
        monkeypatch.setenv("TRADING_PAUSED", "on")
        assert read_bool_env("TRADING_PAUSED", default=False) is True

    def test_risk_manager_blocks_trading_on_malformed_trading_paused(self, monkeypatch):
        """can_trade() must never raise out to the caller and must never
        silently treat a malformed TRADING_PAUSED as "not paused" - it
        blocks the trade and reports why."""
        from alphalive.execution.risk_manager import RiskManager
        from alphalive.strategy_schema import Risk, Execution, SafetyLimits

        monkeypatch.setenv("TRADING_PAUSED", "maybe")

        risk = RiskManager(
            strategy_name="test",
            risk_config=Risk(
                stop_loss_pct=2.0,
                take_profit_pct=5.0,
                max_position_size_pct=10.0,
                max_daily_loss_pct=3.0,
                max_open_positions=5,
                portfolio_max_positions=10,
                trailing_stop_enabled=False,
                commission_per_trade=0.0,
            ),
            execution_config=Execution(order_type="market", cooldown_bars=1),
            safety_limits=SafetyLimits(),
        )

        can_trade, reason = risk.can_trade(
            ticker="AAPL",
            signal="BUY",
            account_equity=100000.0,
            current_positions_count=0,
            total_portfolio_positions=0,
            current_bar=100,
        )
        assert can_trade is False
        assert "malformed" in reason.lower() or "TRADING_PAUSED" in reason
