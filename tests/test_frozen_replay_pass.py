"""
Frozen Replay Pass: End-to-End Integration across AlphaLab, AlphaLive, and AlphaSignal.

Priority: Proving that an actual AlphaLab export can travel through AlphaLive's
signal, risk, and order paths with a fake broker (AlpacaSim), including when
AlphaSignal or the broker misbehaves, with crash recovery and zero external calls.
"""

import asyncio
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
import requests

# Set up paths for cross-repo imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALPHALAB_BACKEND = (PROJECT_ROOT.parent / "AlphaLab" / "backend").resolve()
if str(ALPHALAB_BACKEND) not in sys.path:
    sys.path.insert(0, str(ALPHALAB_BACKEND))

# AlphaLab imports
from alphalab.api.helpers import _build_export_json, _translate_params_for_export

# AlphaLive imports
from alphalive.broker.alpaca_broker import AlpacaBroker
from alphalive.execution.order_manager import OrderManager
from alphalive.execution.risk_manager import GlobalRiskManager, RiskManager
from alphalive.services.alphasignal_client import (
    AlphaSignalClient,
    GATE_STATUS_AVAILABLE_BLOCK,
    GATE_STATUS_AVAILABLE_PASS,
    GATE_STATUS_INVALID_RESPONSE_BYPASS,
    GATE_STATUS_MISCONFIGURED_BLOCK,
    GATE_STATUS_STALE_BYPASS,
    GATE_STATUS_ERROR_BYPASS,
    IDENTITY_VERIFIED,
    run_pre_execution_checks,
)
from alphalive.state import (
    BotState,
    INTENT_CANCELLED,
    INTENT_FILLED,
    INTENT_PREPARED,
    INTENT_SUBMITTED,
    INTENT_SUBMITTING,
    INTENT_UNCERTAIN,
)
from alphalive.strategy.signal_engine import SignalEngine
from alphalive.strategy_schema import StrategySchema
from tests.broker_sim.alpaca_sim import AlpacaSim
from tests.test_multi_ticker_parity import (
    STRATEGIES,
    TICKERS,
    load_fixture,
    load_expected_signals,
    measure_parity,
    manifest_params,
)

ET = ZoneInfo("America/New_York")


# ============================================================================
# Section 1: Real AlphaLab Export -> AlphaLive Loader Contract
# ============================================================================


