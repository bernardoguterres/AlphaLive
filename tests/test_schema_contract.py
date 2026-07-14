"""Cross-repo schema contract test: AlphaLab's StrategyExportSchema vs
AlphaLive's StrategySchema.

Audit finding (E2E_Simulation_CodeQuality_Audit_2026-07-13, Part 14 #4): the
two schemas are hand-maintained Pydantic model trees with no shared source
or codegen - a field renamed/added on one side and not the other silently
drifts (this is exactly how the ma_crossover fast_period/slow_period vs.
short_window/long_window bug and the greenblatt_weekly trailing_stop_pct
unit-collision bug happened). This test is the "minimum bar" fix the audit
recommended: not a shared package, just a field-name-parity check between
the two live schema definitions that fails loudly on new drift.

Mirrored in AlphaLab/backend/tests/test_schema_contract.py so it fires in
either repo's test run.

Both repos are expected to live as sibling directories (their actual
deployment/checkout layout on this machine); if AlphaLab isn't present at
the expected relative path (e.g. an isolated single-repo checkout), the
whole module is skipped rather than failed.
"""

import sys
from pathlib import Path

import pytest

_ALPHALAB_BACKEND = Path(__file__).resolve().parents[2] / "AlphaLab" / "backend"

if not _ALPHALAB_BACKEND.is_dir():
    pytest.skip(
        f"AlphaLab not found at {_ALPHALAB_BACKEND} - schema contract test "
        f"requires both repos checked out as sibling directories",
        allow_module_level=True,
    )

sys.path.insert(0, str(_ALPHALAB_BACKEND))

from strategy_schema import (  # noqa: E402
    BacktestPeriod as LabBacktestPeriod,
    ExecutionConfig,
    MetadataConfig,
    PerformanceMetrics,
    RiskConfig,
    SafetyLimitsConfig,
)
from strategy_schema import (  # noqa: E402
    MACrossoverParams,
    RSIMeanReversionParams,
    MomentumBreakoutParams,
    BollingerBreakoutParams,
    VWAPReversionParams,
    GreenblattWeeklyParams,
    BollingerRSIComboParams,
    TrendAdaptiveRSIParams,
)

from alphalive.strategy_schema import (  # noqa: E402
    BacktestPeriod as LiveBacktestPeriod,
    Execution,
    Metadata,
    Performance,
    Risk,
    SafetyLimits,
)
from alphalive.strategy_schema import (  # noqa: E402
    MACrossoverStrategyParams,
    RSIMeanReversionStrategyParams,
    MomentumBreakoutStrategyParams,
    BollingerBreakoutStrategyParams,
    VWAPReversionStrategyParams,
    GreenblattWeeklyStrategyParams,
    BollingerRSIComboStrategyParams,
    TrendAdaptiveRSIStrategyParams,
)


def _assert_field_parity(lab_cls, live_cls, *, lab_only=frozenset(), live_only=frozenset()):
    """Assert lab_cls and live_cls declare the same Pydantic field names,
    modulo the explicitly documented exceptions in lab_only/live_only."""
    lab_fields = set(lab_cls.model_fields.keys())
    live_fields = set(live_cls.model_fields.keys())

    unexpected_lab_only = (lab_fields - live_fields) - lab_only
    unexpected_live_only = (live_fields - lab_fields) - live_only

    assert not unexpected_lab_only, (
        f"{lab_cls.__name__} has fields {lab_cls.__name__} exports that "
        f"{live_cls.__name__} does not read: {unexpected_lab_only}. "
        f"AlphaLive would silently ignore these values from a real export."
    )
    assert not unexpected_live_only, (
        f"{live_cls.__name__} expects fields not present in {lab_cls.__name__}: "
        f"{unexpected_live_only}. If AlphaLab never exports these, AlphaLive "
        f"silently falls back to its own defaults instead of the backtested value."
    )


# ---------------------------------------------------------------------------
# Envelope blocks (Risk, Execution, SafetyLimits, Metadata, BacktestPeriod,
# Performance) - all field sets match exactly today, no known exceptions.
# ---------------------------------------------------------------------------

def test_backtest_period_field_parity():
    _assert_field_parity(LabBacktestPeriod, LiveBacktestPeriod)


def test_performance_field_parity():
    _assert_field_parity(PerformanceMetrics, Performance)


