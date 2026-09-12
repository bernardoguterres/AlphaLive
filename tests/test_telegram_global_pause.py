"""
Tests for multi-strategy Telegram pause/resume (objective 3 of the
2026-09-11 hardening pass).

Before this fix, TelegramCommandListener only ever held one strategy's
RiskManager, so /pause set trading_paused_manual on that one instance while
every OTHER strategy's RiskManager.can_trade() kept allowing trades - /pause
looked global to the operator but wasn't. This verifies the fix: /pause and
/resume now route through a shared GlobalRiskManager that every registered
RiskManager consults.

Uses real RiskManager/GlobalRiskManager/BotState instances (not mocks) since
this is exactly the integration point that was broken - a mock risk_manager
would hide it. No real Telegram calls: the notifier is a Mock throughout.
"""

from unittest.mock import Mock

import pytest

from alphalive.execution.risk_manager import GlobalRiskManager, RiskManager
from alphalive.notifications.telegram_commands import TelegramCommandListener
from alphalive.state import BotState
from alphalive.strategy_schema import Execution, Risk, SafetyLimits

from tests.test_order_manager import sample_config  # noqa: F401


def _make_risk_manager(ticker: str, global_risk=None) -> RiskManager:
    return RiskManager(
        risk_config=Risk(
            stop_loss_pct=2.0,
            take_profit_pct=5.0,
            max_position_size_pct=10.0,
            max_daily_loss_pct=3.0,
            max_open_positions=5,
            portfolio_max_positions=10,
        ),
        execution_config=Execution(
            order_type="market", limit_offset_pct=0.1, cooldown_bars=1
        ),
        strategy_name=ticker,
        safety_limits=SafetyLimits(),
        global_risk=global_risk,
    )


def _listener(order_manager, risk_manager, config, global_risk, notifier=None):
    return TelegramCommandListener(
        bot_token="test_token",
        chat_id="123456",
        order_manager=order_manager,
        risk_manager=risk_manager,
        broker=Mock(),
        notifier=notifier or Mock(),
        config=config,
        global_risk=global_risk,
    )


@pytest.fixture
def bot_state(tmp_path):
    return BotState(state_file=str(tmp_path / "state.json"))


# ---------------------------------------------------------------------------
# One configured strategy
# ---------------------------------------------------------------------------


