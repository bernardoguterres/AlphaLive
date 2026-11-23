"""Pre-deploy validation (Nov 2026 audit L10/L11)."""

import shutil
import sys
from pathlib import Path


from alphalive.config import load_config_path, position_budget_issue

ROOT = Path(__file__).resolve().parent.parent
PROD = ROOT / "configs" / "production"


def _deploy_dir(tmp_path, names):
    for n in names:
        shutil.copy(PROD / n, tmp_path / n)
    return tmp_path


def test_position_budget_flags_over_100_percent(tmp_path):
    d = _deploy_dir(
        tmp_path, [f"ma_crossover_{t}.json" for t in ("AAPL", "MSFT", "QQQ", "SPY")]
    )
    msg = position_budget_issue(load_config_path(str(d)))
    assert msg and "120%" in msg


def test_position_budget_ok_at_or_under_100_percent(tmp_path):
    d = _deploy_dir(tmp_path, ["ma_crossover_SPY.json", "ma_crossover_QQQ.json"])
    assert position_budget_issue(load_config_path(str(d))) is None


def test_validate_offline_rejects_an_over_budget_deploy(tmp_path, capsys):
    sys.path.insert(0, str(ROOT))
    from run import validate_offline

    d = _deploy_dir(
        tmp_path, [f"ma_crossover_{t}.json" for t in ("AAPL", "MSFT", "QQQ", "SPY")]
    )
    assert validate_offline(str(d)) == 1
    assert "position sizes add up to 120%" in capsys.readouterr().out


def test_verify_deployment_runs_the_bots_offline_validation(monkeypatch):
    """It used to only load and print configs and said 'Safe to deploy' for
    configs/production, which the bot refuses (several strategies per ticker)."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import verify_deployment

    monkeypatch.setenv("STRATEGY_CONFIG_DIR", str(PROD))
    issues = verify_deployment.check_strategy_configs()
    assert issues and "Offline validation failed" in issues[0]