def test_metadata_field_parity():
    _assert_field_parity(MetadataConfig, Metadata)


def test_risk_field_parity():
    _assert_field_parity(RiskConfig, Risk)


def test_execution_field_parity():
    _assert_field_parity(ExecutionConfig, Execution)


def test_safety_limits_field_parity():
    _assert_field_parity(SafetyLimitsConfig, SafetyLimits)


# ---------------------------------------------------------------------------
# Per-strategy parameter blocks. rsi_simple is excluded - AlphaLab supports
# it as a backtestable strategy but it isn't in AlphaLive's StrategyName
# literal (not currently live-deployable), so there is no AlphaLive side to
# compare against.
# ---------------------------------------------------------------------------

def test_ma_crossover_param_field_parity():
    _assert_field_parity(MACrossoverParams, MACrossoverStrategyParams)


def test_rsi_mean_reversion_param_field_parity():
    # Known, documented gap (AlphaLive's own docstring, "audit Blocker #3"):
    # AlphaLive's signal_engine.py reads "period", not the "rsi_period" name
    # AlphaLab currently exports. AlphaLive accepts rsi_period too (as an
    # unused optional) so real exports don't hard-fail, and "period" has a
    # usable default (14) so it isn't silently wrong - but it IS silently
    # not using the backtested value. This is the parity gap's file:line
    # record; the actual fix is a separate, later cross-repo session.
    _assert_field_parity(
        RSIMeanReversionParams,
        RSIMeanReversionStrategyParams,
        live_only={"period"},
    )


def test_momentum_breakout_param_field_parity():
    # atr_period is an AlphaLive-only tunable (indicators.py/signal_engine.py
    # both default it to 14) - AlphaLab has no equivalent internal concept to
    # export, this isn't a dropped backtested value.
    _assert_field_parity(
        MomentumBreakoutParams,
        MomentumBreakoutStrategyParams,
        live_only={"atr_period"},
    )


def test_bollinger_breakout_param_field_parity():
    # volume_ma_period is an AlphaLive-only tunable (indicators.py defaults
    # it to 20), not a value AlphaLab's BollingerBreakout backtest computes
    # or exports.
    _assert_field_parity(
        BollingerBreakoutParams,
        BollingerBreakoutStrategyParams,
        live_only={"volume_ma_period"},
    )


def test_vwap_reversion_param_field_parity():
    # vwap_std_period is an AlphaLive-only override (indicators.py defaults
    # it to vwap_period itself if unset) - not part of AlphaLab's export.
    _assert_field_parity(
        VWAPReversionParams,
        VWAPReversionStrategyParams,
        live_only={"vwap_std_period"},
    )


def test_bollinger_rsi_combo_param_field_parity():
    _assert_field_parity(BollingerRSIComboParams, BollingerRSIComboStrategyParams)


def test_trend_adaptive_rsi_param_field_parity():
    _assert_field_parity(TrendAdaptiveRSIParams, TrendAdaptiveRSIStrategyParams)


def test_greenblatt_weekly_param_field_parity():
    _assert_field_parity(GreenblattWeeklyParams, GreenblattWeeklyStrategyParams)


def test_no_new_strategy_types_silently_added_without_a_parity_test():
    """Guard against a 9th shared strategy being added to both schemas
    without a corresponding parity test above (the whole point of this
    file - a strategy present in both StrategyName literals but never
    diffed here would defeat the contract test silently)."""
    from alphalive.strategy_schema import _STRATEGY_PARAM_MODELS as live_models
    from strategy_schema import StrategyName as LabStrategyName

    lab_strategy_names = set(LabStrategyName.__args__)
    live_strategy_names = set(live_models.keys())
    shared = lab_strategy_names & live_strategy_names

    covered_by_this_file = {
        "ma_crossover",
        "rsi_mean_reversion",
        "momentum_breakout",
        "bollinger_breakout",
        "vwap_reversion",
        "bollinger_rsi_combo",
        "trend_adaptive_rsi",
        "greenblatt_weekly",
    }

    assert shared == covered_by_this_file, (
        f"Strategies shared by both schemas changed: {shared}. "
        f"Add/remove a corresponding _assert_field_parity test above and "
        f"update covered_by_this_file to match."
    )
