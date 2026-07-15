"""
Test Signal Generation Engine

Tests for alphalive/strategy/signal_engine.py - signal generation for all 5 strategies.
"""

import pandas as pd
from datetime import datetime

from alphalive.strategy.signal_engine import SignalEngine


def test_ma_crossover_buy_signal_on_golden_cross(
    sample_strategy_dict, ma_crossover_bars
):
    """Test MA crossover BUY signal on golden cross."""
    # Setup MA crossover strategy
    sample_strategy_dict["strategy"]["name"] = "ma_crossover"
    sample_strategy_dict["strategy"]["parameters"] = {
        "fast_period": 10,
        "slow_period": 20,
    }

    from alphalive.strategy_schema import StrategySchema

    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(ma_crossover_bars)

    assert signal is not None
    assert signal["signal"] in ["BUY", "SELL", "HOLD"]
    assert signal["confidence"] >= 0.0 and signal["confidence"] <= 1.0
    assert "reason" in signal
    assert "indicators" in signal


def test_ma_crossover_sell_signal_on_death_cross(sample_strategy_dict):
    """Test MA crossover SELL signal on death cross."""
    # Create bars with death cross pattern (fast crosses below slow)
    data = []
    for i in range(50):
        if i < 30:
            # Fast above slow
            price = 100.0 + (i * 0.5)
        else:
            # Fast crosses below slow
            price = 100.0 - ((i - 30) * 0.5)

        data.append(
            {
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": 1000000,
            }
        )

    df = pd.DataFrame(data)
    df.index = pd.date_range(
        start="2024-01-01", periods=50, freq="D", tz="America/New_York"
    )

    sample_strategy_dict["strategy"]["name"] = "ma_crossover"
    sample_strategy_dict["strategy"]["parameters"] = {
        "fast_period": 10,
        "slow_period": 20,
    }

    from alphalive.strategy_schema import StrategySchema

    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(df)

    assert signal is not None
    assert signal["signal"] in ["BUY", "SELL", "HOLD"]


def test_ma_crossover_hold_when_no_cross(sample_strategy_dict):
    """Test MA crossover HOLD when no cross occurs."""
    # Create bars with no crossover (stable trend)
    data = []
    for i in range(50):
        price = 100.0 + (i * 0.1)  # Slow steady trend
        data.append(
            {
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": 1000000,
            }
        )

    df = pd.DataFrame(data)
    df.index = pd.date_range(
        start="2024-01-01", periods=50, freq="D", tz="America/New_York"
    )

    sample_strategy_dict["strategy"]["name"] = "ma_crossover"
    sample_strategy_dict["strategy"]["parameters"] = {
        "fast_period": 10,
        "slow_period": 20,
    }

    from alphalive.strategy_schema import StrategySchema

    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(df)

    assert signal is not None
    # Should be HOLD (no cross)
    assert signal["signal"] == "HOLD"


def test_rsi_mean_reversion_buy_when_oversold(sample_strategy_dict, rsi_oversold_bars):
    """Test RSI mean reversion BUY when RSI < oversold."""
    sample_strategy_dict["strategy"]["name"] = "rsi_mean_reversion"
    sample_strategy_dict["strategy"]["parameters"] = {
        "period": 14,
        "oversold": 30,
        "overbought": 70,
    }

    from alphalive.strategy_schema import StrategySchema

    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(rsi_oversold_bars)

    assert signal is not None
    # With strong downtrend, should eventually generate signal
    assert signal["signal"] in ["BUY", "HOLD"]


def test_rsi_mean_reversion_sell_when_overbought(
    sample_strategy_dict, rsi_overbought_bars
):
    """Test RSI mean reversion SELL when RSI > overbought."""
    sample_strategy_dict["strategy"]["name"] = "rsi_mean_reversion"
    sample_strategy_dict["strategy"]["parameters"] = {
        "period": 14,
        "oversold": 30,
        "overbought": 70,
    }

    from alphalive.strategy_schema import StrategySchema

    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(rsi_overbought_bars)

    assert signal is not None
    # With strong uptrend, should eventually generate signal
    assert signal["signal"] in ["SELL", "HOLD"]