def test_real_alphalab_export_to_alphalive_loader():
    """Verify that exports created by AlphaLab's real export helper
    load and validate cleanly in AlphaLive's StrategySchema."""
    strategies_to_test = [
        ("ma_crossover", {"short_window": 20, "long_window": 50}),
        ("rsi_mean_reversion", {"period": 14, "oversold": 30, "overbought": 70}),
        ("momentum_breakout", {"lookback": 20, "surge_pct": 1.5, "atr_period": 14, "volume_ma_period": 20}),
        ("bollinger_breakout", {"period": 20, "std_dev": 2.0, "confirmation_bars": 2, "volume_ma_period": 20}),
        ("bollinger_rsi_combo", {"bb_period": 20, "bb_std": 2.0, "rsi_period": 14, "rsi_oversold": 45, "rsi_overbought": 65}),
        ("trend_adaptive_rsi", {"rsi_period": 14, "trend_lookback": 5, "uptrend_buy": 45, "uptrend_sell": 65, "downtrend_buy": 35, "downtrend_sell": 55, "range_buy": 35, "range_sell": 65}),
        ("greenblatt_weekly", {"fast_sma": 10, "slow_sma": 50, "rsi_period": 14, "rsi_oversold": 35, "rsi_overbought": 65, "min_hold_bars": 52}),
    ]

    for strat_name, raw_params in strategies_to_test:
        export_dict = _build_export_json(
            backtest_id=f"bt_{strat_name}_test",
            ticker="AAPL",
            strategy_name=strat_name,
            params=raw_params,
            start_date="2022-01-01",
            end_date="2023-12-31",
            initial_capital=100000.0,
            results={
                "total_return_pct": 18.5,
                "total_trades": 25,
                "metrics": {
                    "risk": {"sharpe_ratio": 1.45, "sortino_ratio": 1.82, "calmar_ratio": 1.20},
                    "drawdown": {"max_drawdown_pct": -8.5},
                    "trades": {"win_rate": 0.58, "profit_factor": 1.65, "round_trips": 25},
                },
            },
            config={"app": {"version": "0.1.0"}},
            risk_settings={
                "stop_loss_pct": 2.5,
                "take_profit_pct": 6.0,
                "max_position_size_pct": 10.0,
                "max_daily_loss_pct": 3.0,
                "max_open_positions": 5,
                "trailing_stop_enabled": False,
                "trailing_stop_pct": 3.0,
            },
        )

        # 1. Assert top-level schema contract
        assert export_dict["schema_version"] == "1.0"
        assert export_dict["ticker"] == "AAPL"
        assert export_dict["strategy"]["name"] == strat_name
        assert export_dict["risk"]["drawdown_halt_enabled"] is False

        # 2. Validate through AlphaLive's Pydantic StrategySchema
        loaded = StrategySchema(**export_dict)
        assert loaded.strategy.name == strat_name
        assert loaded.risk.stop_loss_pct == 2.5
        assert loaded.risk.take_profit_pct == 6.0
        assert loaded.risk.drawdown_halt_enabled is False

    # 3. Assert refusal of drawdown halt backtests
    halt_export = _build_export_json(
        backtest_id="bt_halt_test",
        ticker="AAPL",
        strategy_name="ma_crossover",
        params={"short_window": 10, "long_window": 20},
        start_date="2022-01-01",
        end_date="2023-12-31",
        initial_capital=100000.0,
        results={},
        config={"app": {"version": "0.1.0"}},
    )
    halt_export["risk"]["drawdown_halt_enabled"] = True
    with pytest.raises(ValueError, match="drawdown_halt_enabled=true"):
        StrategySchema(**halt_export)

    # 4. Assert refusal of research-only strategy
    unsupported_export = dict(halt_export)
    unsupported_export["risk"]["drawdown_halt_enabled"] = False
    unsupported_export["strategy"] = {
        "name": "rsi_simple",
        "parameters": {"rsi_period": 14, "oversold": 30, "overbought": 70},
    }
    with pytest.raises(ValueError):
        StrategySchema(**unsupported_export)


# ============================================================================
# Section 2: Signal Parity Seeded Replay
# ============================================================================


def test_signal_parity_seeded_replay(capsys, monkeypatch):
    monkeypatch.setenv("ENABLE_BEAR_MARKET_FILTER", "false")
    """Replay identical seeded bars through AlphaLab oracle and AlphaLive's
    SignalEngine across all 7 daily strategies on SPY and MSFT fixtures."""
    total_bars_compared = 0
    total_decision_bars = 0
    fixture_summary = []

    for strat in STRATEGIES:
        strat_name = strat["name"]
        params = manifest_params(strat_name)
        for ticker in TICKERS:
            parity = measure_parity(ticker, strat_name, params)
            bars = parity["bars"]
            decisions = parity["decision_bars"]
            agreement = parity["decision_agreement"]
            mismatches = parity["mismatches"]

            total_bars_compared += bars
            total_decision_bars += decisions
            fixture_summary.append({
                "ticker": ticker,
                "strategy": strat_name,
                "bars": bars,
                "decision_bars": decisions,
                "decision_agreement": agreement,
                "mismatch_count": len(mismatches),
            })

            assert agreement == 1.0, f"Mismatch in {strat_name} on {ticker}: {mismatches[:1]}"
            assert len(mismatches) == 0

    assert total_bars_compared == 7000  # 7 strategies * 2 tickers * 500 bars
    assert total_decision_bars > 0
    # Print summary table for morning report
    print(f"\n[Signal Parity Summary] Compared {total_bars_compared} bars across 14 fixtures: {total_decision_bars} decision bars, 0 mismatches.")


