"""Verify retry timing and stop decisions without cloud calls or real waits."""

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("retry", ROOT / "retry.py")
retry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retry)


class RetryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "output"
        self.clock = 0
        self.env = patch.dict(os.environ, {"PROVISION_MODE": "launch", "GITHUB_OUTPUT": str(self.output)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def invoke(self, results, active=None, attempts=3):
        calls = []
        sleeps = []

        def launch(args):
            calls.append(self.clock)
            code, decision = results[len(calls) - 1]
            self.clock += 10
            if decision:
                with self.output.open("a", encoding="utf-8") as stream:
                    stream.write(decision)
            return subprocess.CompletedProcess(args, code)

        def sleep(seconds):
            sleeps.append(seconds)
            self.clock += seconds

        with patch.object(retry.subprocess, "run", side_effect=launch), \
             patch.object(retry, "workflow_active", side_effect=active or [True] * (attempts + 1)), \
             patch.object(retry, "MAX_ATTEMPTS", attempts), \
             patch.object(retry.time, "monotonic", side_effect=lambda: self.clock), \
             patch.object(retry.time, "sleep", side_effect=sleep), \
             patch("builtins.print"):
            result = retry.run()
        return result, calls, sleeps

    def test_capacity_retries_five_minutes_apart_then_stops_on_acceptance(self):
        code, calls, sleeps = self.invoke([(0, "pause=true\npause=false\n"), (0, "pause=true\n")])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [0, 300])
        self.assertEqual(sleeps, [290])
        self.assertNotIn("continue=true", self.output.read_text())

    def test_uncertain_failure_preserves_pause_and_never_retries(self):
        code, calls, sleeps = self.invoke([(1, "pause=true\n")])
        self.assertEqual(code, 1)
        self.assertEqual(calls, [0])
        self.assertEqual(sleeps, [])
        self.assertEqual(self.output.read_text(), "pause=true\n")

    def test_missing_new_decision_cannot_reuse_previous_capacity_rejection(self):
        code, calls, _ = self.invoke([(0, "pause=false\n"), (0, "")])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [0, 300])
        self.assertNotIn("continue=true", self.output.read_text())

    def test_disabled_workflow_stops_before_another_launch(self):
        code, calls, _ = self.invoke([(0, "pause=false\n")], active=[True, False])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [0])
        self.assertNotIn("continue=true", self.output.read_text())

    def test_rejected_window_waits_before_requesting_continuation(self):
        code, calls, sleeps = self.invoke([(0, "pause=false\n")] * 3)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [0, 300, 600])
        self.assertEqual(sleeps, [290, 290, 290])
        self.assertTrue(self.output.read_text().endswith("continue=true\n"))
        self.assertIn("attempts_remaining=997\n", self.output.read_text())

    def test_total_budget_stops_without_sleep_or_continuation(self):
        os.environ["REMAINING_ATTEMPTS"] = "1"
        code, calls, sleeps = self.invoke([(0, "pause=false\n")])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [0])
        self.assertEqual(sleeps, [])
        self.assertTrue(self.output.read_text().endswith("pause=true\n"))
        self.assertNotIn("continue=true", self.output.read_text())

    def test_budget_survives_handoffs_and_stops_after_exactly_1000_rejections(self):
        remaining = 1000
        total_calls = 0
        while remaining:
            os.environ["REMAINING_ATTEMPTS"] = str(remaining)
            self.output.write_text("")
            code, calls, _ = self.invoke([(0, "pause=false\n")] * 60, attempts=60)
            self.assertEqual(code, 0)
            total_calls += len(calls)
            output = self.output.read_text()
            remaining -= len(calls)
            if remaining:
                self.assertIn(f"attempts_remaining={remaining}\n", output)
                self.assertTrue(output.endswith("continue=true\n"))
            else:
                self.assertNotIn("continue=true", output)
                self.assertTrue(output.endswith("pause=true\n"))
        self.assertEqual(total_calls, 1000)

    def test_short_initial_window_keeps_remaining_budget(self):
        os.environ["RETRY_WINDOW_ATTEMPTS"] = "1"
        code, calls, sleeps = self.invoke([(0, "pause=false\n")])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [0])
        self.assertEqual(sleeps, [290])
        self.assertIn("attempts_remaining=999\n", self.output.read_text())

    def test_invalid_budget_or_window_never_launches(self):
        for name, values in (("REMAINING_ATTEMPTS", ("0", "1001", "2.5", "invalid", "١")),
                             ("RETRY_WINDOW_ATTEMPTS", ("0", "61", "1.5"))):
            for value in values:
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name:value}), \
                     patch.object(retry.subprocess, "run") as launch:
                    with self.assertRaises(RuntimeError):
                        retry.run()
                    launch.assert_not_called()

    def test_disabling_during_final_wait_prevents_continuation(self):
        code, calls, sleeps = self.invoke([(0, "pause=false\n")], active=[True, False], attempts=1)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [0])
        self.assertEqual(sleeps, [290])
        self.assertNotIn("continue=true", self.output.read_text())

    def test_preflight_runs_once_without_workflow_checks(self):
        os.environ["PROVISION_MODE"] = "preflight"
        with patch.object(retry.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as launch, \
             patch.object(retry, "workflow_active") as active:
            self.assertEqual(retry.run(), 0)
        launch.assert_called_once()
        active.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_launch_requires_output_before_calling_oci(self):
        del os.environ["GITHUB_OUTPUT"]
        with patch.object(retry.subprocess, "run") as launch:
            with self.assertRaisesRegex(RuntimeError, "GITHUB_OUTPUT"):
                retry.run()
        launch.assert_not_called()

    def test_github_state_check_fails_closed(self):
        os.environ.update(GITHUB_ACTIONS="true", GITHUB_REPOSITORY="example/test")
        with patch.object(retry.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="synthetic-private-payload")):
            with self.assertRaisesRegex(RuntimeError, "Cannot verify workflow state"):
                retry.workflow_active()

    def test_state_check_uses_the_current_workflow(self):
        os.environ.update(GITHUB_ACTIONS="true", GITHUB_REPOSITORY="example/test",
                          OCI_WORKFLOW_FILE="provision-budget.yml")
        with patch.object(retry.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="active\n")) as request:
            self.assertTrue(retry.workflow_active())
        self.assertIn("repos/example/test/actions/workflows/provision-budget.yml", request.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
