"""
Tests for weekly-bar completeness (2026-09-11 pass 3, objective 2):
market_data._resample_to_weekly() must never evaluate against the current,
still-incomplete trading week, and must not blindly discard a genuinely
complete final week either. The completeness cutoff is `min(last daily bar
in the data, explicit as_of)` - no wall-clock access inside the function
itself, so every test below passes as_of explicitly and needs no mocking of
datetime.now().
"""

from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from alphalive.data.market_data import MarketDataFetcher

ET = ZoneInfo("America/New_York")


@pytest.fixture
def fetcher():
    return MarketDataFetcher.__new__(
        MarketDataFetcher
    )  # bypass __init__, no broker needed


def _daily_df(start, end):
    dates = pd.bdate_range(start, end, tz=ET)
    n = len(dates)
    return pd.DataFrame(
        {
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [1000] * n,
        },
        index=pd.DatetimeIndex(dates),
    )


def _ts(s):
    return pd.Timestamp(s, tz=ET)


# ---------------------------------------------------------------------------
# Monday / Tuesday-after-holiday / midweek
# ---------------------------------------------------------------------------


def test_monday_during_market_hours_uses_only_previous_completed_week(fetcher):
    """Data includes Monday 1/15 (today, market open) through Fri 1/12
    (previous week) - as_of is Monday midday. The 1/15 week (only Monday
    present) must be dropped; the 1/8-1/12 week must remain."""
    df = _daily_df("2024-01-08", "2024-01-15")  # Mon 1/8 .. Mon 1/15
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-15 10:00"))

    assert _ts("2024-01-19") not in weekly.index  # current week's Friday - not reached
    assert _ts("2024-01-12") in weekly.index  # previous week, complete


def test_tuesday_after_monday_holiday_uses_previous_completed_week(fetcher):
    """Monday 1/15 2024 was MLK Day (market closed) - no bar for it. Data
    jumps from Fri 1/12 to Tue 1/16. as_of is Tuesday. The in-progress
    1/16 week must still be excluded; only the prior completed week
    remains."""
    dates = [
        d
        for d in pd.bdate_range("2024-01-08", "2024-01-16", tz=ET)
        if d != _ts("2024-01-15")
    ]
    df = pd.DataFrame(
        {
            "open": [100.0 + i for i in range(len(dates))],
            "high": [101.0 + i for i in range(len(dates))],
            "low": [99.0 + i for i in range(len(dates))],
            "close": [100.5 + i for i in range(len(dates))],
            "volume": [1000] * len(dates),
        },
        index=pd.DatetimeIndex(dates),
    )
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-16 10:00"))

    assert _ts("2024-01-19") not in weekly.index
    assert _ts("2024-01-12") in weekly.index


def test_wednesday_incomplete_current_week_excluded(fetcher):
    df = _daily_df("2024-01-08", "2024-01-17")  # through Wed 1/17
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-17 12:00"))

    assert _ts("2024-01-19") not in weekly.index
    assert _ts("2024-01-12") in weekly.index


# ---------------------------------------------------------------------------
# Completed Friday / weekend
# ---------------------------------------------------------------------------


def test_completed_friday_retains_that_weeks_bar(fetcher):
    """Data through Friday 1/12 close, as_of also Friday afternoon (market
    closed for the day) - the just-completed week must be RETAINED, not
    discarded."""
    df = _daily_df("2024-01-08", "2024-01-12")
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-12 16:30"))

    assert _ts("2024-01-12") in weekly.index
    assert weekly.loc[_ts("2024-01-12"), "close"] == df["close"].iloc[-1]


def test_saturday_after_completed_week_retains_friday_bar(fetcher):
    df = _daily_df("2024-01-08", "2024-01-12")
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-13 09:00"))  # Saturday

    assert _ts("2024-01-12") in weekly.index


def test_sunday_after_completed_week_retains_friday_bar(fetcher):
    df = _daily_df("2024-01-08", "2024-01-12")
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-14 09:00"))  # Sunday

    assert _ts("2024-01-12") in weekly.index


# ---------------------------------------------------------------------------
# Historical / backtest-style datasets (as_of far in the "future" relative
# to the data, or omitted entirely) - never wall-clock-driven.
# ---------------------------------------------------------------------------


def test_historical_dataset_ending_on_older_completed_friday_is_retained(fetcher):
    """A historical dataset that ends on a Friday from years ago, evaluated
    with an as_of also from that same time - the old completed week is
    retained, not treated as "incomplete" just because it's not recent."""
    df = _daily_df("2020-03-02", "2020-03-06")  # Mon-Fri, March 2020
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2020-03-06 16:00"))

    assert _ts("2020-03-06") in weekly.index


def test_historical_dataset_ending_midweek_excludes_partial_week(fetcher):
    """A historical dataset ending on a Wednesday (e.g. a backtest fixture
    someone trimmed) must exclude that partial week according to explicit
    as-of completeness, not because "today" is a Wednesday."""
    df = _daily_df("2020-03-02", "2020-03-04")  # Mon-Wed only
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2020-03-04 16:00"))

    assert _ts("2020-03-06") not in weekly.index
    assert len(weekly) == 0  # only one (incomplete) week existed at all