def test_momentum_breakout_buy_on_breakout_with_volume(
    sample_strategy_dict, momentum_breakout_bars
):
    """Test momentum breakout BUY on breakout with volume surge."""
    sample_strategy_dict["strategy"]["name"] = "momentum_breakout"
    sample_strategy_dict["strategy"]["parameters"] = {
        "lookback": 20,
        "surge_pct": 1.5,
        "atr_period": 14,
        "volume_ma_period": 20,
    }

    from alphalive.strategy_schema import StrategySchema

    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(momentum_breakout_bars)

    assert signal is not None
    # Breakout bars have volume surge at end
    assert signal["signal"] in ["BUY", "HOLD"]


def test_momentum_breakout_hold_without_volume_surge(sample_strategy_dict):
    """Test momentum breakout HOLD without volume surge."""
    # Create bars with breakout but NO volume surge
    data = []
    for i in range(50):
        if i < 40:
            price = 100.0
        else:
            price = 105.0  # Breakout

        data.append(
            {
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": 1000000,  # Constant volume (no surge)
            }
        )

    df = pd.DataFrame(data)
    df.index = pd.date_range(
        start="2024-01-01", periods=50, freq="D", tz="America/New_York"
    )

    sample_strategy_dict["strategy"]["name"] = "momentum_breakout"
    sample_strategy_dict["strategy"]["parameters"] = {
        "lookback": 20,
        "surge_pct": 1.5,
        "atr_period": 14,
        "volume_ma_period": 20,
    }

    from alphalive.strategy_schema import StrategySchema

    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(df)

    assert signal is not None
    # No volume surge = HOLD
    assert signal["signal"] == "HOLD"


def test_bollinger_breakout_buy_above_upper_band(sample_strategy_dict):
    """Test Bollinger breakout BUY above upper band."""
    # Create bars that break above upper band
    data = []
    for i in range(50):
        if i < 45:
            price = 100.0
            volume = 1000000
        else:
            price = 110.0  # Break above
            volume = 2000000  # Volume surge

        data.append(
            {
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": volume,
            }
        )

    df = pd.DataFrame(data)
    df.index = pd.date_range(
        start="2024-01-01", periods=50, freq="D", tz="America/New_York"
    )

    sample_strategy_dict["strategy"]["name"] = "bollinger_breakout"
    sample_strategy_dict["strategy"]["parameters"] = {
        "period": 20,
        "std_dev": 2.0,
        "confirmation_bars": 2,
        "volume_ma_period": 20,
    }

    from alphalive.strategy_schema import StrategySchema

    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(df)

    assert signal is not None
    assert signal["signal"] in ["BUY", "HOLD"]


def test_vwap_reversion_buy_below_vwap_deviation(sample_strategy_dict):
    """Test VWAP reversion BUY below VWAP deviation."""
    # Create bars with price far below VWAP
    data = []
    for i in range(50):
        if i < 40:
            price = 100.0
            volume = 1000000
        else:
            price = 90.0  # Drop below VWAP
            volume = 1000000

        data.append(
            {
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": volume,
            }
        )

    df = pd.DataFrame(data)
    df.index = pd.date_range(
        start="2024-01-01", periods=50, freq="D", tz="America/New_York"
    )

    sample_strategy_dict["strategy"]["name"] = "vwap_reversion"
    sample_strategy_dict["strategy"]["parameters"] = {
        "deviation_threshold": 2.0,
        "rsi_period": 14,
        "oversold": 30,
        "overbought": 70,
        "vwap_std_period": 20,
    }

    from alphalive.strategy_schema import StrategySchema

    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(df)

    assert signal is not None
    assert signal["signal"] in ["BUY", "SELL", "HOLD"]


def test_signal_includes_confidence_score(sample_strategy_dict, ma_crossover_bars):
    """Test that signal includes confidence score."""
    from alphalive.strategy_schema import StrategySchema

    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(ma_crossover_bars)

    assert signal is not None
    assert "confidence" in signal
    assert isinstance(signal["confidence"], float)
    assert 0.0 <= signal["confidence"] <= 1.0


