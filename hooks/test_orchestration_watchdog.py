#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
HOOK = Path(__file__).with_name("orchestration_hook.py")
STATE = Path(__file__).with_name("orchestration_state.py")
WATCHDOG = Path(__file__).with_name("orchestration_watchdog.py")
PYTHON = "python3"
TINY_WORKFLOW = json.loads(
    (Path(__file__).parents[1] / "gepetto" / "references" / "workflow.json").read_text(
        encoding="utf-8"
    )
)
TINY_WORKFLOW["workflow"] = "tiny"
TINY_WORKFLOW["policies"]["supervision"]["heartbeat_ttl_seconds"]["review"] = 5


class OrchestrationWatchdogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, CODEX_ORCHESTRATION_STATE_DIR=self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def register(self, role: str, session_id: str = "session-1") -> None:
        if role == "gepetto":
            self.state_command("register", "--session-id", session_id, "--role", role)
            return
        coordinator = f"{session_id}-coordinator"
        self.state_command("register", "--session-id", coordinator, "--role", "gepetto")
        self.state_command(
            "register", "--session-id", session_id, "--role", role,
            "--coordinator-thread-id", coordinator,
        )
        self.state_command("complete", "--session-id", coordinator)

    def state_command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [PYTHON, str(STATE), *arguments],
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )

    def hook(self, session_id: str = "session-1") -> None:
        subprocess.run(
            [PYTHON, str(HOOK)],
            input=json.dumps({"session_id": session_id, "hook_event_name": "SessionStart", "source": "startup"}),
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )

    def check(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [PYTHON, str(WATCHDOG), "check", *arguments],
            env=self.env,
            text=True,
            capture_output=True,
        )

    def session_path(self, session_id: str = "session-1") -> Path:
        return Path(self.temporary.name) / "sessions" / f"{session_id}.json"

    def test_fresh_session_with_heartbeat_is_healthy(self) -> None:
        self.register("review")
        self.hook()
        result = self.check()
        self.assertEqual(result.returncode, 0)
        self.assertIn("session-1 role=review status=healthy", result.stdout)
        self.assertIn("advice=none", result.stdout)

    def test_session_past_role_ttl_is_stale(self) -> None:
        self.register("review")
        self.hook()
        result = self.check("--now", str(int(time.time()) + 100000))
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=stale", result.stdout)
        self.assertIn("advice=LANE_UNRESPONSIVE", result.stdout)

    def test_session_without_heartbeat_is_stale_with_unknown_age(self) -> None:
        self.register("implementation")
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=stale", result.stdout)
        self.assertIn("age=unknown", result.stdout)
        self.assertIn("advice=LANE_UNRESPONSIVE", result.stdout)

    def test_plain_continue_does_not_consume_restart_budget(self) -> None:
        self.register("implementation")
        self.state_command("continue", "--source-id", "session-1", "--successor-id", "session-2")
        result = self.check()
        self.assertEqual(result.returncode, 0)
        self.assertIn("session-2 role=implementation status=healthy", result.stdout)
        self.assertIn("restarts=0", result.stdout)

    def test_supervised_continue_within_budget_stays_healthy(self) -> None:
        self.register("implementation")
        self.state_command("continue", "--source-id", "session-1", "--successor-id", "session-2", "--supervised")
        result = self.check()
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=healthy", result.stdout)
        self.assertIn("restarts=1", result.stdout)

    def test_restarts_beyond_budget_report_over_budget(self) -> None:
        self.register("implementation")
        for source, successor in (("session-1", "session-2"), ("session-2", "session-3"), ("session-3", "session-4")):
            self.state_command("continue", "--source-id", source, "--successor-id", successor, "--supervised")
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("session-4 role=implementation status=over-budget", result.stdout)
        self.assertIn("restarts=3", result.stdout)
        self.assertIn("advice=RESTART_BUDGET_EXCEEDED", result.stdout)

    def test_recycle_threshold_reports_but_exit_stays_zero(self) -> None:
        self.register("review")
        self.hook()
        state = json.loads(self.session_path().read_text(encoding="utf-8"))
        state["events"] = 400
        self.session_path().write_text(json.dumps(state), encoding="utf-8")
        result = self.check()
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=recycle", result.stdout)
        self.assertIn("events=400", result.stdout)
        self.assertIn("advice=proactive checkpoint", result.stdout)

    def test_measured_low_pressure_takes_precedence_over_legacy_event_count(self) -> None:
        self.register("review")
        self.hook()
        state = json.loads(self.session_path().read_text(encoding="utf-8"))
        state["events"] = 400
        self.session_path().write_text(json.dumps(state), encoding="utf-8")
        self.state_command(
            "pressure", "record", "--session-id", "session-1",
            "--context-used-tokens", "100", "--context-limit-tokens", "1000",
        )

        result = self.check("--json")
        (report,) = json.loads(result.stdout)["sessions"]
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["pressure_source"], "measured")

    def test_measured_high_context_pressure_requests_proactive_checkpoint(self) -> None:
        self.register("review")
        self.hook()
        self.state_command(
            "pressure", "record", "--session-id", "session-1",
            "--context-used-tokens", "850", "--context-limit-tokens", "1000",
        )

        result = self.check("--json")
        (report,) = json.loads(result.stdout)["sessions"]
        self.assertEqual(report["status"], "recycle")
        self.assertEqual(report["advice"], "proactive checkpoint")
        self.assertEqual(report["pressure_source"], "measured")

    def test_measured_state_size_can_request_proactive_checkpoint(self) -> None:
        self.register("review")
        self.hook()
        self.state_command(
            "pressure", "record", "--session-id", "session-1",
            "--context-used-tokens", "100", "--context-limit-tokens", "1000",
        )
        workflow = json.loads(json.dumps(TINY_WORKFLOW))
        workflow["policies"]["supervision"].update({
            "recycle_context_ratio": 0.99,
            "recycle_state_bytes": 1,
        })
        workflow_path = Path(self.temporary.name) / "state-pressure-workflow.json"
        workflow_path.write_text(json.dumps(workflow), encoding="utf-8")

        result = self.check("--json", "--workflow", str(workflow_path))
        (report,) = json.loads(result.stdout)["sessions"]
        self.assertEqual(report["status"], "recycle")
        self.assertEqual(report["pressure_source"], "measured")

    def test_stale_or_malformed_pressure_falls_back_to_events_without_crashing_report(self) -> None:
        for session_id in ("stale", "malformed"):
            self.register("review", session_id=session_id)
            self.hook(session_id)
            state = json.loads(self.session_path(session_id).read_text(encoding="utf-8"))
            state["events"] = 400
            state["pressure"] = (
                {"context_ratio": 0.1, "state_bytes": 10, "observed_at": 1,
                 "context_used_tokens": 1, "context_limit_tokens": 10}
                if session_id == "stale" else {"context_ratio": "bad"}
            )
            self.session_path(session_id).write_text(json.dumps(state), encoding="utf-8")

        now = int(time.time())
        result = self.check("--json", "--now", str(now))
        self.assertEqual(result.returncode, 0)
        reports = json.loads(result.stdout)["sessions"]
        self.assertEqual(len(reports), 2)
        self.assertTrue(all(report["status"] == "recycle" for report in reports))
        self.assertTrue(all(report["pressure_source"] == "legacy-events" for report in reports))

    def test_invalid_session_reports_nonzero_without_hiding_other_sessions(self) -> None:
        self.register("review", "good")
        self.hook("good")
        self.register("review", "bad-counter")
        self.hook("bad-counter")
        bad = json.loads(self.session_path("bad-counter").read_text(encoding="utf-8"))
        bad["events"] = []
        self.session_path("bad-counter").write_text(json.dumps(bad), encoding="utf-8")
        malformed = self.session_path("malformed")
        malformed.write_text("{not-json", encoding="utf-8")

        result = self.check("--json")
        self.assertEqual(result.returncode, 1)
        reports = {item["session_id"]: item for item in json.loads(result.stdout)["sessions"]}
        self.assertIn("good", reports)
        self.assertEqual(reports["bad-counter"]["status"], "invalid")
        self.assertEqual(reports["malformed"]["status"], "invalid")

    def test_pressure_state_bytes_matches_persisted_state_after_sample(self) -> None:
        self.register("review")
        self.hook()
        recorded = json.loads(self.state_command(
            "pressure", "record", "--session-id", "session-1",
            "--context-used-tokens", "100", "--context-limit-tokens", "1000",
        ).stdout)
        self.assertEqual(recorded["state_bytes"], self.session_path().stat().st_size)

    def test_completed_sessions_are_skipped(self) -> None:
        self.register("review")
        self.hook()
        self.state_command("complete", "--session-id", "session-1")
        result = self.check()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_json_output_round_trips(self) -> None:
        self.register("review")
        self.hook()
        now = int(time.time()) + 100000
        result = self.check("--json", "--now", str(now))
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["checked_at"], now)
        (session,) = payload["sessions"]
        self.assertEqual(session["session_id"], "session-1")
        self.assertEqual(session["role"], "review")
        self.assertEqual(session["status"], "stale")
        self.assertEqual(session["advice"], "LANE_UNRESPONSIVE")
        self.assertEqual(session["restarts"], 0)
        self.assertEqual(session["events"], 1)
        self.assertIsInstance(session["age"], int)

    def test_custom_workflow_overrides_supervision_policy(self) -> None:
        self.register("review")
        self.hook()
        workflow_path = Path(self.temporary.name) / "workflow.json"
        workflow_path.write_text(json.dumps(TINY_WORKFLOW), encoding="utf-8")
        now = str(int(time.time()) + 60)
        default = self.check("--now", now)
        self.assertEqual(default.returncode, 0)
        self.assertIn("status=healthy", default.stdout)
        overridden = self.check("--workflow", str(workflow_path), "--now", now)
        self.assertEqual(overridden.returncode, 1)
        self.assertIn("status=stale", overridden.stdout)


if __name__ == "__main__":
    unittest.main()