def test_as_of_omitted_falls_back_to_last_daily_bar_as_cutoff(fetcher):
    """No explicit as_of - purely data-driven using the last daily bar,
    matching the pre-pass-3 default behavior for any caller that doesn't
    pass one."""
    df = _daily_df("2024-01-08", "2024-01-17")  # ends midweek (Wed)
    weekly = fetcher._resample_to_weekly(df)  # no as_of

    assert _ts("2024-01-19") not in weekly.index
    assert _ts("2024-01-12") in weekly.index


def test_as_of_later_than_data_does_not_leak_future_information(fetcher):
    """as_of claims a much later date than the data actually covers - the
    cutoff must still be capped by the data's own last bar (min of the
    two), never inventing a "complete" week the data doesn't support."""
    df = _daily_df("2024-01-08", "2024-01-17")  # ends midweek (Wed 1/17)
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-06-01"))

    assert _ts("2024-01-19") not in weekly.index  # still incomplete per the DATA


def test_data_extends_past_as_of_does_not_leak_future_information(fetcher):
    """The data (unusually) contains bars past the explicit as_of - the
    cutoff must still be capped by as_of, never using bars beyond the
    evaluation time even if they're technically present in the frame."""
    df = _daily_df("2024-01-08", "2024-01-19")  # through the following Friday
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-17 12:00"))  # Wed

    assert _ts("2024-01-19") not in weekly.index  # not reached as of Wednesday
    assert _ts("2024-01-12") in weekly.index


# ---------------------------------------------------------------------------
# DST weeks
# ---------------------------------------------------------------------------


def test_dst_spring_forward_week_completeness_unaffected(fetcher):
    """US/Eastern DST began 2024-03-10 (a Sunday) - the week of 3/4-3/8
    straddles no DST boundary itself, but the SURROUNDING evaluation
    happens right at the transition. Date-only comparison must be
    unaffected by the UTC-offset change."""
    df = _daily_df("2024-03-04", "2024-03-08")  # Mon-Fri, week before DST starts
    weekly = fetcher._resample_to_weekly(
        df, as_of=_ts("2024-03-11 10:00")
    )  # Monday after DST

    assert _ts("2024-03-08") in weekly.index


def test_dst_fall_back_week_completeness_unaffected(fetcher):
    """DST ended 2024-11-03 (a Sunday). Data through Fri 11/1 (before the
    transition), as_of the following Monday (after the transition)."""
    df = _daily_df("2024-10-28", "2024-11-01")
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-11-04 10:00"))

    assert _ts("2024-11-01") in weekly.index


# ---------------------------------------------------------------------------
# Empty and single-week inputs
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_output(fetcher):
    df = pd.DataFrame(
        {"open": [], "high": [], "low": [], "close": [], "volume": []},
        index=pd.DatetimeIndex([], tz=ET),
    )
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-15"))
    assert len(weekly) == 0


def test_single_complete_week_input_retained(fetcher):
    df = _daily_df("2024-01-08", "2024-01-12")  # exactly one Mon-Fri week
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-12 16:30"))
    assert len(weekly) == 1
    assert _ts("2024-01-12") in weekly.index


def test_single_incomplete_week_input_dropped_to_empty(fetcher):
    df = _daily_df("2024-01-08", "2024-01-09")  # Mon-Tue only
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-09 12:00"))
    assert len(weekly) == 0


# ---------------------------------------------------------------------------
# Friday session-completion boundary (2026-09-11 pass 4): a Friday-at-
# midnight weekly label must not be treated as "complete" merely because
# the calendar date arrived - the trading session on that Friday has to
# have actually closed. Reproduced directly against the OLD (date-only,
# midnight-normalized) comparison before this fix: as_of Friday 09:45 with
# a same-day partial Friday bar present in the data used to retain that
# week (a real defect - see market_data.py's _MARKET_CLOSE_TIME).
# ---------------------------------------------------------------------------


def test_friday_before_market_close_with_partial_current_friday_bar_excluded(fetcher):
    """A same-day (still-forming) Friday daily bar is present in the data,
    and as_of is Friday morning (market open, session not closed) - this
    week must be excluded even though a Friday bar already exists, because
    the SESSION hasn't closed yet. This is the exact scenario a naive
    date-only (midnight-normalized) comparison gets wrong."""
    df = _daily_df("2024-01-08", "2024-01-12")  # Mon-Fri, including today's bar
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-12 09:45"))

    assert _ts("2024-01-12") not in weekly.index
    assert len(weekly) == 0  # this was the only week and it's incomplete


def test_friday_after_market_close_with_completed_bar_retained(fetcher):
    df = _daily_df("2024-01-08", "2024-01-12")
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-12 16:30"))

    assert _ts("2024-01-12") in weekly.index


def test_friday_exactly_at_market_close_boundary_is_retained(fetcher):
    """as_of at exactly the close boundary (16:00) counts as complete -
    the boundary is inclusive ("at or after")."""
    df = _daily_df("2024-01-08", "2024-01-12")
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-12 16:00"))

    assert _ts("2024-01-12") in weekly.index


