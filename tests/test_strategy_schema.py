"""Regression tests for the 2026-07-13 audit's Group 1 cross-repo
export-contract bugs (see testing/reports/Alpha_Pipeline_Integrated_Audit_2026-07-13.md
§6, blockers 5/6). Covers the strict per-strategy parameter models
(bug 1.5) and the trailing_stop_pct/trailing_stop_fraction rename (bug 1.4).
"""

import copy

import pytest
from pydantic import ValidationError

from alphalive.strategy_schema import StrategySchema


def test_ma_crossover_rejects_unknown_field(sample_strategy_dict):
    """Bug 1.5 regression: strategy.parameters was previously an untyped
    Dict[str, Any] - any field name AlphaLive didn't happen to read was
    silently dropped. A field the strategy doesn't recognize (e.g. a typo
    or a leftover field from a different strategy) must now be rejected.
    """
    config = copy.deepcopy(sample_strategy_dict)
    config["strategy"]["parameters"][
        "short_window"
    ] = 10  # AlphaLab-internal name, never valid here

    with pytest.raises(ValidationError):
        StrategySchema(**config)


def test_momentum_breakout_rejects_alphalab_internal_names(sample_strategy_dict):
    """Bug 1.2/1.3 regression: a raw AlphaLab-internal-name payload
    (volume_surge_pct/volume_avg_period, pre export-mapping) must be
    rejected rather than silently absorbed with AlphaLive falling back to
    defaults."""
    config = copy.deepcopy(sample_strategy_dict)
    config["strategy"]["name"] = "momentum_breakout"
    config["strategy"]["parameters"] = {
        "lookback": 20,
        "volume_surge_pct": 150,  # AlphaLab's export-mapping should have renamed this to surge_pct
        "volume_avg_period": 20,  # ...and this to volume_ma_period
    }

    with pytest.raises(ValidationError):
        StrategySchema(**config)


def test_momentum_breakout_accepts_canonical_names(sample_strategy_dict):
    """The post-translation field names (as AlphaLab's export-mapping layer
    now emits them) must validate and be usable via plain dict .get()."""
    config = copy.deepcopy(sample_strategy_dict)
    config["strategy"]["name"] = "momentum_breakout"
    config["strategy"]["parameters"] = {
        "strategy_type": "momentum_breakout",
        "lookback": 20,
        "surge_pct": 1.5,
        "volume_ma_period": 20,
    }

    schema = StrategySchema(**config)
    assert schema.strategy.parameters["surge_pct"] == 1.5
    assert schema.strategy.parameters["volume_ma_period"] == 20


def test_bollinger_breakout_rejects_alphalab_internal_names(sample_strategy_dict):
    """Bug 1.3 regression: bb_period/bb_std_dev (AlphaLab-internal) must
    not silently pass as bollinger_breakout parameters."""
    config = copy.deepcopy(sample_strategy_dict)
    config["strategy"]["name"] = "bollinger_breakout"
    config["strategy"]["parameters"] = {"bb_period": 20, "bb_std_dev": 2.0}

    with pytest.raises(ValidationError):
        StrategySchema(**config)


def test_greenblatt_trailing_stop_pct_rejected(sample_strategy_dict):
    """Bug 1.4 regression: the strategy-level trailing stop is now named
    trailing_stop_fraction specifically so it can never be confused with
    risk.trailing_stop_pct (an absolute percentage, not a fraction). The
    old name must be rejected here, not silently accepted with the wrong
    unit convention.
    """
    config = copy.deepcopy(sample_strategy_dict)
    config["strategy"]["name"] = "greenblatt_weekly"
    config["strategy"]["parameters"] = {
        "fast_sma": 10,
        "slow_sma": 50,
        "trailing_stop_pct": 0.20,  # old, now-invalid name for this field
    }

    with pytest.raises(ValidationError):
        StrategySchema(**config)


def test_greenblatt_trailing_stop_fraction_accepted_and_distinct_from_risk_pct(
    sample_strategy_dict,
):
    config = copy.deepcopy(sample_strategy_dict)
    config["strategy"]["name"] = "greenblatt_weekly"
    config["strategy"]["parameters"] = {
        "fast_sma": 10,
        "slow_sma": 50,
        "trailing_stop_fraction": 0.20,
    }
    config["risk"]["trailing_stop_pct"] = 3.0

    schema = StrategySchema(**config)
    assert schema.strategy.parameters["trailing_stop_fraction"] == 0.20
    assert schema.risk.trailing_stop_pct == 3.0


def test_rsi_mean_reversion_default_period(sample_strategy_dict):
    """Neither period nor rsi_period supplied: unaffected by the 2026-08-15
    alias-reconciliation fix, period keeps its default of 14."""
    config = copy.deepcopy(sample_strategy_dict)
    config["strategy"]["name"] = "rsi_mean_reversion"
    config["strategy"]["parameters"] = {}

    schema = StrategySchema(**config)
    assert schema.strategy.parameters["period"] == 14


def test_rsi_mean_reversion_rsi_period_alias_takes_effect(sample_strategy_dict):
    """2026-08-15 bug fix: a real AlphaLab export only ever sets rsi_period
    (never period). That non-default value must now actually reach
    signal_engine.py instead of being silently discarded in favor of the
    period field's own default of 14."""
    config = copy.deepcopy(sample_strategy_dict)
    config["strategy"]["name"] = "rsi_mean_reversion"
    config["strategy"]["parameters"] = {"rsi_period": 9}

    schema = StrategySchema(**config)
    assert schema.strategy.parameters["period"] == 9


def test_rsi_mean_reversion_legacy_period_only_config_unaffected(sample_strategy_dict):
    """Backward compatibility: an old-style AlphaLive config that only ever
    set period (predating rsi_period's existence in either schema) must
    keep working exactly as before."""
    config = copy.deepcopy(sample_strategy_dict)
    config["strategy"]["name"] = "rsi_mean_reversion"
    config["strategy"]["parameters"] = {"period": 21}

    schema = StrategySchema(**config)
    assert schema.strategy.parameters["period"] == 21


def test_rsi_mean_reversion_matching_aliases_no_conflict(sample_strategy_dict):
    """Both fields set but agreeing: not a conflict, and behaves the same
    as either alone."""
    config = copy.deepcopy(sample_strategy_dict)
    config["strategy"]["name"] = "rsi_mean_reversion"
    config["strategy"]["parameters"] = {"period": 21, "rsi_period": 21}

    schema = StrategySchema(**config)
    assert schema.strategy.parameters["period"] == 21


def test_rsi_mean_reversion_conflicting_aliases_rejected(sample_strategy_dict):
    """Both fields explicitly set to different values must fail loudly at
    config-load time rather than silently picking one - an ambiguous
    export/config should never produce an unpredictable live parameter."""
    config = copy.deepcopy(sample_strategy_dict)
    config["strategy"]["name"] = "rsi_mean_reversion"
    config["strategy"]["parameters"] = {"period": 9, "rsi_period": 21}

    with pytest.raises(ValidationError):
        StrategySchema(**config)


def test_unregistered_strategy_name_raises(sample_strategy_dict):
    """A strategy name with no registered parameter model must fail
    loudly at load time, not silently accept arbitrary parameters."""
    config = copy.deepcopy(sample_strategy_dict)
    config["strategy"]["name"] = "not_a_real_strategy"

    with pytest.raises(ValidationError):
        StrategySchema(**config)
