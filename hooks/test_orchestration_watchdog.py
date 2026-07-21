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
TINY_WORKFLOW = {
    "workflow": "tiny",
    "version": 1,
    "initial_node": "review",
    "nodes": {"review": {}},
    "transitions": [{"id": "advance", "from": ["review"], "event": "ADVANCE", "to": "review"}],
    "policies": {"supervision": {"heartbeat_ttl_seconds": {"review": 5}}},
}


class OrchestrationWatchdogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, CODEX_ORCHESTRATION_STATE_DIR=self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def register(self, role: str, session_id: str = "session-1") -> None:
        self.state_command("register", "--session-id", session_id, "--role", role)

    def state_command(self, *arguments: str) -> None:
        subprocess.run(
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
