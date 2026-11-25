"""Dashboard strategy panel: /api/strategies and the payload's `strategies`
describe the loaded config in plain words, the engine state and the bot's
last recorded decision."""

import json

import pytest

import dashboard.server as dashboard_server
from alphalive.state import BotState

CONFIG = "configs/production/bollinger_rsi_SPY_15Min.json"


@pytest.fixture
def client(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    bs = BotState(state_file=str(state_file))
    bs.record_signal("SPY", "HOLD", "No setup: price in range")
    bs.save_engine_state("SPY", {"in_position": True, "shadow_position": True})
    monkeypatch.setenv("STATE_FILE", str(state_file))
    monkeypatch.setenv("STRATEGY_CONFIG", CONFIG)
    monkeypatch.setattr(dashboard_server, "_strategies", None)
    monkeypatch.setattr(dashboard_server, "_PASSWORD", "")
    from fastapi.testclient import TestClient

    return TestClient(dashboard_server.app)


def test_api_strategies_describes_the_running_config(client):
    data = client.get("/api/strategies").json()
    assert data["count"] == 1
    s = data["strategies"][0]
    assert (s["name"], s["ticker"], s["timeframe"]) == (
        "bollinger_rsi_combo",
        "SPY",
        "15Min",
    )
    assert "lower Bollinger band" in s["rules"]["entry"]
    assert "RSI(14) below 45" in s["rules"]["entry"]
    assert "middle band" in s["rules"]["exit"] and "55" in s["rules"]["exit"]
    assert s["risk"]["stop_loss_pct"] == 1.5
    assert s["risk"]["max_trades_per_day"] == 3
    assert "strategy_type" not in s["parameters"]
    assert s["engine"] == {"in_position": True, "shadow_position": True}
    assert s["last_signal"]["signal"] == "HOLD"


def test_no_config_means_empty_list(monkeypatch, client):
    monkeypatch.delenv("STRATEGY_CONFIG")
    monkeypatch.setattr(dashboard_server, "_strategies", None)
    assert client.get("/api/strategies").json() == {"strategies": [], "count": 0}


@pytest.mark.parametrize(
    "name",
    [
        "ma_crossover",
        "rsi_mean_reversion",
        "momentum_breakout",
        "bollinger_breakout",
        "vwap_reversion",
        "trend_adaptive_rsi",
        "greenblatt_weekly",
        "bollinger_rsi_combo",
    ],
)
def test_every_strategy_has_plain_rules(name):
    rules = dashboard_server._strategy_rules(name, {})
    assert rules["entry"] != "See parameters" and rules["exit"] != "See parameters"


def test_record_signal_persists(tmp_path):
    path = tmp_path / "s.json"
    BotState(state_file=str(path)).record_signal("SPY", "BUY", "x" * 500)
    saved = json.loads(path.read_text())["last_signals"]["SPY"]
    assert saved["signal"] == "BUY" and len(saved["reason"]) == 300 and saved["at"]