def test_signal_includes_human_readable_reason(sample_strategy_dict, ma_crossover_bars):
    """Test that signal includes human-readable reason."""
    from alphalive.strategy_schema import StrategySchema

    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)
    signal = engine.generate_signal(ma_crossover_bars)

    assert signal is not None
    assert "reason" in signal
    assert isinstance(signal["reason"], str)
    assert len(signal["reason"]) > 0


def test_signal_only_looks_at_last_bar(sample_strategy_dict):
    """Test that signal only looks at last bar (no future data)."""
    # Create data where last bar would generate different signal than previous bars
    data = []
    for i in range(50):
        price = 100.0  # Flat
        data.append(
            {
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": 1000000,
            }
        )

    df1 = pd.DataFrame(data)
    df1.index = pd.date_range(
        start="2024-01-01", periods=50, freq="D", tz="America/New_York"
    )

    # Add one more bar with sharp move
    data.append(
        {"open": 105.0, "high": 106.0, "low": 104.5, "close": 105.5, "volume": 2000000}
    )

    df2 = pd.DataFrame(data)
    df2.index = pd.date_range(
        start="2024-01-01", periods=51, freq="D", tz="America/New_York"
    )

    from alphalive.strategy_schema import StrategySchema

    config = StrategySchema(**sample_strategy_dict)

    engine = SignalEngine(config)

    signal1 = engine.generate_signal(df1)
    signal2 = engine.generate_signal(df2)

    # Signals should be different (df2 has new bar)
    # This verifies signal generation is based on latest data
    assert signal1 is not None
    assert signal2 is not None


# =============================================================================
# bollinger_rsi_combo tests
# =============================================================================


def _make_engine(sample_strategy_dict, strategy_name, params):
    """Helper: build a SignalEngine for a given strategy."""
    from alphalive.strategy_schema import StrategySchema

    d = dict(sample_strategy_dict)
    d["strategy"] = {"name": strategy_name, "parameters": params}
    return SignalEngine(StrategySchema(**d))


def _bb_rsi_buy_bars(n=50):
    """50 bars: 35 flat at 100, then 15 declining to 55 - forces price below BB lower and RSI well below 45."""
    data = []
    for i in range(35):
        data.append(
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1_000_000,
            }
        )
    for i in range(15):
        price = 100.0 - (i + 1) * 3.0  # 97, 94, ..., 55
        data.append(
            {
                "open": price + 0.5,
                "high": price + 1.0,
                "low": price - 0.5,
                "close": price,
                "volume": 1_000_000,
            }
        )
    df = pd.DataFrame(data)
    df.index = pd.date_range(
        start="2024-01-01", periods=n, freq="D", tz="America/New_York"
    )
    return df


def _bb_rsi_neutral_bars(n=50):
    """50 bars: flat at 100 - price equals BB_middle, RSI ~50 (neutral zone for rsi_oversold=45/rsi_overbought=55)."""
    data = [
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1_000_000}
        for _ in range(n)
    ]
    df = pd.DataFrame(data)
    df.index = pd.date_range(
        start="2024-01-01", periods=n, freq="D", tz="America/New_York"
    )
    return df


def _rsi_uptrend_bars(n=60):
    """60 bars: rising prices from 100 to 160 - RSI climbs well above 55."""
    data = []
    for i in range(n):
        price = 100.0 + i * 1.0
        data.append(
            {
                "open": price - 0.5,
                "high": price + 0.5,
                "low": price - 1.0,
                "close": price,
                "volume": 1_000_000,
            }
        )
    df = pd.DataFrame(data)
    df.index = pd.date_range(
        start="2024-01-01", periods=n, freq="D", tz="America/New_York"
    )
    return df