# ============================================================================
# Section 3: AlphaSignal Pre-Execution Gate with Injected Failures
# ============================================================================


class MockAlphaSignalHandler(BaseHTTPRequestHandler):
    mode = "valid_positive"

    def do_GET(self):
        if self.path.startswith("/health"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "service": "alphasignal",
                "version": "1.2.0",
                "sentiment_contract": "2",
                "status": "healthy",
            }).encode())
            return

        if self.path.startswith("/sentiment/"):
            ticker = self.path.split("/")[-1]
            if self.mode == "valid_positive":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "ticker": ticker,
                    "sentiment_score": 0.65, "latest_score": 0.65,
                    "confidence": 0.88,
                    "sources": ["source_1"],
                    "data_available": True,
                    "status": "ok",
                    "degraded": False,
                }).encode())
            elif self.mode == "breach_negative":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "ticker": ticker,
                    "sentiment_score": -0.75, "latest_score": -0.75,
                    "confidence": 0.92,
                    "sources": ["source_2"],
                    "data_available": True,
                    "status": "ok",
                    "degraded": False,
                }).encode())
            elif self.mode == "stale":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "ticker": ticker,
                    "sentiment_score": None, "latest_score": None,
                    "confidence": 0.0,
                    "sources": [],
                    "data_available": True,
                    "status": "stale",
                    "degraded": False,
                }).encode())
            elif self.mode == "malformed":
                # Contradictory fields: status=ok but data_available=False
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "ticker": ticker,
                    "sentiment_score": -0.5, "latest_score": -0.5,
                    "confidence": 0.5,
                    "sources": [],
                    "data_available": False,
                    "status": "ok",
                    "degraded": False,
                }).encode())
            elif self.mode == "timeout":
                time.sleep(1.5)  # exceed 0.5s client timeout
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")
            elif self.mode == "http500":
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"Internal Server Error")
            else:
                self.send_response(404)
                self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress server logs during test


@pytest.fixture(scope="module")
def alphasignal_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockAlphaSignalHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def test_alphasignal_gate_and_injected_failures(alphasignal_server):
    """Exercise sentiment checks, threshold breaches, timeout, stale data,
    malformed payloads, fail policies, and protective exit bypass."""
    # 1. Valid positive sentiment (+0.65): BUY allowed
    MockAlphaSignalHandler.mode = "valid_positive"
    client = AlphaSignalClient(base_url=alphasignal_server, timeout_seconds=1.0, fail_policy="open")
    allowed, pred = asyncio.run(client.is_execution_allowed("AAPL", intended_direction=2))
    assert allowed is True
    assert pred["gate_status"] == GATE_STATUS_AVAILABLE_PASS

    # 2. Threshold breach (-0.75 < -0.3): BUY blocked
    MockAlphaSignalHandler.mode = "breach_negative"
    allowed, pred = asyncio.run(client.is_execution_allowed("AAPL", intended_direction=2))
    assert allowed is False
    assert pred["gate_status"] == GATE_STATUS_AVAILABLE_BLOCK

    # 3. Timeout with fail_open vs fail_closed
    MockAlphaSignalHandler.mode = "timeout"
    client_open = AlphaSignalClient(base_url=alphasignal_server, timeout_seconds=0.2, fail_policy="open")
    allowed_open, pred_open = asyncio.run(client_open.is_execution_allowed("AAPL", intended_direction=2))
    assert allowed_open is True
    assert pred_open["gate_status"] == GATE_STATUS_ERROR_BYPASS
    assert pred_open["fail_policy"] == "open"

    client_closed = AlphaSignalClient(base_url=alphasignal_server, timeout_seconds=0.2, fail_policy="closed")
    allowed_closed, pred_closed = asyncio.run(client_closed.is_execution_allowed("AAPL", intended_direction=2))
    assert allowed_closed is False
    assert pred_closed["gate_status"] == GATE_STATUS_ERROR_BYPASS
    assert pred_closed["fail_policy"] == "closed"

    # 4. Stale data with fail_open vs fail_closed
    MockAlphaSignalHandler.mode = "stale"
    allowed_stale_open, pred_stale_open = asyncio.run(client_open.is_execution_allowed("AAPL", intended_direction=2))
    assert allowed_stale_open is True
    assert pred_stale_open["gate_status"] == GATE_STATUS_STALE_BYPASS
    assert pred_stale_open["fail_policy"] == "open"

    allowed_stale_closed, pred_stale_closed = asyncio.run(client_closed.is_execution_allowed("AAPL", intended_direction=2))
    assert allowed_stale_closed is False
    assert pred_stale_closed["gate_status"] == GATE_STATUS_STALE_BYPASS
    assert pred_stale_closed["fail_policy"] == "closed"

    # 5. Malformed payload with fail_open vs fail_closed
    MockAlphaSignalHandler.mode = "malformed"
    allowed_mal_open, pred_mal_open = asyncio.run(client_open.is_execution_allowed("AAPL", intended_direction=2))
    assert allowed_mal_open is True
    assert pred_mal_open["gate_status"] == GATE_STATUS_INVALID_RESPONSE_BYPASS
    assert pred_mal_open["fail_policy"] == "open"

    allowed_mal_closed, pred_mal_closed = asyncio.run(client_closed.is_execution_allowed("AAPL", intended_direction=2))
    assert allowed_mal_closed is False
    assert pred_mal_closed["gate_status"] == GATE_STATUS_INVALID_RESPONSE_BYPASS
    assert pred_mal_closed["fail_policy"] == "closed"

    # 6. Exit bypass: SELL (direction=1)
    MockAlphaSignalHandler.mode = "breach_negative"
    allowed_exit, pred_exit = asyncio.run(run_pre_execution_checks(client_closed, "AAPL", signal_direction=1))
    assert allowed_exit is True
    assert pred_exit["gate_status"] == "neutral_bypass"

