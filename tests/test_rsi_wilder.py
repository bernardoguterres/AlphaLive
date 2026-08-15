"""Canonical Wilder RSI tests (Prompt 2.5 remediation, 2026-08-15).

See FINAL_ENGINEERING_AUDIT.md / END_TO_END_VALIDATION.md for the two
defects this fixes: (A) AlphaLab's rsi_period parameter was accepted but
never actually used by RSIMeanReversion; (B) AlphaLab's old RSI formula
(raw EWM, no warm-up guard) and AlphaLive's (the `ta` library) disagreed
numerically, causing real signal-parity failures on fresh runtime data.

The golden values below are pinned literals, independently duplicated in
AlphaLab's tests/test_rsi_wilder.py - both repos assert against the SAME
numbers rather than importing a shared dependency, matching this
project's "two independent implementations + parity test" pattern.
"""

import numpy as np
import pandas as pd
import pytest

from alphalive.strategy.indicators import rsi_wilder
from alphalive.strategy.signal_engine import SignalEngine
from alphalive.strategy_schema import StrategySchema

# 30-bar deterministic mixed sequence, identical to AlphaLab's fixture.
GOLDEN_CLOSES = [
    100.00,
    100.50,
    100.25,
    101.00,
    100.75,
    101.50,
    102.00,
    101.25,
    102.50,
    103.00,
    102.75,
    103.50,
    104.00,
    103.25,
    104.50,
    105.00,
    104.25,
    105.50,
    106.00,
    105.25,
    106.50,
    107.00,
    106.25,
    107.50,
    108.00,
    107.25,
    108.50,
    109.00,
    108.25,
    109.50,
]


def _close():
    return pd.Series(GOLDEN_CLOSES)


@pytest.mark.parametrize("period", [9, 14, 21])
def test_rsi_wilder_warmup_rows_are_nan(period):
    r = rsi_wilder(_close(), period)
    assert r.iloc[:period].isna().all()


@pytest.mark.parametrize(
    "period,expected_first",
    [(9, 77.272727), (14, 75.0), (21, 74.137931)],
)
def test_rsi_wilder_first_valid_row(period, expected_first):
    r = rsi_wilder(_close(), period)
    assert r.first_valid_index() == period
    assert r.iloc[period] == pytest.approx(expected_first, abs=1e-5)


def test_rsi_wilder_steady_state_values_period_9():
    r = rsi_wilder(_close(), 9)
    assert r.iloc[10] == pytest.approx(73.513514, abs=1e-5)
    assert r.iloc[29] == pytest.approx(71.681515, abs=1e-5)


def test_rsi_wilder_steady_state_values_period_14():
    r = rsi_wilder(_close(), 14)
    assert r.iloc[19] == pytest.approx(69.36129, abs=1e-5)
    assert r.iloc[29] == pytest.approx(71.86734, abs=1e-5)


def test_rsi_wilder_steady_state_values_period_21():
    r = rsi_wilder(_close(), 21)
    assert r.iloc[29] == pytest.approx(72.112145, abs=1e-5)


def test_rsi_wilder_flat_price_is_50():
    r = rsi_wilder(pd.Series([100.0] * 40), 14)
    assert r.iloc[14:].eq(50.0).all()


def test_rsi_wilder_monotonic_rise_is_100():
    r = rsi_wilder(pd.Series(np.arange(100, 160, 1.0)), 14)
    assert r.iloc[14:].eq(100.0).all()


def test_rsi_wilder_monotonic_fall_is_0():
    r = rsi_wilder(pd.Series(np.arange(160, 100, -1.0)), 14)
    assert r.iloc[14:].eq(0.0).all()


def test_rsi_wilder_too_short_series_is_all_nan():
    r = rsi_wilder(pd.Series([100.0, 101.0, 99.0]), 14)
    assert r.isna().all()


def test_rsi_period_9_differs_from_14_where_mathematically_expected():
    close = _close()
    r9 = rsi_wilder(close, 9)
    r14 = rsi_wilder(close, 14)
    common_idx = r9.dropna().index.intersection(r14.dropna().index)
    assert len(common_idx) > 0
    assert not np.allclose(r9.loc[common_idx], r14.loc[common_idx])


def _make_config(period: int) -> StrategySchema:
    return StrategySchema(
        schema_version="1.0",
        strategy={
            "name": "rsi_mean_reversion",
            "parameters": {
                "period": period,
                "oversold": 30,
                "overbought": 70,
                "use_bb_confirmation": False,
            },
        },
        ticker="AAPL",
        timeframe="1Day",
        risk={
            "stop_loss_pct": 2.0,
            "take_profit_pct": 5.0,
            "max_position_size_pct": 10.0,
            "max_daily_loss_pct": 5.0,
            "max_open_positions": 3,
            "portfolio_max_positions": 10,
        },
        execution={"order_type": "market"},
        safety_limits={},
        metadata={
            "exported_from": "test_rsi_wilder",
            "exported_at": "2026-08-15T00:00:00",
            "alphalab_version": "1.0.0",
            "backtest_id": f"rsi_wilder_test_{period}",
            "backtest_period": {"start": "2024-01-01", "end": "2024-02-01"},
            "performance": {
                "sharpe_ratio": 0.0,
                "sortino_ratio": 0.0,
                "total_return_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "win_rate_pct": 0.0,
                "profit_factor": 1.0,
                "total_trades": 0,
                "calmar_ratio": 0.0,
            },
        },
    )


def _bars_from_prices(prices):
    data = [
        {"open": p, "high": p + 0.5, "low": p - 0.5, "close": p, "volume": 1_000_000}
        for p in prices
    ]
    df = pd.DataFrame(data)
    df.index = pd.date_range(
        start="2024-01-01", periods=len(prices), freq="D", tz="America/New_York"
    )
    return df


def test_rsi_mean_reversion_signal_engine_uses_the_configured_period():
    """The real SignalEngine, not just rsi_wilder() in isolation, must
    consume the configured period end to end - same asymmetric-crossing
    dataset as AlphaLab's equivalent test (identical construction, so a
    genuine cross-repo comparison is meaningful, not just a same-repo
    sanity check)."""
    prices = [100.0]
    for i in range(24):
        prices.append(prices[-1] + (1.0 if i % 2 == 0 else -0.8))
    for i in range(5):
        prices.append(prices[-1] - 1.5)
    prices += [prices[-1]] * 5
    df = _bars_from_prices(prices)

    engine9 = SignalEngine(_make_config(9))
    engine14 = SignalEngine(_make_config(14))

    # At bar 28 (0-indexed), by construction RSI(9) ~26 (oversold) while
    # RSI(14) ~35 (not oversold) - verified identically in AlphaLab's test.
    sig9 = engine9.generate_signal(df.iloc[:29])
    sig14 = engine14.generate_signal(df.iloc[:29])

    assert sig9["signal"] == "BUY", f"RSI(9) should be oversold here: {sig9}"
    assert sig14["signal"] != "BUY", (
        f"RSI(14) should NOT be oversold at the same bar - if it also "
        f"fires BUY, rsi_period isn't actually differentiating behavior: "
        f"{sig14}"
    )