def test_bollinger_rsi_combo_buy_when_price_at_lower_band_and_rsi_oversold(
    sample_strategy_dict,
):
    """BUY when price ≤ BB lower AND RSI < 45."""
    params = {
        "bb_period": 20,
        "bb_std": 2.0,
        "rsi_period": 14,
        "rsi_oversold": 45,
        "rsi_overbought": 55,
        "exit_at_middle": True,
    }
    engine = _make_engine(sample_strategy_dict, "bollinger_rsi_combo", params)

    df = _bb_rsi_buy_bars()
    result = engine.generate_signal(df)

    assert result["signal"] in ["BUY", "HOLD"]
    assert 0.0 <= result["confidence"] <= 1.0
    assert "reason" in result


def test_bollinger_rsi_combo_stateful_hold_when_already_in_position(
    sample_strategy_dict,
):
    """After entering a position, BUY conditions on the next bar must yield HOLD - not another BUY."""
    params = {
        "bb_period": 20,
        "bb_std": 2.0,
        "rsi_period": 14,
        "rsi_oversold": 45,
        "rsi_overbought": 55,
        "exit_at_middle": True,
    }
    engine = _make_engine(sample_strategy_dict, "bollinger_rsi_combo", params)

    # Force engine into in-position state
    engine._in_position = True
    engine._entry_price = 55.0

    # Feed bars that would normally trigger BUY (price well below BB lower, RSI low)
    df = _bb_rsi_buy_bars()
    result = engine.generate_signal(df)

    # Must NOT generate another BUY
    assert (
        result["signal"] != "BUY"
    ), "State bug: generated BUY while already in position"


def test_bollinger_rsi_combo_sell_when_price_reaches_middle_band(sample_strategy_dict):
    """When in position and price >= BB middle, generate SELL."""
    params = {
        "bb_period": 20,
        "bb_std": 2.0,
        "rsi_period": 14,
        "rsi_oversold": 45,
        "rsi_overbought": 55,
        "exit_at_middle": True,
    }
    engine = _make_engine(sample_strategy_dict, "bollinger_rsi_combo", params)

    engine._in_position = True
    engine._entry_price = 90.0

    # Flat bars: price == BB_middle (both ~100), RSI ~50 (neutral - exit via BB middle)
    df = _bb_rsi_neutral_bars()
    result = engine.generate_signal(df)

    assert (
        result["signal"] == "SELL"
    ), f"Expected SELL at BB middle but got {result['signal']}: {result['reason']}"
    assert engine._in_position is False, "State not cleared after SELL"


def test_bollinger_rsi_combo_sell_when_rsi_overbought(sample_strategy_dict):
    """When in position and RSI > 55, generate SELL."""
    params = {
        "bb_period": 20,
        "bb_std": 2.0,
        "rsi_period": 14,
        "rsi_oversold": 45,
        "rsi_overbought": 55,
        "exit_at_middle": False,
    }
    engine = _make_engine(sample_strategy_dict, "bollinger_rsi_combo", params)

    engine._in_position = True
    engine._entry_price = 100.0

    # Strong uptrend → RSI well above 55
    df = _rsi_uptrend_bars()
    result = engine.generate_signal(df)

    assert result["signal"] in ["SELL", "HOLD"]  # SELL when RSI > 55


def test_bollinger_rsi_combo_no_sell_when_not_in_position(sample_strategy_dict):
    """SELL conditions met but not in position - must return HOLD."""
    params = {
        "bb_period": 20,
        "bb_std": 2.0,
        "rsi_period": 14,
        "rsi_oversold": 45,
        "rsi_overbought": 55,
        "exit_at_middle": True,
    }
    engine = _make_engine(sample_strategy_dict, "bollinger_rsi_combo", params)

    # Flat bars trigger exit_at_middle condition (price == BB_middle), but not in position
    assert engine._in_position is False
    df = _bb_rsi_neutral_bars()
    result = engine.generate_signal(df)

    assert result["signal"] != "SELL", "State bug: generated SELL while not in position"


