#!/usr/bin/env python3

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hooks import orchestration_state
from hooks.test_orchestration_contract import artifact_text, valid_specification
from hooks.test_orchestration_packets import valid_packets


HOOK = Path(__file__).with_name("orchestration_hook.py")
STATE = Path(__file__).with_name("orchestration_state.py")
CONFIG = Path(__file__).with_name("hooks.json")
PYTHON = "python3"
HOOK_COMMAND = '/usr/bin/env python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_hook.py"'
SHA = "a" * 40


def packet_message(packet_type: str) -> str:
    return f"{packet_type}:\n{json.dumps(valid_packets()[packet_type])}"


def merge_ready_packet() -> dict[str, object]:
    packet = valid_packets()["JIMINY_READY"]
    for item in packet["pull_requests"]:
        item["gates"]["approvals_satisfied"] = True
    return packet


class OrchestrationHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, CODEX_ORCHESTRATION_STATE_DIR=self.temporary.name)
        self.research_artifact = Path(self.temporary.name) / "research.md"
        self.research_artifact.write_text(artifact_text(), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_hook_config_uses_portable_codex_home(self) -> None:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        commands = [
            hook["command"]
            for entries in config["hooks"].values()
            for entry in entries
            for hook in entry["hooks"]
            if "orchestration_hook.py" in hook["command"]
        ]
        self.assertTrue(commands)
        self.assertTrue(all(command == HOOK_COMMAND for command in commands))

    def test_blocking_hook_failure_exits_with_blocking_status(self) -> None:
        self.register("implementation")
        state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        state_path.write_text("{invalid-json", encoding="utf-8")
        result = subprocess.run(
            [PYTHON, str(HOOK)],
            input=json.dumps(
                {
                    "session_id": "session-1",
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": "git push origin branch"},
                }
            ),
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("codex-orchestration-hook", result.stderr)

    def register(self, role: str, *extra: str, session_id: str = "session-1") -> None:
        if role != "gepetto" and "--coordinator-thread-id" not in extra:
            coordinator = f"{session_id}-coordinator"
            subprocess.run(
                [PYTHON, str(STATE), "register", "--session-id", coordinator, "--role", "gepetto"],
                env=self.env, text=True, capture_output=True, check=True,
            )
            extra = (*extra, "--coordinator-thread-id", coordinator)
        subprocess.run(
            [PYTHON, str(STATE), "register", "--session-id", session_id, "--role", role, *extra],
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )

    def hook(self, event: str, **fields: object) -> dict[str, object] | None:
        session_id = str(fields.pop("session_id", "session-1"))
        return self.hook_for(session_id, event, **fields)

    def hook_for(
        self, session_id: str, event: str, **fields: object
    ) -> dict[str, object] | None:
        result = subprocess.run(
            [PYTHON, str(HOOK)],
            input=json.dumps({"session_id": session_id, "hook_event_name": event, **fields}),
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )
        return json.loads(result.stdout) if result.stdout else None

    def test_compaction_is_inert_without_registered_state(self) -> None:
        self.assertIsNone(self.hook("SessionStart", source="compact"))

    def test_unregistered_stop_hooks_return_valid_empty_json(self) -> None:
        self.assertEqual(self.hook("Stop", last_assistant_message="done", stop_hook_active=False), {})
        self.assertEqual(self.hook("SubagentStop", agent_id="unknown", last_assistant_message="done", stop_hook_active=False), {})

    def test_registered_compaction_requests_role_aware_handoff(self) -> None:
        self.register("gepetto")
        result = self.hook("SessionStart", source="compact")
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("$checkpoint", context)
        self.assertIn("gepetto", context)
        self.assertIn("unarchived", context)

    def test_registered_non_compact_session_start_is_ignored(self) -> None:
        self.register("gepetto")
        self.assertIsNone(self.hook("SessionStart", source="startup"))

    def test_complete_disables_compaction_handoff(self) -> None:
        self.register("jiminy")
        subprocess.run(
            [PYTHON, str(STATE), "complete", "--session-id", "session-1"],
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIsNone(self.hook("SessionStart", source="compact"))

    def test_research_subagent_does_not_duplicate_lane_receipt_enforcement(self) -> None:
        self.register("research")
        start = self.hook("SubagentStart", agent_id="agent-1", agent_type="default")
        context = start["hookSpecificOutput"]["additionalContext"]
        self.assertIn("code-read-only", context)
        self.assertIn("issue create/update remains allowed", context)
        self.assertIn("split, consolidate", context)
        stop = self.hook("SubagentStop", agent_id="agent-1", agent_type="default", last_assistant_message="done", stop_hook_active=False)
        self.assertEqual(stop, {})

    def test_review_nested_agent_is_a_fixer(self) -> None:
        self.register("review")
        self.hook("SubagentStart", agent_id="reviewer", agent_type="default")
        fixer = self.hook("SubagentStart", agent_id="fixer", agent_type="default")
        self.assertIn("assigned fixes", fixer["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.hook("SubagentStop", agent_id="fixer", last_assistant_message="fixed", stop_hook_active=False), {})

    def test_lane_stop_requires_complete_valid_packet(self) -> None:
        for role, packet_type in (
            ("research", "RESEARCH_PACKET"),
            ("implementation", "IMPLEMENTATION_PACKET"),
            ("review", "REVIEW_PACKET"),
            ("jiminy", "JIMINY_COMPLETE"),
        ):
            with self.subTest(role=role):
                session_id = f"session-{role}"
                self.register(role, session_id=session_id)
                blocked = self.hook_for(
                    session_id, "Stop", last_assistant_message=f"{packet_type}:\n{{}}",
                    stop_hook_active=False,
                )
                self.assertEqual(blocked["decision"], "block")
                allowed = self.hook_for(
                    session_id, "Stop", last_assistant_message=packet_message(packet_type),
                    stop_hook_active=False,
                )
                self.assertEqual(allowed, {})
                self.assertFalse(self.session_state(session_id)["active"])

    def test_valid_lane_stop_persists_canonical_terminal_receipt(self) -> None:
        self.register("implementation")
        packet = valid_packets()["IMPLEMENTATION_PACKET"]

        allowed = self.hook(
            "Stop",
            last_assistant_message=f"IMPLEMENTATION_PACKET:\n{json.dumps(packet)}",
            stop_hook_active=False,
        )

        self.assertEqual(allowed, {})
        state = self.session_state()
        self.assertFalse(state["active"])
        self.assertEqual(state["terminal_packet_type"], "IMPLEMENTATION_PACKET")
        canonical = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(
            state["terminal_packet_digest"],
            "sha256:" + hashlib.sha256(canonical).hexdigest(),
        )

    def test_lane_stop_rejects_duplicate_packet_headers(self) -> None:
        self.register("implementation")
        duplicate = self.hook(
            "Stop",
            last_assistant_message=(
                f"{packet_message('IMPLEMENTATION_PACKET')}\n"
                f"{packet_message('IMPLEMENTATION_PACKET')}"
            ),
            stop_hook_active=False,
        )
        self.assertEqual(duplicate["decision"], "block")
        recursive = self.hook(
            "Stop",
            last_assistant_message="IMPLEMENTATION_PACKET:\nIMPLEMENTATION_PACKET:",
            stop_hook_active=True,
        )
        self.assertEqual(recursive, {})
        state = self.session_state()
        self.assertTrue(state["active"])
        self.assertTrue(state["forced_stop_without_receipt"])

    def test_jiminy_stop_requires_exactly_one_terminal_packet(self) -> None:
        self.register("jiminy")
        blocked = self.hook("Stop", last_assistant_message="done", stop_hook_active=False)
        self.assertEqual(blocked["decision"], "block")
        malformed = self.hook(
            "Stop", last_assistant_message="JIMINY_COMPLETE:\n  blockers: []", stop_hook_active=False,
        )
        self.assertEqual(malformed["decision"], "block")
        self.assertTrue(self.session_state()["active"])
        allowed = self.hook(
            "Stop", last_assistant_message=packet_message("JIMINY_COMPLETE"),
            stop_hook_active=False,
        )
        self.assertEqual(allowed, {})
        self.assertFalse(self.session_state()["active"])

    def test_jiminy_subagent_start_does_not_inject_lane_contract(self) -> None:
        self.register("jiminy")
        self.assertIsNone(self.hook("SubagentStart", agent_id="helper", agent_type="default"))

    def test_force_push_is_denied(self) -> None:
        self.register("implementation")
        result = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "git push --force-with-lease origin branch"})
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_force_push_in_combined_short_option_cluster_is_denied(self) -> None:
        self.register("implementation")
        for command in ("git push -uf origin branch", "git push -fv origin branch"):
            with self.subTest(command=command):
                result = self.hook(
                    "PreToolUse", tool_name="Bash", tool_input={"command": command}
                )
                self.assertEqual(
                    result["hookSpecificOutput"]["permissionDecision"], "deny"
                )

    def test_branch_name_containing_dash_f_is_not_treated_as_force_push(self) -> None:
        self.register("implementation")
        result = self.hook(
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "git push origin my-feature"},
        )
        self.assertIsNone(result)

    def test_research_cannot_apply_patch(self) -> None:
        self.register("research")
        result = self.hook("PreToolUse", tool_name="apply_patch", tool_input={"command": "*** Begin Patch"})
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_research_can_create_and_update_issues(self) -> None:
        self.register("research")
        create = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "gh issue create --title leaf --body contract"})
        update = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "gh issue edit 42 --body-file contract.md"})
        self.assertIsNone(create)
        self.assertIsNone(update)

    def test_child_cannot_mutate_coordinator_authority_through_state_cli(self) -> None:
        self.register("implementation")
        denied_commands = (
            "python3 hooks/orchestration_state.py register --session-id fake --role gepetto",
            "python3 hooks/orchestration_state.py register --session-id fake --role jiminy --merge-authorized",
            "python3 hooks/orchestration_state.py complete --session-id session-1-coordinator",
            "python3 hooks/orchestration_state.py continue --source-id session-1-coordinator --successor-id fake",
            "python3 hooks/orchestration_state.py ledger set --session-id session-1-coordinator --lane x --json {}",
            "python3 hooks/orchestration_state.py graph apply --session-id session-1-coordinator --lane x --current-node review --event ACTIONABLE_FINDINGS",
        )
        for command in denied_commands:
            result = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": command})
            self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny", command)
        own_continue = self.hook(
            "PreToolUse", tool_name="Bash",
            tool_input={"command": "python3 hooks/orchestration_state.py continue --source-id session-1 --successor-id next"},
        )
        self.assertIsNone(own_continue)

    def test_child_authority_guard_covers_python_module_invocations(self) -> None:
        self.register("implementation")
        denied = (
            "python3 -m hooks.orchestration_state register --session-id fake --role gepetto",
            "/usr/bin/python3 -m hooks.orchestration_state ledger show --session-id session-1-coordinator",
            "python3.14 -m orchestration_state graph apply --session-id session-1-coordinator --lane x --current-node review --event ACTIONABLE_FINDINGS",
        )
        for command in denied:
            result = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": command})
            self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny", command)
        own = self.hook(
            "PreToolUse", tool_name="Bash",
            tool_input={
                "command": "python3 -m hooks.orchestration_state continue --source-id session-1 --successor-id next"
            },
        )
        self.assertIsNone(own)

    def test_child_authority_guard_tokenizes_quoted_and_chained_invocations(self) -> None:
        self.register("implementation")
        denied = (
            "python3 -m 'hooks.orchestration_state' register --session-id fake --role gepetto",
            "python3 'hooks/orchestration_state.py' register --session-id fake --role gepetto",
            "echo safe&&/usr/bin/python3.14 -m 'hooks.orchestration_state' ledger show --session-id session-1-coordinator",
            "python3 -m 'hooks.orchestration_state register --session-id fake --role gepetto",
        )
        for command in denied:
            result = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": command})
            self.assertEqual(
                result["hookSpecificOutput"]["permissionDecision"], "deny", command
            )
        own = self.hook(
            "PreToolUse", tool_name="Bash",
            tool_input={
                "command": "echo safe && python3 -m 'hooks.orchestration_state' continue --source-id session-1 --successor-id next"
            },
        )
        self.assertIsNone(own)

    def test_checkpoint_moves_active_role_state(self) -> None:
        self.register("review")
        subprocess.run(
            [PYTHON, str(STATE), "continue", "--source-id", "session-1", "--successor-id", "session-2"],
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIsNone(self.hook("SessionStart", source="compact"))
        result = subprocess.run(
            [PYTHON, str(HOOK)],
            input=json.dumps({"session_id": "session-2", "hook_event_name": "SessionStart", "source": "compact"}),
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("review", result.stdout)

    def state_command(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [PYTHON, str(STATE), *arguments],
            env=self.env,
            text=True,
            capture_output=True,
            check=check,
        )

    def accept_command(
        self,
        event: str,
        packet: dict[str, object],
        *,
        lane: str,
        actor: str,
        expected_revision: int | None = None,
        observed_head: str | None = None,
        runner: str | None = None,
        review_attempt: str | None = None,
        research_artifact: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        revision = expected_revision
        if revision is None:
            revision = int(self.session_state()["state_revision"])
        supplied_packet = copy.deepcopy(packet)
        if event == "RESEARCH_PACKET":
            research_artifact = research_artifact or self.research_artifact
            supplied_packet["artifact"]["content_ref"] = (
                orchestration_state.context_digest([research_artifact])["ref"]
            )
        arguments = [
            "graph", "accept", "--session-id", "session-1", "--lane", lane,
            "--actor-session-id", actor, "--expected-revision", str(revision),
            "--event", event, "--packet-json", json.dumps(supplied_packet),
        ]
        if observed_head is not None:
            arguments.extend(("--observed-pr-head-sha", observed_head))
        if runner is not None:
            arguments.extend(("--runner-session-id", runner))
        if review_attempt is not None:
            arguments.extend(("--review-attempt-id", review_attempt))
        if research_artifact is not None:
            arguments.extend(("--research-artifact-file", str(research_artifact)))
        return self.state_command(*arguments, check=check)

    def bind_review_attempt(
        self, *, lane: str, actor: str, expected_revision: int | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        revision = expected_revision
        if revision is None:
            revision = int(self.session_state()["state_revision"])
        return self.state_command(
            "graph", "review-attempt", "--session-id", "session-1",
            "--lane", lane, "--actor-session-id", actor,
            "--expected-revision", str(revision), check=check,
        )

    def test_read_only_roles_cannot_edit_or_write(self) -> None:
        for index, role in enumerate(("gepetto", "jiminy", "research"), start=1):
            session_id = f"read-only-{index}"
            self.register(role, session_id=session_id)
            for tool in ("Edit", "Write"):
                result = self.hook("PreToolUse", session_id=session_id, tool_name=tool, tool_input={"file_path": "/tmp/x", "content": "y"})
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny", (role, tool))

    def test_implementation_can_edit_and_write(self) -> None:
        self.register("implementation")
        for tool in ("Edit", "Write"):
            self.assertIsNone(self.hook("PreToolUse", tool_name=tool, tool_input={"file_path": "/tmp/x", "content": "y"}))

    def test_api_merge_is_denied_even_for_authorized_jiminy(self) -> None:
        self.register("jiminy", "--merge-authorized")
        rest = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "gh api repos/o/r/pulls/1/merge -X PUT"})
        self.assertEqual(rest["hookSpecificOutput"]["permissionDecision"], "deny")
        graphql = self.hook(
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "gh api graphql -f query='mutation { mergePullRequest(input: {pullRequestId: \"x\"}) { clientMutationId } }'"},
        )
        self.assertEqual(graphql["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_non_merge_gh_api_is_allowed(self) -> None:
        self.register("jiminy", "--merge-authorized")
        self.assertIsNone(self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "gh api repos/o/r/pulls/1"}))

    def test_ledger_set_deep_merges_and_show_round_trips(self) -> None:
        self.register("gepetto")
        self.state_command("ledger", "set", "--session-id", "session-1", "--lane", "lane-1", "--json", '{"role": "review", "gates": {"ci": "pending"}}')
        self.state_command("ledger", "set", "--session-id", "session-1", "--lane", "lane-1", "--json", '{"gates": {"ci": "green"}, "node": "review"}')
        shown = json.loads(self.state_command("ledger", "show", "--session-id", "session-1").stdout)
        self.assertEqual(shown, {"lane-1": {"role": "review", "gates": {"ci": "green"}, "node": "review"}})

    def test_ledger_show_without_ledger_prints_empty_object(self) -> None:
        self.register("gepetto")
        self.assertEqual(json.loads(self.state_command("ledger", "show", "--session-id", "session-1").stdout), {})

    def test_ledger_set_requires_registered_session(self) -> None:
        result = self.state_command("ledger", "set", "--session-id", "missing", "--lane", "lane-1", "--json", "{}", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing", result.stderr)

    def test_ledger_survives_checkpoint_continuation(self) -> None:
        self.register("gepetto")
        self.state_command("ledger", "set", "--session-id", "session-1", "--lane", "lane-1", "--json", '{"node": "review"}')
        self.state_command("continue", "--source-id", "session-1", "--successor-id", "session-2")
        shown = json.loads(self.state_command("ledger", "show", "--session-id", "session-2").stdout)
        self.assertEqual(shown, {"lane-1": {"node": "review"}})

    def test_parallel_ledger_updates_do_not_lose_state(self) -> None:
        self.register("gepetto")
        processes = [
            subprocess.Popen(
                [
                    PYTHON, str(STATE), "ledger", "set", "--session-id", "session-1",
                    "--lane", "lane-1", "--json", json.dumps({"proof": {f"item-{index}": index}}),
                ],
                env=self.env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for index in range(20)
        ]
        results = [process.communicate(timeout=10) + (process.returncode,) for process in processes]
        self.assertTrue(all(returncode == 0 for _, _, returncode in results), results)
        shown = json.loads(self.state_command("ledger", "show", "--session-id", "session-1").stdout)
        self.assertEqual(shown["lane-1"]["proof"], {f"item-{index}": index for index in range(20)})

    def test_ledger_move_transfers_lane_and_leaves_tombstone(self) -> None:
        self.register("gepetto")
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "lane-old",
            "--json", '{"role":"review","node":"review","cycles":2}',
        )
        self.state_command(
            "ledger", "move", "--session-id", "session-1",
            "--from-lane", "lane-old", "--to-lane", "lane-new",
        )
        shown = json.loads(self.state_command("ledger", "show", "--session-id", "session-1").stdout)
        self.assertEqual(shown["lane-old"], {"tombstone": True, "successor_lane": "lane-new"})
        self.assertEqual(shown["lane-new"]["continued_from"], "lane-old")
        self.assertEqual(shown["lane-new"]["cycles"], 2)

    def test_graph_apply_atomically_enforces_and_increments_review_cycle(self) -> None:
        self.register("gepetto")
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "review-lane",
            "--json", '{"node":"review","review_fix_cycles":2}',
        )
        applied = json.loads(self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "review-lane",
            "--current-node", "review", "--event", "ACTIONABLE_FINDINGS",
        ).stdout)
        self.assertEqual(applied["transition_id"], "review-found-issues")
        self.assertEqual(applied["state"]["node"], "fixer")
        self.assertEqual(applied["state"]["review_fix_cycles"], 3)
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "review-lane",
            "--json", '{"node":"review"}',
        )
        blocked = self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "review-lane",
            "--current-node", "review", "--event", "ACTIONABLE_FINDINGS", check=False,
        )
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("found 0", blocked.stderr)

    def test_graph_accept_persists_typed_event_and_receipt_in_one_revision(self) -> None:
        self.register("gepetto")
        self.register(
            "implementation", "--coordinator-thread-id", "session-1", session_id="impl-1"
        )
        trusted = {
            "node": "implementation", "review_fix_cycles": 2,
            "head_sha": SHA,
            "owner": "impl-1", "coordinator_thread_id": "session-1",
        }
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "impl-1",
            "--json", json.dumps(trusted),
        )
        before_revision = int(self.session_state()["state_revision"])

        result = json.loads(self.accept_command(
            "IMPLEMENTATION_PACKET", valid_packets()["IMPLEMENTATION_PACKET"],
            lane="impl-1", actor="impl-1", observed_head=SHA,
        ).stdout)

        after = self.session_state()
        self.assertEqual(after["state_revision"], before_revision + 1)
        lane = after["ledger"]["impl-1"]
        self.assertEqual(lane["node"], "review")
        for key, value in trusted.items():
            if key != "node":
                self.assertEqual(lane[key], value)
        receipt = lane["acceptance_receipts"][0]
        self.assertEqual(receipt, result["acceptance_receipt"])
        self.assertEqual(receipt["event"], "IMPLEMENTATION_PACKET")
        self.assertEqual(receipt["transition_id"], "implementation-proved")
        self.assertEqual(receipt["resulting_node"], "review")
        self.assertEqual(receipt["actor_session_id"], "impl-1")
        self.assertEqual(receipt["observed_pr_head_sha"], SHA)
        self.assertRegex(receipt["packet_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertIsInstance(receipt["timestamp"], int)

    def test_graph_accept_failures_leave_state_byte_for_byte_unchanged(self) -> None:
        wrong_version = valid_packets()["IMPLEMENTATION_PACKET"]
        wrong_version["packet_version"] = 2
        cases = (
            ("head mismatch", valid_packets()["IMPLEMENTATION_PACKET"], "b" * 40, None),
            ("missing head", valid_packets()["IMPLEMENTATION_PACKET"], None, None),
            ("wrong packet type", valid_packets()["REVIEW_PACKET"], SHA, None),
            ("wrong packet version", wrong_version, SHA, None),
            ("malformed packet", {"packet_version": 1}, SHA, None),
            ("stale revision", valid_packets()["IMPLEMENTATION_PACKET"], SHA, 0),
        )
        for name, packet, observed_head, revision in cases:
            with self.subTest(name=name):
                self.tearDown()
                self.setUp()
                self.register("gepetto")
                self.register(
                    "implementation", "--coordinator-thread-id", "session-1",
                    session_id="impl-1",
                )
                self.state_command(
                    "ledger", "set", "--session-id", "session-1", "--lane", "impl-1",
                    "--json", '{"node":"implementation"}',
                )
                state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
                before = state_path.read_bytes()
                blocked = self.accept_command(
                    "IMPLEMENTATION_PACKET", packet, lane="impl-1", actor="impl-1",
                    expected_revision=revision, observed_head=observed_head, check=False,
                )
                self.assertEqual(blocked.returncode, 1)
                self.assertEqual(state_path.read_bytes(), before)

    def test_graph_accept_rejects_duplicate_packet_keys_without_mutation(self) -> None:
        self.register("gepetto")
        self.register(
            "implementation", "--coordinator-thread-id", "session-1", session_id="impl-1"
        )
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "impl-1",
            "--json", '{"node":"implementation"}',
        )
        revision = int(self.session_state()["state_revision"])
        packet_json = json.dumps(valid_packets()["IMPLEMENTATION_PACKET"])
        packet_json = packet_json.replace(
            '"packet_version": 1', '"packet_version": 2, "packet_version": 1', 1
        )
        state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        before = state_path.read_bytes()

        blocked = self.state_command(
            "graph", "accept", "--session-id", "session-1", "--lane", "impl-1",
            "--actor-session-id", "impl-1", "--expected-revision", str(revision),
            "--event", "IMPLEMENTATION_PACKET", "--packet-json", packet_json,
            "--observed-pr-head-sha", SHA, check=False,
        )

        self.assertEqual(blocked.returncode, 1)
        self.assertIn("duplicate key: packet_version", blocked.stderr)
        self.assertEqual(state_path.read_bytes(), before)

    def test_graph_accept_rejects_caller_supplied_workflow_without_mutation(self) -> None:
        self.register("gepetto")
        self.register(
            "implementation", "--coordinator-thread-id", "session-1", session_id="impl-1"
        )
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "impl-1",
            "--json", '{"node":"implementation"}',
        )
        revision = int(self.session_state()["state_revision"])
        state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        before = state_path.read_bytes()

        blocked = self.state_command(
            "graph", "accept", "--session-id", "session-1", "--lane", "impl-1",
            "--actor-session-id", "impl-1", "--expected-revision", str(revision),
            "--event", "IMPLEMENTATION_PACKET", "--packet-json",
            json.dumps(valid_packets()["IMPLEMENTATION_PACKET"]),
            "--observed-pr-head-sha", SHA, "--workflow", "/tmp/untrusted-workflow.json",
            check=False,
        )

        self.assertEqual(blocked.returncode, 2)
        self.assertIn("unrecognized arguments: --workflow", blocked.stderr)
        self.assertEqual(state_path.read_bytes(), before)

    def test_graph_accept_enforces_actor_and_jiminy_runner_authority(self) -> None:
        self.register("gepetto")
        self.register(
            "implementation", "--coordinator-thread-id", "session-1", session_id="impl-1"
        )
        self.register(
            "implementation", "--coordinator-thread-id", "session-1", session_id="impl-2"
        )
        self.register("review", "--coordinator-thread-id", "session-1", session_id="review-1")
        self.register("gepetto", session_id="other-coordinator")
        self.register(
            "implementation", "--coordinator-thread-id", "other-coordinator",
            session_id="impl-other",
        )
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "impl-1",
            "--json", '{"node":"implementation"}',
        )
        state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        for actor in ("impl-2", "review-1", "impl-other"):
            before = state_path.read_bytes()
            blocked = self.accept_command(
                "IMPLEMENTATION_PACKET", valid_packets()["IMPLEMENTATION_PACKET"],
                lane="impl-1", actor=actor, observed_head=SHA, check=False,
            )
            self.assertEqual(blocked.returncode, 1)
            self.assertEqual(state_path.read_bytes(), before)

        self.state_command("complete", "--session-id", "impl-1")
        before = state_path.read_bytes()
        inactive = self.accept_command(
            "IMPLEMENTATION_PACKET", valid_packets()["IMPLEMENTATION_PACKET"],
            lane="impl-1", actor="impl-1", observed_head=SHA, check=False,
        )
        self.assertEqual(inactive.returncode, 1)
        self.assertEqual(state_path.read_bytes(), before)

    def test_graph_accept_authorizes_matching_inactive_terminal_actor(self) -> None:
        self.register("gepetto")
        self.register(
            "implementation", "--coordinator-thread-id", "session-1", session_id="impl-1"
        )
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "impl-1",
            "--json", '{"node":"implementation"}',
        )
        packet = valid_packets()["IMPLEMENTATION_PACKET"]
        self.assertEqual(
            self.hook_for(
                "impl-1", "Stop",
                last_assistant_message=f"IMPLEMENTATION_PACKET:\n{json.dumps(packet)}",
                stop_hook_active=False,
            ),
            {},
        )

        accepted = json.loads(self.accept_command(
            "IMPLEMENTATION_PACKET", packet,
            lane="impl-1", actor="impl-1", observed_head=SHA,
        ).stdout)

        self.assertEqual(accepted["state"]["node"], "review")
        self.assertEqual(accepted["acceptance_receipt"]["actor_session_id"], "impl-1")

    def test_graph_accept_rejects_untrusted_inactive_terminal_variants_without_mutation(self) -> None:
        cases = ("packet mismatch", "wrong coordinator", "checkpoint predecessor", "forced stop")
        for case in cases:
            with self.subTest(case=case):
                self.tearDown()
                self.setUp()
                self.register("gepetto")
                coordinator = "session-1" if case != "wrong coordinator" else "other-coordinator"
                if coordinator != "session-1":
                    self.register("gepetto", session_id=coordinator)
                self.register(
                    "implementation", "--coordinator-thread-id", coordinator,
                    session_id="impl-1",
                )
                self.state_command(
                    "ledger", "set", "--session-id", "session-1", "--lane", "impl-1",
                    "--json", '{"node":"implementation"}',
                )
                packet = valid_packets()["IMPLEMENTATION_PACKET"]
                if case == "checkpoint predecessor":
                    self.state_command(
                        "continue", "--source-id", "impl-1", "--successor-id", "impl-2"
                    )
                else:
                    if case == "forced stop":
                        self.assertEqual(self.hook_for(
                            "impl-1", "Stop", last_assistant_message="invalid",
                            stop_hook_active=True,
                        ), {})
                    self.assertEqual(self.hook_for(
                        "impl-1", "Stop",
                        last_assistant_message=(
                            f"IMPLEMENTATION_PACKET:\n{json.dumps(packet)}"
                        ),
                        stop_hook_active=False,
                    ), {})
                supplied = json.loads(json.dumps(packet))
                if case == "packet mismatch":
                    supplied["artifact"]["observed_updated_at"] = "2026-07-23T00:00:00Z"
                state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
                before = state_path.read_bytes()

                blocked = self.accept_command(
                    "IMPLEMENTATION_PACKET", supplied,
                    lane="impl-1", actor="impl-1", observed_head=SHA, check=False,
                )

                self.assertEqual(blocked.returncode, 1)
                self.assertEqual(state_path.read_bytes(), before)

    def test_graph_accept_authorizes_coordinator_inline_single_leaf_research(self) -> None:
        self.register("gepetto")
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "session-1",
            "--json", '{"node":"research"}',
        )
        packet = valid_packets()["RESEARCH_PACKET"]

        accepted = json.loads(self.accept_command(
            "RESEARCH_PACKET", packet, lane="session-1", actor="session-1",
        ).stdout)

        self.assertEqual(accepted["state"]["node"], "implementation")
        self.assertEqual(accepted["acceptance_receipt"]["actor_session_id"], "session-1")
        self.assertRegex(
            accepted["state"]["validated_delivery_spec_digest"],
            r"^sha256:[0-9a-f]{64}$",
        )
        research_proof = accepted["state"]["proof_lifecycle"]["bindings"]["research"]
        self.assertEqual(
            research_proof["evidence"]["validated_delivery_spec_digest"],
            accepted["state"]["validated_delivery_spec_digest"],
        )
        self.assertEqual(
            accepted["acceptance_receipt"]["validated_delivery_spec_digest"],
            accepted["state"]["validated_delivery_spec_digest"],
        )

    def test_rejected_delivery_spec_does_not_mutate_coordinator_state(self) -> None:
        self.register("gepetto")
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "session-1",
            "--json", '{"node":"research"}',
        )
        invalid_artifact = Path(self.temporary.name) / "invalid-research.md"
        invalid_artifact.write_text("# Missing delivery specification\n", encoding="utf-8")
        state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        before = state_path.read_bytes()

        blocked = self.accept_command(
            "RESEARCH_PACKET", valid_packets()["RESEARCH_PACKET"],
            lane="session-1", actor="session-1",
            research_artifact=invalid_artifact, check=False,
        )

        self.assertEqual(blocked.returncode, 1)
        self.assertIn("exactly one readable", blocked.stderr)
        self.assertEqual(state_path.read_bytes(), before)

    def test_research_packet_contract_binding_preserves_valid_decisions(self) -> None:
        source = "https://github.com/owner/repo/issues/1"
        canonical = "https://github.com/owner/repo/issues/2"
        cases = (
            ("keep", valid_specification(), source, [source]),
            ("clarify", valid_specification(), source, [source]),
            ("split", valid_specification(multi_leaf=True), source, [source, canonical]),
            (
                "consolidate",
                {
                    **valid_specification(),
                    "leaves": [{
                        **valid_specification()["leaves"][0],
                        "issue_url": canonical,
                    }],
                },
                source,
                [canonical],
            ),
        )
        for decision, specification, issue_url, delivery_issue_urls in cases:
            with self.subTest(decision=decision):
                self.tearDown()
                self.setUp()
                self.register("gepetto")
                self.register(
                    "research", "--coordinator-thread-id", "session-1",
                    session_id="research-1",
                )
                self.state_command(
                    "ledger", "set", "--session-id", "session-1", "--lane", "research-1",
                    "--json", '{"node":"research"}',
                )
                artifact = Path(self.temporary.name) / f"{decision}.md"
                artifact.write_text(artifact_text(specification), encoding="utf-8")
                packet = valid_packets()["RESEARCH_PACKET"]
                packet["decision"] = decision
                packet["issue_url"] = issue_url
                packet["delivery_issue_urls"] = delivery_issue_urls

                accepted = json.loads(self.accept_command(
                    "RESEARCH_PACKET", packet, lane="research-1", actor="research-1",
                    research_artifact=artifact,
                ).stdout)

                self.assertEqual(accepted["state"]["node"], "implementation")

    def test_research_packet_contract_mismatches_do_not_mutate_coordinator_state(self) -> None:
        source = "https://github.com/owner/repo/issues/1"
        other = "https://github.com/owner/repo/issues/2"
        third = "https://github.com/owner/repo/issues/3"
        split_with_three = valid_specification(multi_leaf=True)
        split_with_three["leaves"].append({
            **split_with_three["leaves"][1],
            "id": "leaf-3",
            "issue_url": third,
            "dependencies": ["leaf-2"],
        })
        cases = (
            ("leaf mismatch", valid_specification(), "keep", source, [other]),
            ("keep source mismatch", valid_specification(), "keep", other, [source]),
            ("clarify source mismatch", valid_specification(), "clarify", other, [source]),
            ("split shape", split_with_three, "split", source, [source, other]),
            (
                "consolidate shape", valid_specification(multi_leaf=True),
                "consolidate", source, [source],
            ),
        )
        for name, specification, decision, issue_url, delivery_issue_urls in cases:
            with self.subTest(name=name):
                self.tearDown()
                self.setUp()
                self.register("gepetto")
                self.register(
                    "research", "--coordinator-thread-id", "session-1",
                    session_id="research-1",
                )
                self.state_command(
                    "ledger", "set", "--session-id", "session-1", "--lane", "research-1",
                    "--json", '{"node":"research"}',
                )
                artifact = Path(self.temporary.name) / f"invalid-{name}.md"
                artifact.write_text(artifact_text(specification), encoding="utf-8")
                packet = valid_packets()["RESEARCH_PACKET"]
                packet["decision"] = decision
                packet["issue_url"] = issue_url
                packet["delivery_issue_urls"] = delivery_issue_urls
                state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
                before = state_path.read_bytes()

                blocked = self.accept_command(
                    "RESEARCH_PACKET", packet, lane="research-1", actor="research-1",
                    research_artifact=artifact, check=False,
                )

                self.assertEqual(blocked.returncode, 1)
                self.assertEqual(state_path.read_bytes(), before)

    def test_graph_accept_preserves_dedicated_research_actor_authority(self) -> None:
        self.register("gepetto")
        self.register(
            "research", "--coordinator-thread-id", "session-1", session_id="research-1"
        )
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "research-1",
            "--json", '{"node":"research"}',
        )

        accepted = json.loads(self.accept_command(
            "RESEARCH_PACKET", valid_packets()["RESEARCH_PACKET"],
            lane="research-1", actor="research-1",
        ).stdout)

        self.assertEqual(accepted["state"]["node"], "implementation")

    def test_graph_accept_authorizes_matching_terminal_jiminy_actor(self) -> None:
        self.register("gepetto")
        self.register(
            "jiminy", "--coordinator-thread-id", "session-1", session_id="jiminy-1"
        )
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "delivery-lane",
            "--json", (
                '{"node":"integration_verification",'
                '"jiminy_runner_session_id":"jiminy-1"}'
            ),
        )
        packet = valid_packets()["JIMINY_COMPLETE"]
        self.assertEqual(self.hook_for(
            "jiminy-1", "Stop",
            last_assistant_message=f"JIMINY_COMPLETE:\n{json.dumps(packet)}",
            stop_hook_active=False,
        ), {})

        accepted = json.loads(self.accept_command(
            "JIMINY_COMPLETE", packet,
            lane="delivery-lane", actor="jiminy-1", runner="jiminy-1",
        ).stdout)

        self.assertEqual(accepted["state"]["node"], "complete")

    def test_graph_accept_rejects_non_fast_path_coordinator_research_without_mutation(self) -> None:
        for case in ("clarify", "split", "blocked artifact", "dedicated lane", "other coordinator"):
            with self.subTest(case=case):
                self.tearDown()
                self.setUp()
                self.register("gepetto")
                self.register("gepetto", session_id="other-coordinator")
                lane = "research-lane" if case == "dedicated lane" else "session-1"
                actor = "other-coordinator" if case == "other coordinator" else "session-1"
                self.state_command(
                    "ledger", "set", "--session-id", "session-1", "--lane", lane,
                    "--json", '{"node":"research"}',
                )
                packet = valid_packets()["RESEARCH_PACKET"]
                if case == "clarify":
                    packet["decision"] = "clarify"
                elif case == "split":
                    packet["decision"] = "split"
                    packet["delivery_issue_urls"].append(
                        "https://github.com/owner/repo/issues/2"
                    )
                elif case == "blocked artifact":
                    packet["artifact"] = {
                        "kind": "tmp_markdown", "status": "blocked", "marker": None,
                        "content_ref": "sha256:" + "c" * 64,
                        "locations": [{"path": "/tmp/research.md"}],
                    }
                state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
                before = state_path.read_bytes()

                blocked = self.accept_command(
                    "RESEARCH_PACKET", packet, lane=lane, actor=actor, check=False,
                )

                self.assertEqual(blocked.returncode, 1)
                self.assertEqual(state_path.read_bytes(), before)

    def test_graph_accept_requires_authorized_jiminy_runner_when_entering_merge(self) -> None:
        self.register("gepetto")
        self.register("review", "--coordinator-thread-id", "session-1", session_id="review-1")
        self.register("jiminy", "--coordinator-thread-id", "session-1", session_id="jiminy-1")
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "review-1",
            "--json", '{"node":"review","review_fix_cycles":0}',
        )
        state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        before = state_path.read_bytes()
        missing = self.accept_command(
            "REVIEW_PACKET", valid_packets()["REVIEW_PACKET"],
            lane="review-1", actor="review-1", observed_head=SHA, check=False,
        )
        self.assertEqual(missing.returncode, 1)
        self.assertIn("Jiminy runner", missing.stderr)
        self.assertEqual(state_path.read_bytes(), before)

        accepted = json.loads(self.accept_command(
            "REVIEW_PACKET", valid_packets()["REVIEW_PACKET"],
            lane="review-1", actor="review-1", observed_head=SHA, runner="jiminy-1",
        ).stdout)
        self.assertEqual(accepted["state"]["node"], "merge")
        self.assertEqual(accepted["state"]["jiminy_runner_session_id"], "jiminy-1")

    def test_graph_accept_binds_and_preserves_jiminy_runner(self) -> None:
        self.register("gepetto")
        self.register("jiminy", "--coordinator-thread-id", "session-1", session_id="jiminy-1")
        self.register("jiminy", "--coordinator-thread-id", "session-1", session_id="jiminy-2")
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "review-lane",
            "--json", json.dumps({
                "node": "merge", "head_sha": SHA,
                "pr": valid_packets()["JIMINY_PR_RESULT"]["pr_url"],
            }),
        )
        state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        before = state_path.read_bytes()
        unbound = self.accept_command(
            "JIMINY_PR_RESULT", valid_packets()["JIMINY_PR_RESULT"],
            lane="review-lane", actor="jiminy-1", check=False,
        )
        self.assertEqual(unbound.returncode, 1)
        self.assertIn("not bound to a runner", unbound.stderr)
        self.assertEqual(state_path.read_bytes(), before)

        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "review-lane",
            "--json", '{"jiminy_runner_session_id":"jiminy-1"}',
        )
        before = state_path.read_bytes()
        wrong = self.accept_command(
            "JIMINY_PR_RESULT", valid_packets()["JIMINY_PR_RESULT"],
            lane="review-lane", actor="jiminy-2", check=False,
        )
        self.assertEqual(wrong.returncode, 1)
        self.assertEqual(state_path.read_bytes(), before)

        self.state_command("continue", "--source-id", "jiminy-1", "--successor-id", "jiminy-3")
        ready = merge_ready_packet()
        ready["coordinator_thread_id"] = "session-1"
        revision = int(self.session_state()["state_revision"])
        self.state_command(
            "graph", "ready", "--session-id", "session-1", "--lane", "review-lane",
            "--expected-revision", str(revision), "--packet-json", json.dumps(ready),
            "--runner-session-id", "jiminy-3",
        )
        accepted = json.loads(self.accept_command(
            "JIMINY_PR_RESULT", valid_packets()["JIMINY_PR_RESULT"],
            lane="review-lane", actor="jiminy-3",
        ).stdout)
        self.assertEqual(accepted["state"]["jiminy_runner_session_id"], "jiminy-3")

    def test_graph_accept_concurrent_expected_revision_has_one_winner(self) -> None:
        self.register("gepetto")
        self.register(
            "implementation", "--coordinator-thread-id", "session-1", session_id="impl-1"
        )
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "impl-1",
            "--json", '{"node":"implementation"}',
        )
        revision = int(self.session_state()["state_revision"])
        arguments = [
            PYTHON, str(STATE), "graph", "accept", "--session-id", "session-1",
            "--lane", "impl-1", "--actor-session-id", "impl-1",
            "--expected-revision", str(revision), "--event", "IMPLEMENTATION_PACKET",
            "--packet-json", json.dumps(valid_packets()["IMPLEMENTATION_PACKET"]),
            "--observed-pr-head-sha", SHA,
        ]
        processes = [
            subprocess.Popen(
                arguments, env=self.env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        results = [process.communicate(timeout=10) + (process.returncode,) for process in processes]
        self.assertEqual(sorted(result[2] for result in results), [0, 1], results)
        failure = next(result for result in results if result[2] == 1)
        self.assertIn("state revision conflict", failure[1])

    def test_head_invalidation_advances_once_rejects_stale_proof_and_accepts_fresh_review(self) -> None:
        new_head = "b" * 40
        self.register("gepetto")
        self.register(
            "implementation", "--coordinator-thread-id", "session-1",
            session_id="impl-1",
        )
        self.register(
            "review", "--coordinator-thread-id", "session-1", session_id="review-1"
        )
        self.register(
            "jiminy", "--coordinator-thread-id", "session-1", session_id="jiminy-1"
        )
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "impl-1",
            "--json", json.dumps({
                "node": "implementation",
                "base_sha": "c" * 40,
                "pr": valid_packets()["IMPLEMENTATION_PACKET"]["pr_url"],
                "research_content_ref": "sha256:" + ("c" * 64),
            }),
        )
        implementation = valid_packets()["IMPLEMENTATION_PACKET"]
        self.accept_command(
            "IMPLEMENTATION_PACKET", implementation,
            lane="impl-1", actor="impl-1", observed_head=SHA,
        )
        self.state_command(
            "ledger", "move", "--session-id", "session-1",
            "--from-lane", "impl-1", "--to-lane", "review-1",
        )
        review = valid_packets()["REVIEW_PACKET"]
        self.accept_command(
            "REVIEW_PACKET", review, lane="review-1", actor="review-1",
            observed_head=SHA, runner="jiminy-1",
        )
        before = self.session_state()
        revision = int(before["state_revision"])
        invalidation = json.dumps({"observation": new_head, "reason": "PR head moved"})
        changed = json.loads(self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "review-1",
            "--current-node", "merge", "--event", "PR_HEAD_CHANGED",
            "--expected-revision", str(revision), "--context-json", invalidation,
            "--runner-session-id", "jiminy-1",
        ).stdout)

        lifecycle = changed["state"]["proof_lifecycle"]
        self.assertEqual(lifecycle["generations"], {"contract": 0, "base": 0, "head": 1})
        self.assertEqual(lifecycle["observations"]["head"], new_head)
        self.assertNotIn("review", lifecycle["bindings"])
        self.assertNotIn("ci", lifecycle["bindings"])
        self.assertNotIn("merge_ready", lifecycle["bindings"])
        self.assertNotIn("implementation", lifecycle["bindings"])
        self.assertEqual(len(lifecycle["invalidation_history"]), 1)
        self.assertEqual(len(changed["state"]["acceptance_receipts"]), 2)

        replay_revision = int(self.session_state()["state_revision"])
        replay = json.loads(self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "review-1",
            "--current-node", "review", "--event", "PR_HEAD_CHANGED",
            "--expected-revision", str(replay_revision), "--context-json", invalidation,
            "--runner-session-id", "jiminy-1",
        ).stdout)
        self.assertTrue(replay["idempotent"])
        self.assertEqual(int(self.session_state()["state_revision"]), replay_revision)

        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "review-1",
            "--json", '{"node":"implementation"}',
        )
        state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        unchanged = state_path.read_bytes()
        illegal_replay = self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "review-1",
            "--current-node", "implementation", "--event", "PR_HEAD_CHANGED",
            "--expected-revision", str(self.session_state()["state_revision"]),
            "--context-json", invalidation, check=False,
        )
        self.assertEqual(illegal_replay.returncode, 1)
        self.assertIn("found 0", illegal_replay.stderr)
        self.assertEqual(state_path.read_bytes(), unchanged)
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "review-1",
            "--json", '{"node":"review"}',
        )

        # Even a caller that rewrites the public workflow node cannot make old
        # review/CI/readiness bindings satisfy a current merge gate.
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "review-1",
            "--json", '{"node":"merge"}',
        )
        unchanged = state_path.read_bytes()
        stale_merge = self.accept_command(
            "JIMINY_PR_RESULT", valid_packets()["JIMINY_PR_RESULT"],
            lane="review-1", actor="jiminy-1", runner="jiminy-1", check=False,
        )
        self.assertEqual(stale_merge.returncode, 1)
        self.assertIn("found 0", stale_merge.stderr)
        self.assertEqual(state_path.read_bytes(), unchanged)
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "review-1",
            "--json", '{"node":"review"}',
        )

        unchanged = state_path.read_bytes()
        stale = self.accept_command(
            "REVIEW_PACKET", review, lane="review-1", actor="review-1",
            observed_head=SHA, runner="jiminy-1", check=False,
        )
        self.assertEqual(stale.returncode, 1)
        self.assertIn("requires a current review attempt", stale.stderr)
        self.assertEqual(state_path.read_bytes(), unchanged)

        attempt = json.loads(self.bind_review_attempt(
            lane="review-1", actor="review-1",
        ).stdout)["review_attempt_id"]
        unchanged = state_path.read_bytes()
        old_head = self.accept_command(
            "REVIEW_PACKET", review, lane="review-1", actor="review-1",
            observed_head=SHA, runner="jiminy-1", review_attempt=attempt,
            check=False,
        )
        self.assertEqual(old_head.returncode, 1)
        self.assertEqual(state_path.read_bytes(), unchanged)

        fresh = valid_packets()["REVIEW_PACKET"]
        fresh["reviewed_head_sha"] = new_head
        accepted = json.loads(self.accept_command(
            "REVIEW_PACKET", fresh, lane="review-1", actor="review-1",
            observed_head=new_head, runner="jiminy-1",
            review_attempt=attempt,
        ).stdout)
        self.assertEqual(accepted["state"]["node"], "merge")
        self.assertEqual(
            accepted["acceptance_receipt"]["generations"],
            {"contract": 0, "base": 0, "head": 1},
        )
        self.assertEqual(
            accepted["state"]["proof_lifecycle"]["bindings"]["review"]["generations"],
            {"contract": 0, "base": 0, "head": 1},
        )

        merge_result = valid_packets()["JIMINY_PR_RESULT"]
        merge_result["reviewed_head_sha"] = new_head
        before_ready = state_path.read_bytes()
        unauthorized_result = self.accept_command(
            "JIMINY_PR_RESULT", merge_result,
            lane="review-1", actor="jiminy-1", runner="jiminy-1", check=False,
        )
        self.assertEqual(unauthorized_result.returncode, 1)
        self.assertIn("expected_merge_set", unauthorized_result.stderr)
        self.assertEqual(state_path.read_bytes(), before_ready)

        ready = merge_ready_packet()
        ready["coordinator_thread_id"] = "session-1"
        ready["pull_requests"][0]["reviewed_head_sha"] = new_head
        revision = int(self.session_state()["state_revision"])
        stale_ready = self.state_command(
            "graph", "ready", "--session-id", "session-1", "--lane", "review-1",
            "--expected-revision", str(revision - 1),
            "--packet-json", json.dumps(ready),
            "--runner-session-id", "jiminy-1", check=False,
        )
        self.assertEqual(stale_ready.returncode, 1)
        self.assertEqual(state_path.read_bytes(), before_ready)
        monitoring_ready = json.loads(json.dumps(ready))
        monitoring_ready["merge_authority"] = "monitoring-only"
        self.state_command(
            "graph", "ready", "--session-id", "session-1", "--lane", "review-1",
            "--expected-revision", str(revision),
            "--packet-json", json.dumps(monitoring_ready),
            "--runner-session-id", "jiminy-1",
        )
        monitor_result = self.accept_command(
            "JIMINY_PR_RESULT", merge_result,
            lane="review-1", actor="jiminy-1", runner="jiminy-1", check=False,
        )
        self.assertEqual(monitor_result.returncode, 1)
        self.assertIn("merge authority", monitor_result.stderr)
        revision = int(self.session_state()["state_revision"])
        bound = json.loads(self.state_command(
            "graph", "ready", "--session-id", "session-1", "--lane", "review-1",
            "--expected-revision", str(revision),
            "--packet-json", json.dumps(ready),
            "--runner-session-id", "jiminy-1",
        ).stdout)
        expected_binding = bound["state"]["proof_lifecycle"]["bindings"]["expected_merge_set"]
        self.assertEqual(
            expected_binding["evidence"]["expected_pr_urls"], ready["expected_pr_urls"]
        )
        self.assertEqual(
            expected_binding["generations"], {"contract": 0, "base": 0, "head": 1}
        )

        merged = json.loads(self.accept_command(
            "JIMINY_PR_RESULT", merge_result,
            lane="review-1", actor="jiminy-1", runner="jiminy-1",
        ).stdout)
        self.assertEqual(merged["state"]["node"], "merge")
        revision = int(self.session_state()["state_revision"])
        before_forged_ready = state_path.read_bytes()
        forged_ready = self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "review-1",
            "--current-node", "merge", "--event", "MERGES_VERIFIED",
            "--expected-revision", str(revision), "--runner-session-id", "jiminy-1",
            "--context-json", json.dumps({
                "ready": {"expected_pr_urls": ["https://example.test/forged"]},
                "packet": {"merge_results": {"https://example.test/forged": "d" * 40}},
            }), check=False,
        )
        self.assertEqual(forged_ready.returncode, 1)
        self.assertEqual(state_path.read_bytes(), before_forged_ready)
        wrong_results = self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "review-1",
            "--current-node", "merge", "--event", "MERGES_VERIFIED",
            "--expected-revision", str(revision), "--runner-session-id", "jiminy-1",
            "--context-json", json.dumps({
                "packet": {"merge_results": {"https://example.test/forged": "d" * 40}},
            }), check=False,
        )
        self.assertEqual(wrong_results.returncode, 1)
        self.assertEqual(state_path.read_bytes(), before_forged_ready)
        verified = json.loads(self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "review-1",
            "--current-node", "merge", "--event", "MERGES_VERIFIED",
            "--expected-revision", str(revision), "--runner-session-id", "jiminy-1",
            "--context-json", json.dumps({
                "packet": {
                    "merge_results": {
                        merge_result["pr_url"]: merge_result["merge_commit_sha"],
                    },
                },
            }),
        ).stdout)
        self.assertEqual(verified["state"]["node"], "integration_verification")
        self.assertEqual(
            verified["state"]["proof_lifecycle"]["bindings"]["merge_ready"]["generations"],
            {"contract": 0, "base": 0, "head": 1},
        )

    def test_review_attempt_distinguishes_fresh_identical_proof_from_actor_change(self) -> None:
        self.register("gepetto")
        for reviewer in ("review-old", "review-new"):
            self.register(
                "review", "--coordinator-thread-id", "session-1",
                session_id=reviewer,
            )
        self.register(
            "jiminy", "--coordinator-thread-id", "session-1", session_id="jiminy-1"
        )
        review = valid_packets()["REVIEW_PACKET"]
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "review-old",
            "--json", json.dumps({
                "node": "review",
                "head_sha": SHA,
                "pr": "https://github.com/owner/repo/pull/2",
                "research_content_ref": "sha256:" + ("c" * 64),
            }),
        )
        self.accept_command(
            "REVIEW_PACKET", review, lane="review-old", actor="review-old",
            observed_head=SHA, runner="jiminy-1",
        )
        revision = int(self.session_state()["state_revision"])
        self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "review-old",
            "--current-node", "merge", "--event", "MATERIAL_CONTRACT_CHANGED",
            "--expected-revision", str(revision), "--runner-session-id", "jiminy-1",
            "--context-json", json.dumps({
                "observation": "sha256:" + ("d" * 64),
                "reason": "acceptance contract changed",
            }),
        )
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "review-old",
            "--json", '{"node":"review"}',
        )

        state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        before = state_path.read_bytes()
        missing_attempt = self.accept_command(
            "REVIEW_PACKET", review, lane="review-old", actor="review-old",
            observed_head=SHA, runner="jiminy-1", check=False,
        )
        self.assertEqual(missing_attempt.returncode, 1)
        self.assertIn("requires a current review attempt", missing_attempt.stderr)
        self.assertEqual(state_path.read_bytes(), before)

        wrong_actor = self.bind_review_attempt(
            lane="review-old", actor="review-new", check=False,
        )
        self.assertEqual(wrong_actor.returncode, 1)
        self.assertEqual(state_path.read_bytes(), before)
        stale_revision = self.bind_review_attempt(
            lane="review-old", actor="review-old",
            expected_revision=int(self.session_state()["state_revision"]) - 1,
            check=False,
        )
        self.assertEqual(stale_revision.returncode, 1)
        self.assertEqual(state_path.read_bytes(), before)

        first_binding = json.loads(self.bind_review_attempt(
            lane="review-old", actor="review-old",
        ).stdout)
        first_attempt = first_binding["review_attempt_id"]
        replay_revision = int(self.session_state()["state_revision"])
        before_replay = state_path.read_bytes()
        replayed_binding = json.loads(self.bind_review_attempt(
            lane="review-old", actor="review-old",
        ).stdout)
        self.assertTrue(replayed_binding["idempotent"])
        self.assertEqual(replayed_binding["review_attempt_id"], first_attempt)
        self.assertEqual(int(self.session_state()["state_revision"]), replay_revision)
        self.assertEqual(state_path.read_bytes(), before_replay)
        altered_attempt = first_attempt[:-1] + (
            "0" if first_attempt[-1] != "0" else "1"
        )
        before = state_path.read_bytes()
        altered = self.accept_command(
            "REVIEW_PACKET", review, lane="review-old", actor="review-old",
            observed_head=SHA, runner="jiminy-1",
            review_attempt=altered_attempt, check=False,
        )
        self.assertEqual(altered.returncode, 1)
        self.assertIn("stale, altered, consumed, or unauthorized", altered.stderr)
        self.assertEqual(state_path.read_bytes(), before)

        same_reviewer = json.loads(self.accept_command(
            "REVIEW_PACKET", review, lane="review-old", actor="review-old",
            observed_head=SHA, runner="jiminy-1", review_attempt=first_attempt,
        ).stdout)
        self.assertEqual(same_reviewer["state"]["node"], "merge")
        self.assertEqual(
            same_reviewer["acceptance_receipt"]["generations"],
            {"contract": 1, "base": 0, "head": 0},
        )
        self.assertEqual(
            same_reviewer["acceptance_receipt"]["actor_session_id"], "review-old"
        )
        self.assertEqual(
            same_reviewer["acceptance_receipt"]["review_attempt_id"], first_attempt
        )

        revision = int(self.session_state()["state_revision"])
        self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "review-old",
            "--current-node", "merge", "--event", "MATERIAL_CONTRACT_CHANGED",
            "--expected-revision", str(revision), "--runner-session-id", "jiminy-1",
            "--context-json", json.dumps({
                "observation": "sha256:" + ("e" * 64),
                "reason": "acceptance contract changed again",
            }),
        )
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "review-old",
            "--json", '{"node":"review"}',
        )
        self.state_command(
            "ledger", "move", "--session-id", "session-1",
            "--from-lane", "review-old", "--to-lane", "review-new",
        )

        before = state_path.read_bytes()
        actor_change_only = self.accept_command(
            "REVIEW_PACKET", review, lane="review-new", actor="review-new",
            observed_head=SHA, runner="jiminy-1", check=False,
        )
        self.assertEqual(actor_change_only.returncode, 1)
        self.assertIn("requires a current review attempt", actor_change_only.stderr)
        self.assertEqual(state_path.read_bytes(), before)

        second_attempt = json.loads(self.bind_review_attempt(
            lane="review-new", actor="review-new",
        ).stdout)["review_attempt_id"]
        before = state_path.read_bytes()
        old_attempt = self.accept_command(
            "REVIEW_PACKET", review, lane="review-new", actor="review-new",
            observed_head=SHA, runner="jiminy-1",
            review_attempt=first_attempt, check=False,
        )
        self.assertEqual(old_attempt.returncode, 1)
        self.assertIn("stale, altered, consumed, or unauthorized", old_attempt.stderr)
        self.assertEqual(state_path.read_bytes(), before)

        fresh_actor_attempt = json.loads(self.accept_command(
            "REVIEW_PACKET", review, lane="review-new", actor="review-new",
            observed_head=SHA, runner="jiminy-1", review_attempt=second_attempt,
        ).stdout)
        self.assertEqual(fresh_actor_attempt["state"]["node"], "merge")
        self.assertEqual(
            fresh_actor_attempt["acceptance_receipt"]["generations"],
            {"contract": 2, "base": 0, "head": 0},
        )

    def test_base_and_contract_invalidations_clear_scoped_current_proof(self) -> None:
        lane = {
            "node": "merge",
            "jiminy_runner_session_id": "jiminy-1",
            "base_sha": "b" * 40,
            "head_sha": SHA,
            "validated_delivery_spec_digest": "sha256:" + ("f" * 64),
            "proof_lifecycle": {
                "generations": {"contract": 2, "base": 3, "head": 4},
                "observations": {
                    "contract": "sha256:" + ("c" * 64),
                    "base": "b" * 40,
                    "head": SHA,
                },
                "bindings": {
                    name: {
                        "generations": {"contract": 2, "base": 3, "head": 4},
                        "evidence": {"name": name},
                    }
                    for name in (
                        "implementation", "review", "ci", "expected_merge_set",
                        "merge_result", "merge_ready",
                    )
                },
                "invalidation_history": [],
            },
        }
        self.register("gepetto")
        self.register(
            "jiminy", "--coordinator-thread-id", "session-1", session_id="jiminy-1"
        )
        # Seed a trusted lifecycle through the state primitive to exercise migration
        # semantics without granting the public ledger command write access to it.
        with patch.dict(os.environ, {"CODEX_ORCHESTRATION_STATE_DIR": self.temporary.name}):
            state = orchestration_state.load_state("session-1")
            state["ledger"] = {"lane-1": lane}
            orchestration_state.write_state(
                "session-1", state, expected_revision=int(state["state_revision"])
            )

        revision = int(self.session_state()["state_revision"])
        base_change = json.loads(self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "lane-1",
            "--current-node", "merge", "--event", "MATERIAL_BASE_CHANGED",
            "--expected-revision", str(revision), "--context-json",
            json.dumps({"observation": "d" * 40, "reason": "stack base moved"}),
            "--runner-session-id", "jiminy-1",
        ).stdout)
        lifecycle = base_change["state"]["proof_lifecycle"]
        self.assertEqual(lifecycle["generations"], {"contract": 2, "base": 4, "head": 4})
        self.assertEqual(lifecycle["bindings"], {})
        self.assertEqual(base_change["state"]["node"], "implementation")
        self.assertRegex(
            base_change["state"]["validated_delivery_spec_digest"],
            r"^sha256:[0-9a-f]{64}$",
        )

        state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        replay_revision = int(self.session_state()["state_revision"])
        before_replay = state_path.read_bytes()
        missing_authority = self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "lane-1",
            "--current-node", "implementation", "--event", "MATERIAL_BASE_CHANGED",
            "--expected-revision", str(replay_revision), "--context-json",
            json.dumps({"observation": "d" * 40, "reason": "stack base moved"}),
            check=False,
        )
        self.assertEqual(missing_authority.returncode, 1)
        self.assertIn("Jiminy runner", missing_authority.stderr)
        self.assertEqual(state_path.read_bytes(), before_replay)
        base_replay = json.loads(self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "lane-1",
            "--current-node", "implementation", "--event", "MATERIAL_BASE_CHANGED",
            "--expected-revision", str(replay_revision), "--context-json",
            json.dumps({"observation": "d" * 40, "reason": "stack base moved"}),
            "--runner-session-id", "jiminy-1",
        ).stdout)
        self.assertTrue(base_replay["idempotent"])
        self.assertEqual(int(self.session_state()["state_revision"]), replay_revision)
        self.assertEqual(state_path.read_bytes(), before_replay)

        # Restore a current implementation proof, then invalidate the contract.
        lifecycle["bindings"]["implementation"] = {
            "generations": dict(lifecycle["generations"]), "evidence": {"fresh": True},
        }
        lifecycle["bindings"]["review"] = {
            "generations": dict(lifecycle["generations"]), "evidence": {"fresh": True},
        }
        with patch.dict(os.environ, {"CODEX_ORCHESTRATION_STATE_DIR": self.temporary.name}):
            state = orchestration_state.load_state("session-1")
            state["ledger"]["lane-1"] = base_change["state"]
            orchestration_state.write_state(
                "session-1", state, expected_revision=int(state["state_revision"])
            )
        revision = int(self.session_state()["state_revision"])
        contract_change = json.loads(self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "lane-1",
            "--current-node", "implementation", "--event", "MATERIAL_CONTRACT_CHANGED",
            "--expected-revision", str(revision), "--context-json",
            json.dumps({
                "observation": "sha256:" + ("e" * 64),
                "reason": "acceptance contract changed",
            }),
        ).stdout)
        lifecycle = contract_change["state"]["proof_lifecycle"]
        self.assertEqual(lifecycle["generations"], {"contract": 3, "base": 4, "head": 4})
        self.assertEqual(lifecycle["bindings"], {})
        self.assertEqual(len(lifecycle["invalidation_history"]), 2)
        self.assertEqual(contract_change["state"]["node"], "research")
        self.assertNotIn("validated_delivery_spec_digest", contract_change["state"])

        replay_revision = int(self.session_state()["state_revision"])
        before_replay = state_path.read_bytes()
        contract_context = json.dumps({
            "observation": "sha256:" + ("e" * 64),
            "reason": "acceptance contract changed",
        })
        contract_replay = json.loads(self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "lane-1",
            "--current-node", "research", "--event", "MATERIAL_CONTRACT_CHANGED",
            "--expected-revision", str(replay_revision),
            "--context-json", contract_context,
        ).stdout)
        self.assertTrue(contract_replay["idempotent"])
        self.assertEqual(int(self.session_state()["state_revision"]), replay_revision)
        self.assertEqual(state_path.read_bytes(), before_replay)

        changed_reason = self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "lane-1",
            "--current-node", "research", "--event", "MATERIAL_CONTRACT_CHANGED",
            "--expected-revision", str(replay_revision), "--context-json",
            json.dumps({
                "observation": "sha256:" + ("e" * 64),
                "reason": "different reason",
            }), check=False,
        )
        self.assertEqual(changed_reason.returncode, 1)
        self.assertEqual(state_path.read_bytes(), before_replay)

    def test_jiminy_ready_requires_every_gate_without_mutation(self) -> None:
        self.register("gepetto")
        self.register(
            "jiminy", "--coordinator-thread-id", "session-1", session_id="jiminy-1"
        )
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "lane-1",
            "--json", json.dumps({
                "node": "merge",
                "head_sha": SHA,
                "pr": "https://github.com/owner/repo/pull/2",
                "jiminy_runner_session_id": "jiminy-1",
            }),
        )
        state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        cases = (
            ("review_packet_verified", False),
            ("required_checks_green", False),
            ("approvals_satisfied", False),
            ("approvals_satisfied", "unknown"),
            ("mergeable", False),
            ("mergeable", "unknown"),
            ("unresolved_required_threads", 1),
            ("unresolved_required_threads", "unknown"),
        )
        for gate, value in cases:
            with self.subTest(gate=gate, value=value):
                ready = merge_ready_packet()
                ready["coordinator_thread_id"] = "session-1"
                ready["pull_requests"][0]["gates"][gate] = value
                revision = int(self.session_state()["state_revision"])
                before = state_path.read_bytes()
                rejected = self.state_command(
                    "graph", "ready", "--session-id", "session-1",
                    "--lane", "lane-1", "--expected-revision", str(revision),
                    "--packet-json", json.dumps(ready),
                    "--runner-session-id", "jiminy-1", check=False,
                )
                self.assertEqual(rejected.returncode, 1)
                self.assertIn("every merge gate", rejected.stderr)
                self.assertEqual(state_path.read_bytes(), before)

        ready = merge_ready_packet()
        ready["coordinator_thread_id"] = "session-1"
        accepted = json.loads(self.state_command(
            "graph", "ready", "--session-id", "session-1", "--lane", "lane-1",
            "--expected-revision", str(self.session_state()["state_revision"]),
            "--packet-json", json.dumps(ready),
            "--runner-session-id", "jiminy-1",
        ).stdout)
        self.assertFalse(accepted["idempotent"])

    def test_merge_set_requires_current_accepted_result_for_every_expected_pr(self) -> None:
        self.register("gepetto")
        self.register(
            "jiminy", "--coordinator-thread-id", "session-1", session_id="jiminy-1"
        )
        pr_one = valid_packets()["JIMINY_PR_RESULT"]["pr_url"]
        pr_two = "https://github.com/owner/repo/pull/3"
        for lane, pr_url in (("lane-1", pr_one), ("lane-2", pr_two)):
            self.state_command(
                "ledger", "set", "--session-id", "session-1", "--lane", lane,
                "--json", json.dumps({
                    "node": "merge",
                    "head_sha": SHA,
                    "pr": pr_url,
                    "jiminy_runner_session_id": "jiminy-1",
                }),
            )

        ready = merge_ready_packet()
        ready["coordinator_thread_id"] = "session-1"
        second = json.loads(json.dumps(ready["pull_requests"][0]))
        second["pr_url"] = pr_two
        second["branch"] = "issue-2"
        ready["pull_requests"].append(second)
        ready["expected_pr_urls"] = [pr_one, pr_two]
        ready["merge_order"] = [pr_one, pr_two]
        for lane in ("lane-1", "lane-2"):
            revision = int(self.session_state()["state_revision"])
            self.state_command(
                "graph", "ready", "--session-id", "session-1", "--lane", lane,
                "--expected-revision", str(revision),
                "--packet-json", json.dumps(ready),
                "--runner-session-id", "jiminy-1",
            )

        result_one = valid_packets()["JIMINY_PR_RESULT"]
        self.accept_command(
            "JIMINY_PR_RESULT", result_one,
            lane="lane-1", actor="jiminy-1", runner="jiminy-1",
        )
        state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        before = state_path.read_bytes()
        fabricated = self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "lane-1",
            "--current-node", "merge", "--event", "MERGES_VERIFIED",
            "--runner-session-id", "jiminy-1", "--context-json", json.dumps({
                "packet": {"merge_results": {
                    pr_one: result_one["merge_commit_sha"],
                    pr_two: "d" * 40,
                }},
            }), check=False,
        )
        self.assertEqual(fabricated.returncode, 1)
        self.assertIn("accepted merge result", fabricated.stderr)
        self.assertEqual(state_path.read_bytes(), before)

        result_two = valid_packets()["JIMINY_PR_RESULT"]
        result_two["pr_url"] = pr_two
        result_two["merge_commit_sha"] = "c" * 40
        self.accept_command(
            "JIMINY_PR_RESULT", result_two,
            lane="lane-2", actor="jiminy-1", runner="jiminy-1",
        )
        before = state_path.read_bytes()
        forged_sha = self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "lane-1",
            "--current-node", "merge", "--event", "MERGES_VERIFIED",
            "--runner-session-id", "jiminy-1", "--context-json", json.dumps({
                "packet": {"merge_results": {
                    pr_one: result_one["merge_commit_sha"],
                    pr_two: "d" * 40,
                }},
            }), check=False,
        )
        self.assertEqual(forged_sha.returncode, 1)
        self.assertIn("must equal current accepted", forged_sha.stderr)
        self.assertEqual(state_path.read_bytes(), before)

        verified = json.loads(self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "lane-1",
            "--current-node", "merge", "--event", "MERGES_VERIFIED",
            "--runner-session-id", "jiminy-1", "--context-json", json.dumps({
                "packet": {"merge_results": {
                    pr_one: result_one["merge_commit_sha"],
                    pr_two: result_two["merge_commit_sha"],
                }},
            }),
        ).stdout)
        self.assertEqual(verified["state"]["node"], "integration_verification")

    def test_block_and_resume_capture_trusted_source_node_atomically(self) -> None:
        self.register("gepetto")
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "lane-1",
            "--json", '{"node":"review"}',
        )
        blocked = json.loads(self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "lane-1",
            "--current-node", "review", "--event", "FLOW_BLOCKED",
        ).stdout)
        self.assertEqual(blocked["state"]["node"], "blocked")
        self.assertEqual(blocked["state"]["resume_node"], "review")

        state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        before = state_path.read_bytes()
        forged = self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "lane-1",
            "--current-node", "blocked", "--event", "RESUME_AUTHORIZED",
            "--context-json", '{"resume_node":"implementation"}', check=False,
        )
        self.assertEqual(forged.returncode, 1)
        self.assertEqual(state_path.read_bytes(), before)

        resumed = json.loads(self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "lane-1",
            "--current-node", "blocked", "--event", "RESUME_AUTHORIZED",
        ).stdout)
        self.assertEqual(resumed["state"]["node"], "review")
        self.assertNotIn("resume_node", resumed["state"])

        for index, (event, target) in enumerate((
            ("REVIEW_FIX_LIMIT_EXCEEDED", "needs_decision"),
            ("LANE_UNRESPONSIVE", "blocked"),
            ("RESTART_BUDGET_EXCEEDED", "needs_decision"),
        ), start=2):
            lane = f"lane-{index}"
            self.state_command(
                "ledger", "set", "--session-id", "session-1", "--lane", lane,
                "--json", '{"node":"review"}',
            )
            paused = json.loads(self.state_command(
                "graph", "apply", "--session-id", "session-1", "--lane", lane,
                "--current-node", "review", "--event", event,
            ).stdout)
            self.assertEqual(paused["state"]["node"], target)
            self.assertEqual(paused["state"]["resume_node"], "review")
            resumed = json.loads(self.state_command(
                "graph", "apply", "--session-id", "session-1", "--lane", lane,
                "--current-node", target, "--event", "RESUME_AUTHORIZED",
            ).stdout)
            self.assertEqual(resumed["state"]["node"], "review")
            self.assertNotIn("resume_node", resumed["state"])

    def test_pre_f4_lane_backfills_typed_research_artifact_observation(self) -> None:
        self.register("gepetto")
        content_ref = valid_packets()["IMPLEMENTATION_PACKET"]["artifact"]["content_ref"]
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "legacy-lane",
            "--json", json.dumps({
                "node": "review",
                "research_content_ref": content_ref,
                "base_sha": "b" * 40,
                "head_sha": SHA,
            }),
        )
        migrated = json.loads(self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "legacy-lane",
            "--current-node", "review", "--event", "FLOW_BLOCKED",
        ).stdout)
        self.assertEqual(
            migrated["state"]["proof_lifecycle"]["observations"],
            {"contract": content_ref, "base": "b" * 40, "head": SHA},
        )

        partial_lane = {
            "node": "review",
            "research_content_ref": content_ref,
            "proof_lifecycle": {
                "generations": {"contract": 0, "base": 0, "head": 0},
                "observations": {"contract": None, "base": None, "head": None},
                "bindings": {},
                "invalidation_history": [],
            },
        }
        with patch.dict(os.environ, {"CODEX_ORCHESTRATION_STATE_DIR": self.temporary.name}):
            state = orchestration_state.load_state("session-1")
            state["ledger"]["partial-legacy-lane"] = partial_lane
            orchestration_state.write_state(
                "session-1", state, expected_revision=int(state["state_revision"])
            )
        repaired = json.loads(self.state_command(
            "graph", "apply", "--session-id", "session-1",
            "--lane", "partial-legacy-lane", "--current-node", "review",
            "--event", "FLOW_BLOCKED",
        ).stdout)
        self.assertEqual(
            repaired["state"]["proof_lifecycle"]["observations"]["contract"],
            content_ref,
        )

        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "bad-legacy-lane",
            "--json", '{"node":"review","research_content_ref":"not-a-ref"}',
        )
        state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        before = state_path.read_bytes()
        malformed = self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "bad-legacy-lane",
            "--current-node", "review", "--event", "FLOW_BLOCKED", check=False,
        )
        self.assertEqual(malformed.returncode, 1)
        self.assertIn("content reference", malformed.stderr)
        self.assertEqual(state_path.read_bytes(), before)

    def test_invalidation_malformed_or_stale_revision_is_no_mutation(self) -> None:
        self.register("gepetto")
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "lane-1",
            "--json", '{"node":"review","head_sha":"' + SHA + '"}',
        )
        state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        revision = int(self.session_state()["state_revision"])
        cases = (
            (str(revision - 1), {"observation": "b" * 40, "reason": "drift"}),
            (str(revision), {"observation": "short", "reason": "drift"}),
            (str(revision), {"observation": "b" * 40, "reason": ""}),
            (str(revision), {"observation": "b" * 40, "reason": "drift", "resume_node": "merge"}),
        )
        for expected_revision, context in cases:
            with self.subTest(context=context):
                before = state_path.read_bytes()
                result = self.state_command(
                    "graph", "apply", "--session-id", "session-1", "--lane", "lane-1",
                    "--current-node", "review", "--event", "PR_HEAD_CHANGED",
                    "--expected-revision", expected_revision,
                    "--context-json", json.dumps(context), check=False,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(state_path.read_bytes(), before)

    def test_public_ledger_cannot_overwrite_trusted_lifecycle_or_resume_state(self) -> None:
        self.register("gepetto")
        state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        before = state_path.read_bytes()
        for update in (
            {"proof_lifecycle": {"generations": {}}},
            {"resume_node": "merge"},
            {"expected_pr_urls": ["https://example.test/pr/1"]},
            {"merge_ready": True},
        ):
            with self.subTest(update=update):
                result = self.state_command(
                    "ledger", "set", "--session-id", "session-1", "--lane", "lane-1",
                    "--json", json.dumps(update), check=False,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(state_path.read_bytes(), before)

    def test_packet_events_cannot_use_free_form_graph_apply(self) -> None:
        self.register("gepetto")
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "impl-1",
            "--json", '{"node":"implementation","review_fix_cycles":4}',
        )
        state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        before = state_path.read_bytes()
        blocked = self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "impl-1",
            "--current-node", "implementation", "--event", "IMPLEMENTATION_PACKET",
            "--context-json", json.dumps({
                "packet": valid_packets()["IMPLEMENTATION_PACKET"],
                "live": {"pr_head_sha": SHA}, "review_fix_cycles": 0,
            }), check=False,
        )
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("must use graph accept", blocked.stderr)
        self.assertEqual(state_path.read_bytes(), before)

    def test_atomic_state_replace_failure_preserves_prior_bytes_and_cleans_stage(self) -> None:
        self.register("gepetto")
        state_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        before = state_path.read_bytes()
        candidate = self.session_state()
        candidate["active"] = False

        with patch.dict(os.environ, {
            "CODEX_ORCHESTRATION_STATE_DIR": self.temporary.name,
        }), patch.object(Path, "replace", side_effect=OSError("injected replace failure")):
            with self.assertRaisesRegex(OSError, "injected replace failure"):
                orchestration_state.write_state(
                    "session-1", candidate,
                    expected_revision=int(candidate["state_revision"]),
                )

        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(list(state_path.parent.glob(".session-1.json.*.tmp")), [])

    def test_blocking_workflow_load_does_not_hold_registry_lock(self) -> None:
        self.register("gepetto")
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "review-lane",
            "--json", '{"node":"review","review_fix_cycles":0}',
        )
        fifo = Path(self.temporary.name) / "workflow.fifo"
        os.mkfifo(fifo)
        applying = subprocess.Popen(
            [
                PYTHON, str(STATE), "graph", "apply", "--session-id", "session-1",
                "--lane", "review-lane", "--current-node", "review",
                "--event", "ACTIONABLE_FINDINGS", "--workflow", str(fifo),
            ],
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        time.sleep(0.1)
        status = subprocess.run(
            [PYTHON, str(STATE), "status"], env=self.env, text=True,
            capture_output=True, timeout=1, check=True,
        )
        self.assertIn("session-1", status.stdout)
        workflow = STATE.parents[1] / "gepetto" / "references" / "workflow.json"
        with fifo.open("wb", buffering=0) as handle:
            handle.write(workflow.read_bytes())
        stdout, stderr = applying.communicate(timeout=2)
        self.assertEqual(applying.returncode, 0, (stdout, stderr))

    def test_context_binding_requires_reload_only_when_digest_changes(self) -> None:
        self.register("implementation")
        instructions = Path(self.temporary.name) / "AGENTS.md"
        instructions.write_text("first rules\n", encoding="utf-8")

        first = json.loads(self.state_command(
            "context", "bind",
            "--session-id", "session-1",
            "--key", "repository-instructions",
            "--file", str(instructions),
        ).stdout)
        unchanged = json.loads(self.state_command(
            "context", "bind",
            "--session-id", "session-1",
            "--key", "repository-instructions",
            "--file", str(instructions),
        ).stdout)
        instructions.write_text("second rules\n", encoding="utf-8")
        changed = json.loads(self.state_command(
            "context", "bind",
            "--session-id", "session-1",
            "--key", "repository-instructions",
            "--file", str(instructions),
        ).stdout)

        self.assertTrue(first["reload_required"])
        self.assertFalse(unchanged["reload_required"])
        self.assertTrue(changed["reload_required"])
        self.assertNotEqual(first["ref"], changed["ref"])

    def test_context_digest_is_stable_until_file_content_changes(self) -> None:
        artifact = Path(self.temporary.name) / "research.md"
        artifact.write_text("approved scope\n", encoding="utf-8")
        first = json.loads(self.state_command("context", "digest", "--file", str(artifact)).stdout)
        same = json.loads(self.state_command("context", "digest", "--file", str(artifact)).stdout)
        artifact.write_text("changed scope\n", encoding="utf-8")
        changed = json.loads(self.state_command("context", "digest", "--file", str(artifact)).stdout)

        self.assertEqual(first["digest"], same["digest"])
        self.assertNotEqual(first["digest"], changed["digest"])
        self.assertEqual(first["ref"], f"sha256:{first['digest']}")

    def test_context_digest_is_versioned_domain_separated_and_carries_arity(self) -> None:
        whole = Path(self.temporary.name) / "whole"
        left = Path(self.temporary.name) / "left"
        right = Path(self.temporary.name) / "right"
        whole.write_bytes(b"ab")
        left.write_bytes(b"a")
        right.write_bytes(b"b")
        single = json.loads(self.state_command("context", "digest", "--file", str(whole)).stdout)
        multiple = json.loads(self.state_command(
            "context", "digest", "--file", str(left), "--file", str(right),
        ).stdout)

        self.assertEqual(single["digest_version"], 1)
        self.assertEqual(single["arity"], 1)
        self.assertEqual(multiple["arity"], 2)
        self.assertNotEqual(single["digest"], multiple["digest"])

    def test_missing_context_file_fails_without_mutating_state(self) -> None:
        self.register("implementation")
        before = self.session_state()
        result = self.state_command(
            "context", "bind", "--session-id", "session-1",
            "--key", "research-artifact",
            "--file", str(Path(self.temporary.name) / "missing.md"),
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("No such file", result.stderr)
        self.assertEqual(self.session_state(), before)

    def test_blocking_context_read_does_not_hold_registry_lock(self) -> None:
        self.register("gepetto")
        fifo = Path(self.temporary.name) / "context.fifo"
        os.mkfifo(fifo)
        binding = subprocess.Popen(
            [
                PYTHON, str(STATE), "context", "bind", "--session-id", "session-1",
                "--key", "fifo", "--file", str(fifo),
            ],
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        time.sleep(0.1)
        status = subprocess.run(
            [PYTHON, str(STATE), "status"], env=self.env, text=True,
            capture_output=True, timeout=1, check=True,
        )
        self.assertIn("session-1", status.stdout)
        with fifo.open("wb", buffering=0) as handle:
            handle.write(b"stable context")
        stdout, stderr = binding.communicate(timeout=2)
        self.assertEqual(binding.returncode, 0, (stdout, stderr))

    def test_malformed_state_containers_fail_cleanly_without_mutation(self) -> None:
        self.register("gepetto")
        path = Path(self.temporary.name) / "sessions" / "session-1.json"
        malformed = self.session_state()
        malformed["context_refs"] = []
        malformed["ledger"] = []
        path.write_text(json.dumps(malformed), encoding="utf-8")
        before = path.read_bytes()
        artifact = Path(self.temporary.name) / "artifact.md"
        artifact.write_text("content", encoding="utf-8")

        context = self.state_command(
            "context", "bind", "--session-id", "session-1", "--key", "artifact",
            "--file", str(artifact), check=False,
        )
        ledger = self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "lane-1",
            "--json", "{}", check=False,
        )
        self.assertEqual(context.returncode, 1)
        self.assertEqual(ledger.returncode, 1)
        self.assertNotIn("Traceback", context.stderr + ledger.stderr)
        self.assertEqual(path.read_bytes(), before)

    def test_status_lists_sessions(self) -> None:
        self.register("gepetto")
        self.state_command("register", "--session-id", "session-2", "--role", "review", "--coordinator-thread-id", "session-1")
        self.state_command("complete", "--session-id", "session-2")
        lines = self.state_command("status").stdout.splitlines()
        self.assertIn("session-1 role=gepetto active=true coordinator=-", lines)
        self.assertIn("session-2 role=review active=false coordinator=session-1", lines)

    def test_child_verifies_authoritative_coordinator_registration_without_mutation(self) -> None:
        self.register("gepetto")
        self.state_command(
            "register", "--session-id", "session-2", "--role", "implementation",
            "--coordinator-thread-id", "session-1",
        )
        before = self.session_state("session-2")
        verified = json.loads(self.state_command(
            "verify", "--session-id", "session-2", "--role", "implementation",
            "--coordinator-thread-id", "session-1",
        ).stdout)
        after = self.session_state("session-2")

        self.assertEqual(verified, {
            "active": True,
            "coordinator_thread_id": "session-1",
            "role": "implementation",
            "session_id": "session-2",
            "verified": True,
        })
        self.assertEqual(after, before)

        mismatch = self.state_command(
            "verify", "--session-id", "session-2", "--role", "review",
            "--coordinator-thread-id", "session-1", check=False,
        )
        self.assertEqual(mismatch.returncode, 1)
        self.assertIn("role mismatch", mismatch.stderr)

    def test_child_registration_requires_active_gepetto_coordinator(self) -> None:
        missing = self.state_command(
            "register", "--session-id", "session-2", "--role", "implementation",
            "--coordinator-thread-id", "session-1", check=False,
        )
        self.assertEqual(missing.returncode, 1)
        self.assertIn("active Gepetto coordinator", missing.stderr)

        self.register("review")
        wrong_role = self.state_command(
            "register", "--session-id", "session-2", "--role", "implementation",
            "--coordinator-thread-id", "session-1", check=False,
        )
        self.assertEqual(wrong_role.returncode, 1)
        self.assertIn("active Gepetto coordinator", wrong_role.stderr)

    def test_all_child_roles_require_coordinator_and_gepetto_rejects_one(self) -> None:
        for role in ("research", "implementation", "review", "jiminy"):
            result = self.state_command(
                "register", "--session-id", f"standalone-{role}", "--role", role, check=False,
            )
            self.assertEqual(result.returncode, 1, role)
        self.register("gepetto")
        nested = self.state_command(
            "register", "--session-id", "nested-gepetto", "--role", "gepetto",
            "--coordinator-thread-id", "session-1", check=False,
        )
        self.assertEqual(nested.returncode, 1)

    def test_conflicting_child_reregistration_is_rejected(self) -> None:
        self.register("gepetto")
        self.state_command(
            "register", "--session-id", "session-2", "--role", "implementation",
            "--coordinator-thread-id", "session-1",
        )
        before = self.session_state("session-2")
        conflict = self.state_command(
            "register", "--session-id", "session-2", "--role", "review",
            "--coordinator-thread-id", "session-1", check=False,
        )

        self.assertEqual(conflict.returncode, 1)
        self.assertIn("already authoritative", conflict.stderr)
        self.assertEqual(self.session_state("session-2"), before)

    def test_identical_reregistration_is_read_only_and_cannot_revive_lane(self) -> None:
        self.register("jiminy", "--merge-authorized", "--no-checkpoint")
        before = self.session_state()
        repeated = self.state_command(
            "register", "--session-id", "session-1", "--role", "jiminy",
            "--coordinator-thread-id", "session-1-coordinator",
        )
        self.assertEqual(repeated.returncode, 0)
        self.assertEqual(self.session_state(), before)

        self.state_command("complete", "--session-id", "session-1")
        completed = self.session_state()
        revive = self.state_command(
            "register", "--session-id", "session-1", "--role", "jiminy",
            "--coordinator-thread-id", "session-1-coordinator",
            check=False,
        )
        self.assertEqual(revive.returncode, 1)
        self.assertEqual(self.session_state(), completed)

    def test_child_verification_fails_after_coordinator_completes(self) -> None:
        self.register("gepetto")
        self.state_command(
            "register", "--session-id", "session-2", "--role", "implementation",
            "--coordinator-thread-id", "session-1",
        )
        self.state_command("complete", "--session-id", "session-1")

        result = self.state_command(
            "verify", "--session-id", "session-2", "--role", "implementation",
            "--coordinator-thread-id", "session-1", check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("active Gepetto coordinator", result.stderr)

        implicit = self.state_command(
            "verify", "--session-id", "session-2", "--role", "implementation",
            check=False,
        )
        self.assertEqual(implicit.returncode, 1)
        self.assertIn("active Gepetto coordinator", implicit.stderr)

    def test_child_verification_follows_coordinator_checkpoint_lineage(self) -> None:
        self.register("gepetto")
        self.state_command(
            "register", "--session-id", "session-2", "--role", "implementation",
            "--coordinator-thread-id", "session-1",
        )
        self.state_command(
            "continue", "--source-id", "session-1", "--successor-id", "session-3",
        )

        verified = json.loads(self.state_command(
            "verify", "--session-id", "session-2", "--role", "implementation",
            "--coordinator-thread-id", "session-3",
        ).stdout)
        self.assertEqual(verified["coordinator_thread_id"], "session-3")
        self.assertTrue(verified["verified"])

    def test_merge_requires_authorized_jiminy_and_bound_head(self) -> None:
        self.register("jiminy")
        denied = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": f"gh pr merge 1 --squash --match-head-commit {SHA}"})
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

        self.register("jiminy", "--merge-authorized", session_id="authorized-jiminy")
        unbound = self.hook("PreToolUse", session_id="authorized-jiminy", tool_name="Bash", tool_input={"command": "gh pr merge 1 --squash"})
        self.assertEqual(unbound["hookSpecificOutput"]["permissionDecision"], "deny")
        allowed = self.hook("PreToolUse", session_id="authorized-jiminy", tool_name="Bash", tool_input={"command": f"gh pr merge 1 --squash --match-head-commit {SHA}"})
        self.assertIsNone(allowed)

    def session_state(self, session_id: str = "session-1") -> dict[str, object]:
        path = Path(self.temporary.name) / "sessions" / f"{session_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_hook_events_stamp_heartbeat_and_count_events(self) -> None:
        self.register("implementation")
        self.assertNotIn("last_heartbeat", self.session_state())
        self.hook("SessionStart", source="startup")
        state = self.session_state()
        self.assertIsInstance(state["last_heartbeat"], int)
        self.assertEqual(state["events"], 1)
        self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "ls"})
        self.assertEqual(self.session_state()["events"], 2)

    def test_continue_resets_successor_events(self) -> None:
        self.register("implementation")
        self.hook("SessionStart", source="startup")
        self.hook("SessionStart", source="startup")
        self.state_command("continue", "--source-id", "session-1", "--successor-id", "session-2")
        self.assertEqual(self.session_state()["events"], 2)
        successor = self.session_state("session-2")
        self.assertEqual(successor["events"], 0)
        self.assertIsInstance(successor["last_heartbeat"], int)

    def test_continue_preserves_context_refs_but_resets_ephemeral_pressure(self) -> None:
        self.register("implementation")
        artifact = Path(self.temporary.name) / "research.md"
        artifact.write_text("stable contract\n", encoding="utf-8")
        self.state_command(
            "context", "bind", "--session-id", "session-1",
            "--key", "research-artifact", "--source", str(artifact),
            "--file", str(artifact),
        )
        self.state_command(
            "pressure", "record", "--session-id", "session-1",
            "--context-used-tokens", "800", "--context-limit-tokens", "1000",
        )
        self.state_command("continue", "--source-id", "session-1", "--successor-id", "session-2")

        successor = self.session_state("session-2")
        self.assertTrue(successor["context_refs"]["research-artifact"]["ref"].startswith("sha256:"))
        self.assertNotIn("pressure", successor)

    def test_continue_rejects_same_or_existing_successor_without_mutation(self) -> None:
        self.register("implementation")
        before = self.session_state()
        same = self.state_command(
            "continue", "--source-id", "session-1", "--successor-id", "session-1",
            check=False,
        )
        self.assertEqual(same.returncode, 1)
        self.assertEqual(self.session_state(), before)

        self.register("review", session_id="session-2")
        successor_before = self.session_state("session-2")
        existing = self.state_command(
            "continue", "--source-id", "session-1", "--successor-id", "session-2",
            check=False,
        )
        self.assertEqual(existing.returncode, 1)
        self.assertEqual(self.session_state(), before)
        self.assertEqual(self.session_state("session-2"), successor_before)

    def test_continue_rolls_back_if_second_staged_commit_fails(self) -> None:
        self.register("implementation")
        source_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        before = source_path.read_bytes()
        original_replace = Path.replace
        calls = 0

        def fail_second_replace(path: Path, target: Path) -> Path:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected source commit failure")
            return original_replace(path, target)

        with patch.dict(os.environ, {"CODEX_ORCHESTRATION_STATE_DIR": self.temporary.name}), patch.object(
            Path, "replace", fail_second_replace
        ):
            with self.assertRaisesRegex(OSError, "injected source commit failure"):
                orchestration_state.continue_session("session-1", "session-2")

        self.assertEqual(source_path.read_bytes(), before)
        self.assertFalse((Path(self.temporary.name) / "sessions" / "session-2.json").exists())

    def test_continue_stage_failure_removes_journal_and_stages_without_mutation(self) -> None:
        self.register("implementation")
        source_path = Path(self.temporary.name) / "sessions" / "session-1.json"
        before = source_path.read_bytes()
        original_stage = orchestration_state._stage_state
        calls = 0

        def fail_second_stage(*args: object, **kwargs: object) -> tuple[Path, Path]:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected successor staging failure")
            return original_stage(*args, **kwargs)

        with patch.dict(os.environ, {"CODEX_ORCHESTRATION_STATE_DIR": self.temporary.name}), patch.object(
            orchestration_state, "_stage_state", side_effect=fail_second_stage
        ):
            with self.assertRaisesRegex(OSError, "injected successor staging failure"):
                orchestration_state.continue_session("session-1", "session-2")

        self.assertEqual(source_path.read_bytes(), before)
        self.assertFalse((Path(self.temporary.name) / "transactions" / "continuation.json").exists())
        self.assertEqual(list((Path(self.temporary.name) / "sessions").glob(".*.tmp")), [])

    def test_hard_exit_continuation_recovers_one_active_owner_on_next_operation(self) -> None:
        self.register("implementation")
        crash_env = dict(self.env, CODEX_ORCHESTRATION_TEST_CRASH_AFTER="successor")
        crashed = subprocess.run(
            [PYTHON, str(STATE), "continue", "--source-id", "session-1", "--successor-id", "session-2"],
            env=crash_env, text=True, capture_output=True,
        )
        self.assertEqual(crashed.returncode, 91)
        self.assertTrue(self.session_state()["active"])
        self.assertTrue(self.session_state("session-2")["active"])

        self.state_command("status")
        self.assertFalse(self.session_state()["active"])
        self.assertTrue(self.session_state("session-2")["active"])
        self.assertFalse((Path(self.temporary.name) / "transactions" / "continuation.json").exists())


if __name__ == "__main__":
    unittest.main()
