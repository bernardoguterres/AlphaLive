"""
CLI safety tests for tests/fixtures/generate_expected_signals.py
(2026-09-11 pass 3, objective 4): the generator must never silently
overwrite tracked/fixed fixture paths - it requires an explicit output
directory, explicit strategy selection, and an explicit --overwrite flag
before touching an existing file.

Every test here runs the script only against tmp_path directories via
subprocess and asserts the repository's own tests/fixtures/ directory is
never touched. No git, no network, no credentials.
"""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent / "fixtures" / "generate_expected_signals.py"
REPO_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _run(args, timeout=60):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _repo_fixture_mtimes():
    return {
        p: p.stat().st_mtime for p in REPO_FIXTURES_DIR.glob("expected_signals_*.csv")
    }


@pytest.fixture(autouse=True)
def _guard_repo_fixtures():
    """Fails the test (rather than silently passing) if anything in this
    file actually touches a tracked fixture file - the exact regression
    this hardening pass exists to prevent."""
    before = _repo_fixture_mtimes()
    yield
    after = _repo_fixture_mtimes()
    assert before == after, "A test in this file modified a tracked repo fixture!"


# ---------------------------------------------------------------------------
# Missing / invalid arguments
# ---------------------------------------------------------------------------


def test_missing_output_dir_is_rejected():
    result = _run(["--all-strategies"])
    assert result.returncode != 0
    assert "--output-dir" in result.stderr


def test_missing_strategy_selection_is_rejected(tmp_path):
    result = _run(["--output-dir", str(tmp_path)])
    assert result.returncode != 0
    assert "--strategy" in result.stderr or "required" in result.stderr.lower()


def test_strategy_and_all_strategies_together_is_rejected(tmp_path):
    result = _run(
        [
            "--output-dir",
            str(tmp_path),
            "--strategy",
            "ma_crossover",
            "--all-strategies",
        ]
    )
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# Existing file without --overwrite
# ---------------------------------------------------------------------------


def test_existing_file_without_overwrite_is_refused(tmp_path):
    target = tmp_path / "expected_signals_ma_crossover.csv"
    target.write_text("bar_index,signal\n0,HOLD\n")
    original_content = target.read_text()

    result = _run(["--output-dir", str(tmp_path), "--strategy", "ma_crossover"])

    assert result.returncode != 0
    assert "overwrite" in (result.stdout + result.stderr).lower()
    assert target.read_text() == original_content  # untouched


def test_existing_file_with_overwrite_succeeds(tmp_path):
    target = tmp_path / "expected_signals_ma_crossover.csv"
    target.write_text("bar_index,signal\n0,HOLD\n")

    result = _run(
        [
            "--output-dir",
            str(tmp_path),
            "--strategy",
            "ma_crossover",
            "--overwrite",
        ]
    )

    assert result.returncode == 0
    assert target.read_text() != "bar_index,signal\n0,HOLD\n"


# ---------------------------------------------------------------------------
# Explicit single strategy / all strategies / temporary output
# ---------------------------------------------------------------------------


def test_explicit_single_strategy_generates_only_that_file(tmp_path):
    result = _run(["--output-dir", str(tmp_path), "--strategy", "vwap_reversion"])

    assert result.returncode == 0
    produced = sorted(p.name for p in tmp_path.glob("expected_signals_*.csv"))
    assert produced == ["expected_signals_vwap_reversion.csv"]


def test_explicit_multiple_strategies_generates_only_those(tmp_path):
    result = _run(
        [
            "--output-dir",
            str(tmp_path),
            "--strategy",
            "ma_crossover",
            "--strategy",
            "momentum_breakout",
        ]
    )

    assert result.returncode == 0
    produced = sorted(p.name for p in tmp_path.glob("expected_signals_*.csv"))
    assert produced == [
        "expected_signals_ma_crossover.csv",
        "expected_signals_momentum_breakout.csv",
    ]


def test_explicit_all_strategies_generates_every_known_strategy(tmp_path):
    result = _run(["--output-dir", str(tmp_path), "--all-strategies"])

    assert result.returncode == 0
    produced = sorted(
        p.stem.replace("expected_signals_", "") for p in tmp_path.glob("*.csv")
    )
    assert produced == [
        "bollinger_breakout",
        "ma_crossover",
        "momentum_breakout",
        "rsi_mean_reversion",
        "vwap_reversion",
    ]


def test_output_written_to_temporary_directory_only(tmp_path):
    """The generated files land only under the explicit tmp_path - nothing
    is written elsewhere (e.g. cwd, the script's own directory)."""
    result = _run(["--output-dir", str(tmp_path), "--strategy", "ma_crossover"])

    assert result.returncode == 0
    assert (tmp_path / "expected_signals_ma_crossover.csv").exists()
    # Nothing written next to the script itself.
    assert not (SCRIPT.parent / "expected_signals_ma_crossover.csv.new").exists()


def test_dry_listing_printed_before_writing(tmp_path):
    result = _run(["--output-dir", str(tmp_path), "--all-strategies"])
    assert result.returncode == 0
    assert "Files that will be written" in result.stdout


def test_output_labeled_as_self_generated_not_parity_evidence(tmp_path):
    result = _run(["--output-dir", str(tmp_path), "--strategy", "ma_crossover"])
    assert result.returncode == 0
    combined = result.stdout.lower()
    assert "self-generated" in combined
    assert "not" in combined and "parity" in combined
