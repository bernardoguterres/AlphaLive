"""Process outcome reporting without credentials, network or broker imports."""

import importlib.util
import signal
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "paper_readonly_check.py"
SPEC = importlib.util.spec_from_file_location("paper_check_outcomes", SCRIPT)
prc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prc)


class PaperCheckOutcomesTests(unittest.TestCase):
    def test_nonzero_startup_exit_is_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = Mock(returncode=17)
            child.wait.return_value = 17
            with patch.object(prc.subprocess, "Popen", return_value=child):
                result = prc.run_session(
                    {"DRY_RUN": "true"}, root, root / "config.json", "startup", 1
                )
            output = StringIO()
            with redirect_stdout(output):
                summary = prc.summarize(result.log, "startup", result)
            self.assertFalse(result.ok)
            self.assertEqual(summary["session"].returncode, 17)
            self.assertIn("FAILED (exited early, exit code 17)", output.getvalue())
            child.send_signal.assert_not_called()

    def test_intentional_deadline_stops_are_clean_but_other_exits_fail(self):
        cases = [
            (-signal.SIGTERM, [subprocess.TimeoutExpired("run", 1), 0], True),
            (
                -signal.SIGKILL,
                [
                    subprocess.TimeoutExpired("run", 1),
                    subprocess.TimeoutExpired("run", 30),
                    0,
                ],
                True,
            ),
            (17, [subprocess.TimeoutExpired("run", 1), 17], False),
            (-signal.SIGTERM, [-signal.SIGTERM], False),
        ]
        for code, waits, ok in cases:
            with self.subTest(code=code, waits=len(waits)):
                with tempfile.TemporaryDirectory() as directory:
                    child = Mock(returncode=code)
                    child.wait.side_effect = waits
                    with patch.object(prc.subprocess, "Popen", return_value=child):
                        result = prc.run_session(
                            {"DRY_RUN": "true"}, Path(directory), SCRIPT, "session", 1
                        )
                    self.assertEqual(result.ok, ok)
                    if len(waits) > 1:
                        child.send_signal.assert_called_once_with(signal.SIGTERM)
                    if len(waits) == 3:
                        child.kill.assert_called_once_with()

    def test_cli_status_includes_both_sessions_and_probe_failures(self):
        cases = [
            (0, 0, 0, 0, "Check outcome: OK"),
            (17, 0, 0, 1, "Session outcome: FAILED"),
            (0, 17, 0, 1, "Session outcome: FAILED"),
            (0, 0, 23, 1, "Probe outcome: FAILED (exit code 23)"),
            (
                0,
                0,
                subprocess.TimeoutExpired("probe", 120),
                1,
                "Probe outcome: FAILED (timeout)",
            ),
            (
                0,
                0,
                OSError("cannot launch child"),
                1,
                "Probe outcome: FAILED (could not start)",
            ),
        ]
        for first, restart, probe, expected, message in cases:
            with self.subTest(first=first, restart=restart, probe=probe):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    config = root / "config.json"
                    config.write_text('{"ticker":"SPY"}')
                    codes = iter([first, restart])

                    def session(env, run_dir, config, tag, seconds):
                        log = run_dir / f"{tag}.log"
                        log.write_text("startup log\n")
                        return prc.SessionResult(log, next(codes), False)

                    output = StringIO()
                    with patch.object(
                        prc, "run_session", side_effect=session
                    ), patch.object(prc.time, "sleep"), patch.object(
                        prc.subprocess, "run"
                    ) as run, redirect_stdout(
                        output
                    ):
                        if isinstance(probe, Exception):
                            run.side_effect = probe
                        else:
                            run.return_value = subprocess.CompletedProcess(
                                ["probe"], probe, "probe output\n", "probe error\n"
                            )
                        code = prc.main(
                            [
                                "--simulator",
                                "http://127.0.0.1:1",
                                "--config",
                                str(config),
                                "--out-dir",
                                str(root / "runs"),
                                "--first-seconds",
                                "1",
                                "--restart-seconds",
                                "1",
                            ]
                        )
                    self.assertEqual(code, expected)
                    self.assertIn(message, output.getvalue())
                    self.assertIn("process execution only", output.getvalue())


if __name__ == "__main__":
    unittest.main()