def test_friday_one_minute_before_close_boundary_is_excluded(fetcher):
    df = _daily_df("2024-01-08", "2024-01-12")
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-12 15:59"))

    assert _ts("2024-01-12") not in weekly.index


def test_friday_when_upstream_api_returns_only_completed_daily_bars(fetcher):
    """The data provider itself never returned a same-day Friday bar (data
    stops at Thursday) even though as_of is Friday afternoon - the data-
    driven check alone already excludes the week regardless of the
    session-completion check."""
    df = _daily_df("2024-01-08", "2024-01-11")  # Mon-Thu only
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-12 16:30"))

    assert _ts("2024-01-12") not in weekly.index
    assert len(weekly) == 0


def test_weekend_after_completed_friday_retains_that_week(fetcher):
    df = _daily_df("2024-01-08", "2024-01-12")
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-13 10:00"))  # Saturday

    assert _ts("2024-01-12") in weekly.index


def test_historical_dataset_ending_on_friday_no_as_of_retains_week(fetcher):
    """Historical/backtest usage with no as_of at all - purely data-driven,
    no session-completion check applies (there's no wall clock to
    reference), and a dataset that ends cleanly on a Friday is retained."""
    df = _daily_df("2020-03-02", "2020-03-06")
    weekly = fetcher._resample_to_weekly(df)  # no as_of

    assert _ts("2020-03-06") in weekly.index


def test_daily_bars_normalized_to_midnight_unaffected(fetcher):
    """Daily bars whose timestamps are already exactly midnight (the
    normal case for daily OHLCV) are handled identically to bars with a
    nonzero time component - the fix only changes how `as_of` (not the
    daily bar timestamps) is compared."""
    dates = pd.bdate_range("2024-01-08", "2024-01-12", tz=ET)
    df = pd.DataFrame(
        {
            "open": [100.0] * len(dates),
            "high": [101.0] * len(dates),
            "low": [99.0] * len(dates),
            "close": [100.5] * len(dates),
            "volume": [1000] * len(dates),
        },
        index=pd.DatetimeIndex(dates),  # already midnight-normalized
    )
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-12 16:30"))
    assert _ts("2024-01-12") in weekly.index


def test_timezone_aware_as_of_against_timezone_naive_daily_bars(fetcher):
    """df is tz-naive (no tz on the index) but as_of is tz-aware - must not
    raise, and must still apply the session-completion check correctly."""
    dates = pd.bdate_range("2024-01-08", "2024-01-12")  # no tz
    df = pd.DataFrame(
        {
            "open": [100.0 + i for i in range(len(dates))],
            "high": [101.0 + i for i in range(len(dates))],
            "low": [99.0 + i for i in range(len(dates))],
            "close": [100.5 + i for i in range(len(dates))],
            "volume": [1000] * len(dates),
        },
        index=pd.DatetimeIndex(dates),
    )
    weekly_morning = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-12 09:45"))
    assert pd.Timestamp("2024-01-12") not in weekly_morning.index

    weekly_afternoon = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-12 16:30"))
    assert pd.Timestamp("2024-01-12") in weekly_afternoon.index


def test_timezone_naive_as_of_against_timezone_aware_daily_bars(fetcher):
    """df is tz-aware (ET) but as_of is a naive Timestamp - must be
    localized to df's tz rather than raising or silently miscomparing."""
    df = _daily_df("2024-01-08", "2024-01-12")  # tz=ET
    naive_as_of = pd.Timestamp("2024-01-12 09:45")  # no tz

    weekly = fetcher._resample_to_weekly(df, as_of=naive_as_of)
    assert _ts("2024-01-12") not in weekly.index


def test_early_close_friday_not_specially_handled_documented_limitation(fetcher):
    """No early-close-calendar abstraction exists in this codebase - an
    early close (e.g. 13:00 ET) is NOT recognized as session-complete by
    this function; it uses the ordinary 16:00 close boundary regardless.
    This test documents that current, honest limitation rather than
    asserting a capability that doesn't exist."""
    df = _daily_df("2024-01-08", "2024-01-12")
    # 14:00 - after a hypothetical 13:00 early close, but before the
    # ordinary 16:00 boundary this function actually uses.
    weekly = fetcher._resample_to_weekly(df, as_of=_ts("2024-01-12 14:00"))
    assert _ts("2024-01-12") not in weekly.index  # still treated as incomplete


# ---------------------------------------------------------------------------
# Daily bars remain unchanged (this function is only invoked for 1Week)
# ---------------------------------------------------------------------------


def test_daily_dataframe_itself_is_never_mutated_by_resample(fetcher):
    df = _daily_df("2024-01-08", "2024-01-17")
    original_len = len(df)
    original_close = df["close"].copy()

    fetcher._resample_to_weekly(df, as_of=_ts("2024-01-17 12:00"))

    assert len(df) == original_len
    assert (df["close"] == original_close).all()