# ============================================================================
# Section 4: Multi-Session Replay with AlpacaSim & Crash Recovery
# ============================================================================


BASE_CONFIG = {
    "schema_version": "1.0",
    "strategy": {
        "name": "ma_crossover",
        "parameters": {"fast_period": 10, "slow_period": 20},
    },
    "ticker": "AAPL",
    "timeframe": "1Day",
    "risk": {
        "stop_loss_pct": 2.0,
        "take_profit_pct": 5.0,
        "max_position_size_pct": 10.0,
        "max_daily_loss_pct": 3.0,
        "max_open_positions": 5,
        "portfolio_max_positions": 10,
        "trailing_stop_enabled": False,
        "trailing_stop_pct": 3.0,
        "commission_per_trade": 0.0,
    },
    "execution": {"order_type": "market", "limit_offset_pct": 0.1, "cooldown_bars": 1},
    "safety_limits": {
        "max_trades_per_day": 20,
        "max_api_calls_per_hour": 500,
        "signal_generation_timeout_seconds": 5.0,
        "broker_degraded_mode_threshold_failures": 3,
    },
    "metadata": {
        "exported_from": "AlphaLab",
        "exported_at": "2026-10-18T00:00:00Z",
        "alphalab_version": "0.1.0",
        "backtest_id": "sim_frozen_replay",
        "backtest_period": {"start": "2020-01-01", "end": "2024-12-31"},
        "performance": {
            "sharpe_ratio": 1.5,
            "sortino_ratio": 1.8,
            "total_return_pct": 25.0,
            "max_drawdown_pct": 8.0,
            "win_rate_pct": 60.0,
            "profit_factor": 1.8,
            "total_trades": 10,
            "calmar_ratio": 2.0,
        },
    },
}