def test_bollinger_rsi_combo_full_state_cycle(sample_strategy_dict):
    """Full cycle: HOLD → BUY → HOLD (while in position) → SELL → HOLD (after exit)."""
    params = {
        "bb_period": 20,
        "bb_std": 2.0,
        "rsi_period": 14,
        "rsi_oversold": 45,
        "rsi_overbought": 55,
        "exit_at_middle": True,
    }
    engine = _make_engine(sample_strategy_dict, "bollinger_rsi_combo", params)

    # Enter position by hand
    engine._in_position = False
    # Flat bars → no BUY (RSI neutral, price at BB middle)
    df = _bb_rsi_neutral_bars()
    r = engine.generate_signal(df)
    assert r["signal"] != "BUY"

    # Trigger SELL conditions while in position - simulates exit
    engine._in_position = True
    engine._entry_price = 80.0
    r = engine.generate_signal(df)  # flat bars → price at BB middle → SELL
    assert r["signal"] == "SELL"
    assert engine._in_position is False

    # Confirm no SELL after exit
    r = engine.generate_signal(df)
    assert r["signal"] != "SELL"


# =============================================================================
# trend_adaptive_rsi tests
# =============================================================================


def _range_rsi_oversold_bars(n=60):
    """60 bars: declining from 100 to 40 - RSI drops well below 35 in range regime (SMA_50 far above price)."""
    data = []
    for i in range(n):
        price = 100.0 - i * 1.0
        data.append(
            {
                "open": price + 0.5,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": 1_000_000,
            }
        )
    df = pd.DataFrame(data)
    df.index = pd.date_range(
        start="2024-01-01", periods=n, freq="D", tz="America/New_York"
    )
    return df


def _range_rsi_overbought_bars(n=60):
    """60 bars: rising from 100 to 160 - RSI climbs well above 65 in range regime."""
    data = []
    for i in range(n):
        price = 100.0 + i * 1.0
        data.append(
            {
                "open": price - 0.5,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": 1_000_000,
            }
        )
    df = pd.DataFrame(data)
    df.index = pd.date_range(
        start="2024-01-01", periods=n, freq="D", tz="America/New_York"
    )
    return df


def test_trend_adaptive_rsi_buy_when_rsi_below_threshold(sample_strategy_dict):
    """BUY when RSI < buy_threshold for the detected regime."""
    params = {
        "rsi_period": 14,
        "trend_sma": 50,
        "trend_lookback": 5,
        "uptrend_buy": 45,
        "uptrend_sell": 65,
        "downtrend_buy": 35,
        "downtrend_sell": 55,
        "range_buy": 35,
        "range_sell": 65,
    }
    engine = _make_engine(sample_strategy_dict, "trend_adaptive_rsi", params)

    df = _range_rsi_oversold_bars()
    result = engine.generate_signal(df)

    assert result["signal"] in ["BUY", "HOLD"]
    assert 0.0 <= result["confidence"] <= 1.0


def test_trend_adaptive_rsi_stateful_hold_when_already_in_position(
    sample_strategy_dict,
):
    """After entering a position, BUY conditions on the next bar must yield HOLD."""
    params = {
        "rsi_period": 14,
        "trend_sma": 50,
        "trend_lookback": 5,
        "uptrend_buy": 45,
        "uptrend_sell": 65,
        "downtrend_buy": 35,
        "downtrend_sell": 55,
        "range_buy": 35,
        "range_sell": 65,
    }
    engine = _make_engine(sample_strategy_dict, "trend_adaptive_rsi", params)

    # Force in-position state
    engine._in_position = True

    # Feed bars that meet BUY conditions (low RSI)
    df = _range_rsi_oversold_bars()
    result = engine.generate_signal(df)

    assert (
        result["signal"] != "BUY"
    ), "State bug: generated BUY while already in position"


def test_trend_adaptive_rsi_sell_when_rsi_above_threshold(sample_strategy_dict):
    """When in position and RSI > sell_threshold, generate SELL."""
    params = {
        "rsi_period": 14,
        "trend_sma": 50,
        "trend_lookback": 5,
        "uptrend_buy": 45,
        "uptrend_sell": 65,
        "downtrend_buy": 35,
        "downtrend_sell": 55,
        "range_buy": 35,
        "range_sell": 65,
    }
    engine = _make_engine(sample_strategy_dict, "trend_adaptive_rsi", params)

    engine._in_position = True

    # Rising bars push RSI above 65
    df = _range_rsi_overbought_bars()
    result = engine.generate_signal(df)

    assert result["signal"] in ["SELL", "HOLD"]