def test_single_strategy_global_pause_blocks_it(sample_config, bot_state):
    global_risk = GlobalRiskManager(bot_state=bot_state)
    rm = _make_risk_manager("AAPL", global_risk=global_risk)
    global_risk.register_strategy("AAPL", rm)

    listener = _listener(Mock(), rm, sample_config, global_risk)
    listener._handle_command("/pause")

    can_trade, reason = rm.can_trade(
        ticker="AAPL",
        signal="BUY",
        account_equity=100000,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    assert can_trade is False
    assert "Telegram" in reason


# ---------------------------------------------------------------------------
# Multiple strategies: /pause must block ALL of them, not just the one the
# listener happens to hold a direct RiskManager reference to.
# ---------------------------------------------------------------------------


def test_multi_strategy_pause_blocks_every_strategy(sample_config, bot_state):
    global_risk = GlobalRiskManager(bot_state=bot_state)
    rm_aapl = _make_risk_manager("AAPL", global_risk=global_risk)
    rm_msft = _make_risk_manager("MSFT", global_risk=global_risk)
    rm_spy = _make_risk_manager("SPY", global_risk=global_risk)
    for ticker, rm in (("AAPL", rm_aapl), ("MSFT", rm_msft), ("SPY", rm_spy)):
        global_risk.register_strategy(ticker, rm)

    # Listener was constructed with only AAPL's components (mirrors
    # main.py's "first_ticker" wiring) - the bug this test guards against.
    listener = _listener(Mock(), rm_aapl, sample_config, global_risk)
    listener._handle_command("/pause")

    for ticker, rm in (("AAPL", rm_aapl), ("MSFT", rm_msft), ("SPY", rm_spy)):
        can_trade, reason = rm.can_trade(
            ticker=ticker,
            signal="BUY",
            account_equity=100000,
            current_positions_count=0,
            total_portfolio_positions=0,
        )
        assert can_trade is False, f"{ticker} should be paused globally"


def test_no_configured_strategies_pause_does_not_crash(sample_config, bot_state):
    global_risk = GlobalRiskManager(bot_state=bot_state)  # nothing registered
    listener = _listener(Mock(), Mock(), sample_config, global_risk)
    listener._handle_command("/pause")
    assert global_risk.is_manual_paused() is True
    listener._handle_command("/resume")
    assert global_risk.is_manual_paused() is False


# ---------------------------------------------------------------------------
# Resume must not override another active halt
# ---------------------------------------------------------------------------


def test_resume_reports_remaining_circuit_breaker_halt(sample_config, bot_state):
    global_risk = GlobalRiskManager(bot_state=bot_state)
    rm = _make_risk_manager("AAPL", global_risk=global_risk)
    global_risk.register_strategy("AAPL", rm)
    rm.trading_paused_by_circuit_breaker = True  # independent halt

    notifier = Mock()
    listener = _listener(Mock(), rm, sample_config, global_risk, notifier=notifier)
    listener._handle_command("/pause")
    listener._handle_command("/resume")

    assert global_risk.is_manual_paused() is False  # telegram pause cleared
    can_trade, reason = rm.can_trade(
        ticker="AAPL",
        signal="BUY",
        account_equity=100000,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    assert can_trade is False  # still halted - circuit breaker independent of telegram
    assert "circuit breaker" in reason.lower()

    last_message = notifier.send_message.call_args[0][0]
    assert "still halted" in last_message.lower()
    assert "circuit breaker" in last_message.lower()


def test_resume_reports_env_var_halt(sample_config, bot_state, monkeypatch):
    monkeypatch.setenv("TRADING_PAUSED", "true")
    global_risk = GlobalRiskManager(bot_state=bot_state)
    rm = _make_risk_manager("AAPL", global_risk=global_risk)
    global_risk.register_strategy("AAPL", rm)

    notifier = Mock()
    listener = _listener(Mock(), rm, sample_config, global_risk, notifier=notifier)
    listener._handle_command("/pause")
    listener._handle_command("/resume")

    last_message = notifier.send_message.call_args[0][0]
    assert "TRADING_PAUSED" in last_message


def test_resume_with_no_other_halts_reports_resumed(sample_config, bot_state):
    global_risk = GlobalRiskManager(bot_state=bot_state)
    rm = _make_risk_manager("AAPL", global_risk=global_risk)
    global_risk.register_strategy("AAPL", rm)

    notifier = Mock()
    listener = _listener(Mock(), rm, sample_config, global_risk, notifier=notifier)
    listener._handle_command("/pause")
    listener._handle_command("/resume")

    last_message = notifier.send_message.call_args[0][0]
    assert "Trading Resumed" in last_message
    can_trade, _ = rm.can_trade(
        ticker="AAPL",
        signal="BUY",
        account_equity=100000,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    assert can_trade is True


# ---------------------------------------------------------------------------
# Repeated pause/resume
# ---------------------------------------------------------------------------


def test_repeated_pause_resume_is_idempotent(sample_config, bot_state):
    global_risk = GlobalRiskManager(bot_state=bot_state)
    rm = _make_risk_manager("AAPL", global_risk=global_risk)
    global_risk.register_strategy("AAPL", rm)
    listener = _listener(Mock(), rm, sample_config, global_risk)

    listener._handle_command("/pause")
    listener._handle_command("/pause")
    assert global_risk.is_manual_paused() is True

    listener._handle_command("/resume")
    listener._handle_command("/resume")
    assert global_risk.is_manual_paused() is False


# ---------------------------------------------------------------------------
# SELL exemption preserved under global pause (existing bug 2.6 semantics)
# ---------------------------------------------------------------------------


def test_global_pause_still_exempts_sells(sample_config, bot_state):
    global_risk = GlobalRiskManager(bot_state=bot_state)
    rm = _make_risk_manager("AAPL", global_risk=global_risk)
    global_risk.register_strategy("AAPL", rm)
    global_risk.set_manual_pause("test")

    can_trade, _ = rm.can_trade(
        ticker="AAPL",
        signal="SELL",
        account_equity=100000,
        current_positions_count=1,
        total_portfolio_positions=1,
    )
    assert can_trade is True


# ---------------------------------------------------------------------------
# Restart behavior: persisted pause survives a fresh GlobalRiskManager
# ---------------------------------------------------------------------------


def test_pause_persists_across_restart(sample_config, tmp_path):
    path = str(tmp_path / "state.json")
    state1 = BotState(state_file=path)
    global_risk1 = GlobalRiskManager(bot_state=state1)
    rm1 = _make_risk_manager("AAPL", global_risk=global_risk1)
    global_risk1.register_strategy("AAPL", rm1)
    global_risk1.set_manual_pause("Paused via Telegram /pause command")

    # Simulate restart: fresh BotState + fresh GlobalRiskManager reading the
    # same file.
    state2 = BotState(state_file=path)
    global_risk2 = GlobalRiskManager(bot_state=state2)
    assert global_risk2.is_manual_paused() is True

    rm2 = _make_risk_manager("AAPL", global_risk=global_risk2)
    global_risk2.register_strategy("AAPL", rm2)
    can_trade, _ = rm2.can_trade(
        ticker="AAPL",
        signal="BUY",
        account_equity=100000,
        current_positions_count=0,
        total_portfolio_positions=0,
    )
    assert can_trade is False


def test_no_pause_does_not_persist_true(sample_config, tmp_path):
    path = str(tmp_path / "state.json")
    state1 = BotState(state_file=path)
    GlobalRiskManager(bot_state=state1)  # never paused

    state2 = BotState(state_file=path)
    global_risk2 = GlobalRiskManager(bot_state=state2)
    assert global_risk2.is_manual_paused() is False