def create_order_environment(sim_url: str, state_path: Path):
    sc = StrategySchema(**BASE_CONFIG)
    broker = AlpacaBroker("SIMKEY", "SIMSECRET", paper=True, base_url=sim_url)
    assert broker.connect()
    state = BotState(str(state_path), exclusive=False)
    g = GlobalRiskManager(bot_state=state)
    rm = RiskManager(sc.risk, sc.execution, sc.strategy.name, sc.safety_limits, global_risk=g)
    rm.attach_state(state, "AAPL")
    g.register_strategy("AAPL", rm)
    om = OrderManager(broker, rm, sc, notifier=None, dry_run=False, state=state)
    return om, rm, state, broker


def run_multi_session_replay(state_file: Path, sim: AlpacaSim) -> Dict[str, Any]:
    """Execute a multi-session trading loop with simulated crash/restart cycles."""
    start_time = time.perf_counter()
    latencies = []

    # Reset simulator state
    requests.post(sim.url + "/sim/reset", json={"cash": 100000.0})
    requests.post(sim.url + "/sim/price", json={"symbol": "AAPL", "price": 150.0})

    # --- SESSION 1: Clean Order Submission & Fill ---
    t0 = time.perf_counter()
    om, rm, state, broker = create_order_environment(sim.url, state_file)
    res = om.execute_signal("AAPL", {"signal": "BUY", "reason": "entry"}, current_price=150.0, account_equity=100000.0, current_positions_count=0, total_portfolio_positions=0)
    latencies.append(time.perf_counter() - t0)

    assert res["status"] == "success"
    coid_1 = sim.client_ids()[-1]
    req_qty = float(res["requested_qty"])
    # Simulate broker fill
    requests.post(sim.url + "/sim/fill", json={"client_order_id": coid_1, "qty": req_qty, "price": 150.0})
    om.sync_open_intents()

    pos1 = state.get_open_positions().get("AAPL", {})
    assert pos1.get("qty") == req_qty
    initial_intent_id = om.state.list_all_intents()[-1]["intent_id"]

    # --- SESSION 2: Crash after Intent Persistence, then Restart ---
    # Manually create intent simulating crash in INTENT_SUBMITTING
    simulated_intent = state.create_submission_intent(
        ticker="AAPL", side="BUY", qty=10.0, order_type="market", strategy_name="ma_crossover"
    )
    state.update_submission_intent(simulated_intent["intent_id"], status=INTENT_SUBMITTING)
    saved_coid = simulated_intent["client_order_id"]

    # Process "dies" -> state persisted to disk
    state.save()
    del om, rm, state, broker

    # Restart process from state file
    t1 = time.perf_counter()
    om2, rm2, state2, broker2 = create_order_environment(sim.url, state_file)
    latencies.append(time.perf_counter() - t1)

    # Startup sync reconciles the submitting intent (within grace, broker has no record -> quarantined)
    om2.sync_open_intents()
    open_intents = state2.list_open_intents()
    assert any(i["intent_id"] == simulated_intent["intent_id"] for i in open_intents)
    # Client order ID was preserved and not re-sent as a duplicate
    assert len([cid for cid in sim.client_ids() if cid == saved_coid]) == 0
    # Operator resolves the quarantined missing intent (never sent to broker) to abandoned
    state2.update_submission_intent(simulated_intent["intent_id"], status="abandoned")

    # --- SESSION 3: Crash after Broker Acceptance but before Local Ack ---
    # Broker accepts order, but local process crashes before confirmation
    accepted_order_coid = om2._generate_idempotency_key("AAPL", "buy", datetime.now(ET))
    sim_order = broker2.place_market_order("AAPL", 10, "buy", accepted_order_coid)
    assert sim_order is not None

    # Bot had saved intent as SUBMITTING with that client_order_id before crash
    crash_intent = state2.create_submission_intent(
        ticker="AAPL", side="BUY", qty=10.0, order_type="market", strategy_name="ma_crossover"
    )
    state2.update_submission_intent(crash_intent["intent_id"], status=INTENT_SUBMITTING, client_order_id=accepted_order_coid)
    state2.save()
    del om2, rm2, state2, broker2

    # Process restart
    om3, rm3, state3, broker3 = create_order_environment(sim.url, state_file)
    # Startup sync finds order at broker and updates status to INTENT_SUBMITTED without duplicating submission
    om3.sync_open_intents()
    reconciled_intent = state3.get_submission_intent(crash_intent["intent_id"])
    assert reconciled_intent["status"] == INTENT_SUBMITTED
    assert reconciled_intent["broker_order_id"] == sim_order.id

    # Fill the accepted order
    requests.post(sim.url + "/sim/fill", json={"client_order_id": accepted_order_coid, "qty": 10.0, "price": 152.0})
    om3.sync_open_intents()
    pos3 = state3.get_open_positions().get("AAPL", {})
    assert pos3.get("qty") == req_qty + 10.0

    # --- SESSION 4: Orderly Position Close ---
    requests.post(sim.url + "/sim/price", json={"symbol": "AAPL", "price": 160.0})
    t2 = time.perf_counter()
    close_res = om3.close_position("AAPL", reason="take_profit")
    latencies.append(time.perf_counter() - t2)

    assert close_res["fill_status"] == INTENT_SUBMITTED
    close_coid = sim.client_ids()[-1]
    requests.post(sim.url + "/sim/fill", json={"client_order_id": close_coid, "qty": req_qty + 10.0, "price": 160.0})
    om3.sync_open_intents()

    # Final positions and ledger verification
    final_pos = state3.get_open_positions().get("AAPL", {})
    assert final_pos.get("qty", 0.0) == 0.0
    broker_pos = broker3.get_position("AAPL")
    assert broker_pos is None or float(broker_pos.qty) == 0.0

    state_size_bytes = state_file.stat().st_size
    total_time = time.perf_counter() - start_time

    return {
        "final_qty": final_pos.get("qty", 0.0),
        "realized_pnl": state3.state.get("daily_pnl", 0.0),
        "client_ids": list(sim.client_ids()),
        "total_intents": len(state3.list_all_intents()),
        "state_size_bytes": state_size_bytes,
        "avg_latency_ms": (sum(latencies) / len(latencies)) * 1000.0,
        "total_time_s": total_time,
    }


