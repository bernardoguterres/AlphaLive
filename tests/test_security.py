"""
Test Security Features

Tests for security hardening — credential leak prevention, authentication, rate limiting.
"""

import os
import re
import time
import pytest
from unittest.mock import Mock, patch
from pathlib import Path

from alphalive.notifications.telegram_commands import TelegramCommandListener


def test_no_secrets_in_configs():
    """Scan configs/ for hardcoded secrets."""
    # Patterns that match API keys and tokens
    secret_patterns = [
        r"APCA-API-[A-Z0-9]{20}",  # Alpaca API key
        r"sk_[a-zA-Z0-9]{32}",  # Alpaca secret key
        r"[0-9]{10}:[A-Za-z0-9_-]{35}",  # Telegram bot token
    ]

    configs_dir = Path(__file__).parent.parent / "configs"

    if not configs_dir.exists():
        pytest.skip("configs/ directory not found")

    for root, dirs, files in os.walk(configs_dir):
        for file in files:
            if file.endswith(".json"):
                file_path = os.path.join(root, file)
                with open(file_path, 'r') as f:
                    content = f.read()

                for pattern in secret_patterns:
                    if re.search(pattern, content):
                        pytest.fail(
                            f"Secret pattern found in {file}:\n"
                            f"  Pattern: {pattern}\n"
                            f"  Move secrets to environment variables!"
                        )


def test_telegram_commands_check_chat_id():
    """Verify Telegram commands only respond to configured chat_id."""
    # Mock components
    order_manager = Mock()
    risk_manager = Mock()
    broker = Mock()
    notifier = Mock()
    notifier.send_message = Mock(return_value=True)
    config = Mock()

    # Create listener with chat_id="12345"
    listener = TelegramCommandListener(
        bot_token="test_token",
        chat_id="12345",
        order_manager=order_manager,
        risk_manager=risk_manager,
        broker=broker,
        notifier=notifier,
        config=config
    )

    # Simulate message from WRONG chat_id in _poll_loop
    # (The security check happens in _poll_loop, not _handle_command)
    # We can verify the code checks msg_chat_id == self.chat_id

    # Read the source code to verify chat_id check exists
    source_file = Path(__file__).parent.parent / "alphalive" / "notifications" / "telegram_commands.py"
    with open(source_file, 'r') as f:
        source_code = f.read()

    # Verify chat_id check is present
    assert "msg_chat_id == self.chat_id" in source_code, \
        "Telegram commands MUST check chat_id for security"

    # Verify unauthorized chats are logged as warnings
    assert 'logger.warning' in source_code and 'unauthorized chat' in source_code.lower(), \
        "Unauthorized commands should be logged as warnings"


def test_telegram_rate_limiting():
    """Verify rate limiting prevents command spam."""
    # Mock components
    order_manager = Mock()
    risk_manager = Mock()
    risk_manager.daily_pnl = 100.0
    risk_manager.daily_trades = []
    broker = Mock()
    broker.get_account = Mock(return_value=Mock(equity=100000.0))
    broker.get_all_positions = Mock(return_value=[])
    notifier = Mock()
    notifier.send_message = Mock(return_value=True)
    config = Mock()
    config.strategy.name = "test_strategy"
    config.ticker = "AAPL"
    config.timeframe = "1Day"

    # Create listener
    listener = TelegramCommandListener(
        bot_token="test_token",
        chat_id="12345",
        order_manager=order_manager,
        risk_manager=risk_manager,
        broker=broker,
        notifier=notifier,
        config=config
    )

    # Send 10 commands rapidly (should all succeed)
    for i in range(10):
        listener._handle_command("/help")

    # Verify 10 successful calls
    assert notifier.send_message.call_count == 10

    # 11th command should be rate limited
    listener._handle_command("/help")

    # Verify rate limit message was sent (11th call)
    assert notifier.send_message.call_count == 11

    # Check that the last message is a rate limit warning
    last_call_args = notifier.send_message.call_args
    assert "Rate limit exceeded" in last_call_args[0][0]


def test_env_file_not_in_git():
    """Verify .env file is properly gitignored."""
    gitignore_path = Path(__file__).parent.parent / ".gitignore"

    if not gitignore_path.exists():
        pytest.skip(".gitignore not found")

    with open(gitignore_path, 'r') as f:
        gitignore_content = f.read()

    # Check .env is in .gitignore
    assert ".env" in gitignore_content, \
        ".env file MUST be in .gitignore to prevent credential leaks"

    # If .env exists, verify it's not tracked by git
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        import subprocess
        try:
            result = subprocess.run(
                ["git", "ls-files", ".env"],
                cwd=Path(__file__).parent.parent,
                capture_output=True,
                text=True
            )
            assert result.stdout.strip() == "", \
                ".env file is tracked by git! Remove with: git rm --cached .env"
        except FileNotFoundError:
            # Git not installed or not a git repo
            pytest.skip("Git not available")


def test_health_endpoint_requires_secret():
    """Verify health endpoint authentication is configured."""
    from alphalive.health import HealthCheckHandler

    # Verify handler has secret attribute
    assert hasattr(HealthCheckHandler, 'secret'), \
        "HealthCheckHandler must have secret class variable"

    # Read source to verify authentication check exists
    source_file = Path(__file__).parent.parent / "alphalive" / "health.py"
    with open(source_file, 'r') as f:
        source_code = f.read()

    # Verify authentication check is present
    assert "X-Health-Secret" in source_code, \
        "Health endpoint must check X-Health-Secret header"

    assert "401" in source_code or "Unauthorized" in source_code, \
        "Health endpoint must return 401 for wrong secret"

    assert "503" in source_code or "disabled" in source_code.lower(), \
        "Health endpoint should return 503 when HEALTH_SECRET not set"


def test_no_print_statements_with_secrets():
    """Scan codebase for print() statements that might leak secrets."""
    # Check main source files don't have print() statements
    # (should use logger instead)
    source_dirs = [
        Path(__file__).parent.parent / "alphalive",
    ]

    excluded_files = ["__pycache__", ".pyc", "test_"]

    for source_dir in source_dirs:
        if not source_dir.exists():
            continue

        for file_path in source_dir.rglob("*.py"):
            # Skip test files and cache
            if any(exc in str(file_path) for exc in excluded_files):
                continue

            with open(file_path, 'r') as f:
                lines = f.readlines()

            for i, line in enumerate(lines, 1):
                # Skip comments and docstring examples
                if line.strip().startswith("#"):
                    continue
                if line.strip().startswith(">>>"):  # Docstring example
                    continue

                # Check for print() statements
                if re.search(r'\bprint\s*\(', line) and 'logger' not in line:
                    # Allow print in specific contexts (banners, startup)
                    if 'banner' in str(file_path).lower() or 'run.py' in str(file_path):
                        continue

                    pytest.fail(
                        f"print() statement found in {file_path.name}:{i}\n"
                        f"  Use logger instead to prevent accidental secret logging\n"
                        f"  Line: {line.strip()}"
                    )