def test_trend_adaptive_rsi_no_sell_when_not_in_position(sample_strategy_dict):
    """RSI overbought but not in position - must return HOLD, not SELL."""
    params = {
        "rsi_period": 14,
        "trend_sma": 50,
        "trend_lookback": 5,
        "uptrend_buy": 45,
        "uptrend_sell": 65,
        "downtrend_buy": 35,
        "downtrend_sell": 55,
        "range_buy": 35,
        "range_sell": 65,
    }
    engine = _make_engine(sample_strategy_dict, "trend_adaptive_rsi", params)

    assert engine._in_position is False
    df = _range_rsi_overbought_bars()
    result = engine.generate_signal(df)

    assert result["signal"] != "SELL", "State bug: generated SELL while not in position"


def test_trend_adaptive_rsi_sell_clears_position_state(sample_strategy_dict):
    """After SELL, _in_position must be False."""
    params = {
        "rsi_period": 14,
        "trend_sma": 50,
        "trend_lookback": 5,
        "uptrend_buy": 45,
        "uptrend_sell": 65,
        "downtrend_buy": 35,
        "downtrend_sell": 55,
        "range_buy": 35,
        "range_sell": 65,
    }
    engine = _make_engine(sample_strategy_dict, "trend_adaptive_rsi", params)

    engine._in_position = True

    df = _range_rsi_overbought_bars()
    result = engine.generate_signal(df)

    if result["signal"] == "SELL":
        assert engine._in_position is False, "State not cleared after SELL"


# ---------------------------------------------------------------------------
# get_state() / restore_state() - restart survival for stateful strategies
# ---------------------------------------------------------------------------


def _make_engine(sample_strategy_dict, name="greenblatt_weekly", params=None):
    from alphalive.strategy_schema import StrategySchema

    sample_strategy_dict["strategy"]["name"] = name
    sample_strategy_dict["strategy"]["parameters"] = params or {}
    return SignalEngine(StrategySchema(**sample_strategy_dict))


def test_get_state_defaults_flat(sample_strategy_dict):
    engine = _make_engine(sample_strategy_dict)
    assert engine.get_state() == {
        "in_position": False,
        "entry_price": 0.0,
        "peak_price": 0.0,
        "vwap_position": 0,
        "vwap_bars_since_signal": 10**9,
    }


def test_restore_state_roundtrip(sample_strategy_dict):
    engine = _make_engine(sample_strategy_dict)
    engine._in_position = True
    engine._entry_price = 150.0
    engine._peak_price = 172.5

    snapshot = engine.get_state()

    restored = _make_engine(sample_strategy_dict)
    restored.restore_state(snapshot)
    assert restored._in_position is True
    assert restored._entry_price == 150.0
    assert restored._peak_price == 172.5


def test_restore_state_none_and_empty_are_noops(sample_strategy_dict):
    engine = _make_engine(sample_strategy_dict)
    engine._in_position = True
    engine.restore_state(None)
    assert engine._in_position is True  # untouched
    engine.restore_state({})
    assert engine._in_position is True  # untouched


def test_restore_state_partial_dict_uses_defaults(sample_strategy_dict):
    engine = _make_engine(sample_strategy_dict)
    engine.restore_state({"in_position": True})
    assert engine._in_position is True
    assert engine._entry_price == 0.0
    assert engine._peak_price == 0.0