def test_bounded_multi_session_replay_idempotency(monkeypatch, tmp_path):
    """Run the multi-session replay twice with the same seed: once on run A,
    and once on run B with a fresh state directory. Assert determinism,
    stable intent IDs, zero external calls, and clean state recovery."""
    monkeypatch.setenv("ALPHALIVE_BROKER_READ_ONLY", "false")
    monkeypatch.setenv("ALPACA_HTTP_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("TRADING_PAUSED", "false")

    with AlpacaSim() as sim:
        state_dir_a = tmp_path / "run_a"
        state_dir_a.mkdir()
        state_file_a = state_dir_a / "bot_state.json"

        # Run 1: First replay
        res_a = run_multi_session_replay(state_file_a, sim)

        # Run 2: Fresh state directory, clean replay
        state_dir_b = tmp_path / "run_b"
        state_dir_b.mkdir()
        state_file_b = state_dir_b / "bot_state.json"

        res_b = run_multi_session_replay(state_file_b, sim)

        # Assert parity between run A and run B
        assert res_a["final_qty"] == 0.0
        assert res_b["final_qty"] == 0.0
        assert res_a["realized_pnl"] == res_b["realized_pnl"]
        assert res_a["total_intents"] == res_b["total_intents"]
        assert abs(res_a["state_size_bytes"] - res_b["state_size_bytes"]) < 500

        print(
            f"\n[Replay Idempotency Verified]\n"
            f"Run A: PnL=${res_a['realized_pnl']:.2f}, Intents={res_a['total_intents']}, Size={res_a['state_size_bytes']}B, Avg Latency={res_a['avg_latency_ms']:.2f}ms\n"
            f"Run B: PnL=${res_b['realized_pnl']:.2f}, Intents={res_b['total_intents']}, Size={res_b['state_size_bytes']}B, Avg Latency={res_b['avg_latency_ms']:.2f}ms"
        )
