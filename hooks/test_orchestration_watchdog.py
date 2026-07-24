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

    def watchdog(
        self, command: str, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [PYTHON, str(WATCHDOG), command, *arguments],
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
        self.assertIn("session-1 role=review status=healthy-current", result.stdout)
        self.assertIn("advice=none", result.stdout)

    def test_session_past_role_ttl_is_stale(self) -> None:
        self.register("review")
        self.hook()
        result = self.check("--now", str(int(time.time()) + 100000))
        self.assertEqual(result.returncode, 1)
        self.assertIn("status=stale-current", result.stdout)
        self.assertIn("advice=LANE_UNRESPONSIVE", result.stdout)

    def test_new_session_without_hook_is_healthy_during_startup_window(self) -> None:
        self.register("implementation")
        fresh = self.check()
        self.assertEqual(fresh.returncode, 0)
        self.assertIn("status=healthy-current", fresh.stdout)
        self.assertIn("heartbeat=pending", fresh.stdout)
        expired = self.check("--now", str(int(time.time()) + 100000))
        self.assertEqual(expired.returncode, 1)
        self.assertIn("status=stale-current", expired.stdout)
        self.assertIn("advice=LANE_UNRESPONSIVE", expired.stdout)

    def test_plain_continue_does_not_consume_restart_budget(self) -> None:
        self.register("implementation")
        self.state_command("continue", "--source-id", "session-1", "--successor-id", "session-2")
        result = self.check()
        self.assertEqual(result.returncode, 0)
        self.assertIn("session-2 role=implementation status=healthy-current", result.stdout)
        self.assertIn("restarts=0", result.stdout)

    def test_supervised_continue_within_budget_stays_healthy(self) -> None:
        self.register("implementation")
        self.state_command("continue", "--source-id", "session-1", "--successor-id", "session-2", "--supervised")
        result = self.check()
        self.assertEqual(result.returncode, 0)
        self.assertIn("status=healthy-current", result.stdout)
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
        self.assertIn("status=recycle-current", result.stdout)
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
        self.assertEqual(report["status"], "healthy-current")
        self.assertEqual(report["pressure_status"], "measured-current")

    def test_measured_high_context_pressure_requests_proactive_checkpoint(self) -> None:
        self.register("review")
        self.hook()
        self.state_command(
            "pressure", "record", "--session-id", "session-1",
            "--context-used-tokens", "850", "--context-limit-tokens", "1000",
        )

        result = self.check("--json")
        (report,) = json.loads(result.stdout)["sessions"]
        self.assertEqual(report["status"], "recycle-current")
        self.assertEqual(report["advice"], "proactive checkpoint")
        self.assertEqual(report["pressure_status"], "measured-current")

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
        self.assertEqual(report["status"], "recycle-current")
        self.assertEqual(report["pressure_status"], "measured-current")

    def test_expired_pressure_uses_explicit_state_and_event_heuristic(self) -> None:
        self.register("review")
        self.hook()
        state = json.loads(self.session_path().read_text(encoding="utf-8"))
        state["events"] = 400
        state["pressure"] = {
            "version": 1,
            "status": "measured",
            "source": "fixture",
            "collector": "test-v1",
            "context_window": {
                "status": "identified",
                "id": "window-1",
                "unavailable_reason": None,
            },
            "context_ratio": 0.1,
            "state_bytes": 10,
            "observed_at": 1,
            "context_used_tokens": 1,
            "context_limit_tokens": 10,
            "validation": {"status": "valid", "validated_at": 1},
        }
        self.session_path().write_text(json.dumps(state), encoding="utf-8")

        result = self.check("--json", "--now", str(int(time.time())))
        self.assertEqual(result.returncode, 0)
        (report,) = json.loads(result.stdout)["sessions"]
        self.assertEqual(report["status"], "recycle-current")
        self.assertEqual(report["pressure_status"], "measured-expired")
        self.assertIn("event heuristic", report["advice"])

    def test_malformed_current_pressure_is_invalid(self) -> None:
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
        self.assertEqual(result.returncode, 1)
        reports = json.loads(result.stdout)["sessions"]
        self.assertEqual(len(reports), 2)
        self.assertTrue(all(report["status"] == "invalid" for report in reports))
        self.assertTrue(all(report["pressure_status"] == "invalid" for report in reports))

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
        included = self.check("--json", "--include-completed")
        report = {
            item["session_id"]: item
            for item in json.loads(included.stdout)["sessions"]
        }["session-1"]
        self.assertEqual(report["status"], "completed-ignored")
        self.assertEqual(report["pressure_status"], "ignored")

    def test_legacy_active_record_remains_unknown_and_non_actionable(self) -> None:
        sessions = Path(self.temporary.name) / "sessions"
        sessions.mkdir()
        legacy = {
            "session_id": "legacy-1",
            "role": "review",
            "active": True,
            "events": 900,
            "state_revision": 7,
        }
        self.session_path("legacy-1").write_text(json.dumps(legacy), encoding="utf-8")

        result = self.check("--json", "--now", "2000000000")
        self.assertEqual(result.returncode, 0)
        (report,) = json.loads(result.stdout)["sessions"]
        self.assertEqual(report["status"], "legacy-unknown")
        self.assertEqual(report["heartbeat_status"], "legacy-unknown")
        self.assertEqual(report["pressure_status"], "legacy-unknown")
        self.assertEqual(report["advice"], "none")

    def test_legacy_inactive_record_remains_unknown_not_completed(self) -> None:
        sessions = Path(self.temporary.name) / "sessions"
        sessions.mkdir()
        legacy = {
            "session_id": "legacy-1",
            "role": "review",
            "active": False,
            "state_revision": 7,
        }
        self.session_path("legacy-1").write_text(json.dumps(legacy), encoding="utf-8")

        result = self.check("--json", "--include-completed", "--now", "2000000000")
        self.assertEqual(result.returncode, 0)
        (report,) = json.loads(result.stdout)["sessions"]
        self.assertEqual(report["status"], "legacy-unknown")
        self.assertEqual(report["heartbeat_status"], "legacy-unknown")
        self.assertEqual(report["pressure_status"], "legacy-unknown")

    def test_unsupported_observation_capability_is_explicit(self) -> None:
        self.register("review")
        state = json.loads(self.session_path().read_text(encoding="utf-8"))
        state["lifecycle"]["observation_capabilities"]["heartbeat"] = "unsupported"
        state["lifecycle"]["observation_capabilities"]["pressure"] = "unsupported"
        state["heartbeat"] = {
            "version": 1,
            "status": "unsupported",
            "observed_at": None,
            "event": None,
            "source": None,
            "collector": "none",
        }
        self.session_path().write_text(json.dumps(state), encoding="utf-8")

        result = self.check("--json")
        self.assertEqual(result.returncode, 0)
        (report,) = json.loads(result.stdout)["sessions"]
        self.assertEqual(report["status"], "legacy-unknown")
        self.assertEqual(report["heartbeat_status"], "unsupported")
        self.assertEqual(report["pressure_status"], "unsupported")

    def test_invalid_lifecycle_version_and_capabilities_fail_closed(self) -> None:
        for session_id, mutation in (
            ("bad-version", lambda state: state["lifecycle"].update(version=999)),
            (
                "bad-capability",
                lambda state: state["lifecycle"]["observation_capabilities"].update(
                    heartbeat="unknown"
                ),
            ),
        ):
            self.register("review", session_id)
            state = json.loads(
                self.session_path(session_id).read_text(encoding="utf-8")
            )
            mutation(state)
            self.session_path(session_id).write_text(
                json.dumps(state), encoding="utf-8"
            )

        result = self.check("--json")
        self.assertEqual(result.returncode, 1)
        reports = json.loads(result.stdout)["sessions"]
        self.assertTrue(all(report["status"] == "invalid" for report in reports))

    def test_malformed_current_terminal_is_invalid_without_hiding_other_records(self) -> None:
        self.register("review", "good-terminal")
        self.state_command("complete", "--session-id", "good-terminal")
        self.register("review", "bad-terminal")
        self.state_command("complete", "--session-id", "bad-terminal")
        bad = json.loads(
            self.session_path("bad-terminal").read_text(encoding="utf-8")
        )
        bad["heartbeat"] = "corrupt"
        self.session_path("bad-terminal").write_text(json.dumps(bad), encoding="utf-8")

        result = self.check("--json", "--include-completed")
        self.assertEqual(result.returncode, 1)
        reports = {
            report["session_id"]: report
            for report in json.loads(result.stdout)["sessions"]
        }
        self.assertEqual(reports["good-terminal"]["status"], "completed-ignored")
        self.assertEqual(reports["bad-terminal"]["status"], "invalid")

    def test_audit_is_deterministic_payload_safe_and_reports_runtime_evidence(self) -> None:
        self.register("review")
        state = json.loads(self.session_path().read_text(encoding="utf-8"))
        state["private_payload"] = "must-not-appear"
        self.session_path().write_text(json.dumps(state), encoding="utf-8")

        first = self.watchdog("audit", "--json", "--now", "2000000000")
        second = self.watchdog("audit", "--json", "--now", "2000000000")
        self.assertEqual(first.returncode, 0)
        self.assertEqual(first.stdout, second.stdout)
        self.assertNotIn("must-not-appear", first.stdout)
        payload = json.loads(first.stdout)
        self.assertEqual(payload["runtime"]["status"], "compatible")
        self.assertEqual(payload["runtime"]["record_schema_version"], 1)
        records = {item["session_id"]: item for item in payload["records"]}
        record = records["session-1"]
        self.assertEqual(record["record_schema_version"], 1)
        self.assertEqual(record["lifecycle_version"], 1)
        self.assertIsInstance(record["state_revision"], int)
        self.assertIn("continued_from", record["continuation"])
        self.assertIn("end_reason", record["terminal"])
        self.assertTrue(
            all(item["status"] == "match" for item in payload["runtime"]["files"])
        )

    def test_reconcile_requires_dry_run_and_preserves_exact_bytes(self) -> None:
        sessions = Path(self.temporary.name) / "sessions"
        sessions.mkdir()
        legacy = {
            "session_id": "legacy-1",
            "role": "review",
            "active": True,
            "state_revision": 3,
        }
        self.session_path("legacy-1").write_text(
            json.dumps(legacy, sort_keys=True), encoding="utf-8"
        )
        before = self.session_path("legacy-1").read_bytes()

        denied = self.watchdog("reconcile", "--json")
        self.assertEqual(denied.returncode, 2)
        self.assertEqual(self.session_path("legacy-1").read_bytes(), before)
        planned = self.watchdog(
            "reconcile", "--dry-run", "--json", "--now", "2000000000"
        )
        self.assertEqual(planned.returncode, 0)
        payload = json.loads(planned.stdout)
        self.assertFalse(payload["writes_performed"])
        self.assertEqual(payload["actions"][0]["action"], "preserve-legacy")
        self.assertEqual(self.session_path("legacy-1").read_bytes(), before)

    def test_reconcile_dry_run_does_not_create_registry_files(self) -> None:
        result = self.watchdog(
            "reconcile", "--dry-run", "--json", "--now", "2000000000"
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_audit_reports_interrupted_journal_without_mutating_then_check_recovers(self) -> None:
        self.register("implementation")
        crash_env = dict(self.env, CODEX_ORCHESTRATION_TEST_CRASH_AFTER="successor")
        crashed = subprocess.run(
            [
                PYTHON,
                str(STATE),
                "continue",
                "--source-id",
                "session-1",
                "--successor-id",
                "session-2",
            ],
            env=crash_env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(crashed.returncode, 91)
        source_before = self.session_path("session-1").read_bytes()
        successor_before = self.session_path("session-2").read_bytes()

        audited = self.watchdog("audit", "--json")
        self.assertEqual(audited.returncode, 0)
        self.assertTrue(json.loads(audited.stdout)["continuation_recovery_pending"])
        self.assertEqual(self.session_path("session-1").read_bytes(), source_before)
        self.assertEqual(self.session_path("session-2").read_bytes(), successor_before)

        recovered = self.check("--json")
        self.assertEqual(recovered.returncode, 0)
        self.assertFalse(
            json.loads(self.session_path("session-1").read_text(encoding="utf-8"))[
                "active"
            ]
        )
        self.assertTrue(
            json.loads(self.session_path("session-2").read_text(encoding="utf-8"))[
                "active"
            ]
        )

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
        self.assertEqual(session["status"], "stale-current")
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
        self.assertIn("status=healthy-current", default.stdout)
        overridden = self.check("--workflow", str(workflow_path), "--now", now)
        self.assertEqual(overridden.returncode, 1)
        self.assertIn("status=stale-current", overridden.stdout)


if __name__ == "__main__":
    unittest.main()