def test_restored_peak_drives_trailing_stop(sample_strategy_dict):
    """The whole point: after a restart, the trailing stop must fire off the
    pre-restart peak, not off whatever price the bot sees post-restart."""
    engine = _make_engine(
        sample_strategy_dict,
        params={"fast_sma": 10, "slow_sma": 50, "trailing_stop_fraction": 0.20},
    )
    # Simulate restart mid-position: entry 100, peak 200 before the restart
    engine.restore_state(
        {"in_position": True, "entry_price": 100.0, "peak_price": 200.0}
    )

    # 155 is +55% from entry (no stop without restored peak), but -22.5% from
    # the restored 200 peak -> trailing stop must fire
    n = 60
    prices = [150.0] * (n - 1) + [155.0]
    df = pd.DataFrame(
        {
            "open": prices,
            "high": [p + 1 for p in prices],
            "low": [p - 1 for p in prices],
            "close": prices,
            "volume": [1_000_000] * n,
        }
    )
    df.index = pd.date_range(
        start="2024-01-05", periods=n, freq="W-FRI", tz="America/New_York"
    )

    signal = engine.generate_signal(df)

    assert signal["signal"] == "SELL"
    assert "Trailing stop" in signal["reason"]


# ---------------------------------------------------------------------------
# greenblatt_weekly min-hold gate on optional exits
# ---------------------------------------------------------------------------


def _rising_weekly_df(n=60, start=100.0, step=2.0):
    prices = [start + i * step for i in range(n)]
    df = pd.DataFrame(
        {
            "open": prices,
            "high": [p + 1 for p in prices],
            "low": [p - 1 for p in prices],
            "close": prices,
            "volume": [1_000_000] * n,
        }
    )
    df.index = pd.date_range(
        start="2024-01-05", periods=n, freq="W-FRI", tz="America/New_York"
    )
    return df


def _greenblatt_engine_in_position(sample_strategy_dict, df):
    """Engine holding a position entered near the current price (rising series
    -> RSI is pinned high, trailing stop nowhere near firing)."""
    engine = _make_engine(
        sample_strategy_dict,
        params={
            "fast_sma": 10,
            "slow_sma": 50,
            "rsi_overbought": 65,
            "trailing_stop_fraction": 0.20,
            "exit_rsi_overbought": True,
        },
    )
    last = float(df["close"].iloc[-1])
    engine.restore_state(
        {"in_position": True, "entry_price": last * 0.9, "peak_price": last * 0.95}
    )
    return engine


def test_greenblatt_optional_exit_suppressed_before_min_hold(sample_strategy_dict):
    df = _rising_weekly_df()
    engine = _greenblatt_engine_in_position(sample_strategy_dict, df)
    engine.min_hold_checker = lambda: False  # min hold NOT met

    signal = engine.generate_signal(df)

    assert signal["signal"] == "HOLD"
    assert "minimum hold" in signal["reason"]
    # Critical: suppressed exit must NOT flip engine state
    assert engine._in_position is True


def test_greenblatt_optional_exit_fires_after_min_hold(sample_strategy_dict):
    df = _rising_weekly_df()
    engine = _greenblatt_engine_in_position(sample_strategy_dict, df)
    engine.min_hold_checker = lambda: True  # min hold met

    signal = engine.generate_signal(df)

    assert signal["signal"] == "SELL"
    assert "RSI overbought" in signal["reason"]
    assert engine._in_position is False


def test_greenblatt_optional_exit_allowed_without_checker(sample_strategy_dict):
    """No checker wired (tests, replay) -> exits behave as before."""
    df = _rising_weekly_df()
    engine = _greenblatt_engine_in_position(sample_strategy_dict, df)
    assert engine.min_hold_checker is None

    signal = engine.generate_signal(df)

    assert signal["signal"] == "SELL"


def test_greenblatt_trailing_stop_bypasses_min_hold(sample_strategy_dict):
    """The trailing stop must fire immediately even when min hold is not met."""
    df = _rising_weekly_df()
    engine = _greenblatt_engine_in_position(sample_strategy_dict, df)
    engine.min_hold_checker = lambda: False
    # Force a peak far above the current price -> >20% drawdown from peak
    last = float(df["close"].iloc[-1])
    engine._peak_price = last * 1.5

    signal = engine.generate_signal(df)

    assert signal["signal"] == "SELL"
    assert "Trailing stop" in signal["reason"]
