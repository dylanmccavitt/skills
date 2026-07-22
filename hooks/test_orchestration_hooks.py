#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hooks import orchestration_state
from hooks.test_orchestration_packets import valid_packets


HOOK = Path(__file__).with_name("orchestration_hook.py")
STATE = Path(__file__).with_name("orchestration_state.py")
CONFIG = Path(__file__).with_name("hooks.json")
PYTHON = "python3"
HOOK_COMMAND = '/usr/bin/env python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_hook.py"'
SHA = "a" * 40


def packet_message(packet_type: str) -> str:
    return f"{packet_type}:\n{json.dumps(valid_packets()[packet_type])}"


class OrchestrationHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, CODEX_ORCHESTRATION_STATE_DIR=self.temporary.name)

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

    def test_graph_transition_into_jiminy_node_requires_registered_runner(self) -> None:
        self.register("gepetto")
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "review-lane",
            "--json", '{"node":"review","review_fix_cycles":0}',
        )
        context = json.dumps(
            {
                "packet": valid_packets()["REVIEW_PACKET"],
                "live": {"pr_head_sha": SHA},
            }
        )
        blocked = self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "review-lane",
            "--current-node", "review", "--event", "REVIEW_PACKET",
            "--context-json", context, check=False,
        )
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("Jiminy runner", blocked.stderr)
        self.assertEqual(self.session_state()["ledger"]["review-lane"]["node"], "review")

    def test_graph_transition_accepts_jiminy_runner_bound_to_coordinator(self) -> None:
        self.register("gepetto")
        self.register(
            "jiminy", "--coordinator-thread-id", "session-1", session_id="jiminy-1"
        )
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "review-lane",
            "--json", '{"node":"review","review_fix_cycles":0}',
        )
        context = json.dumps(
            {
                "packet": valid_packets()["REVIEW_PACKET"],
                "live": {"pr_head_sha": SHA},
            }
        )
        applied = self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "review-lane",
            "--current-node", "review", "--event", "REVIEW_PACKET",
            "--context-json", context, "--runner-session-id", "jiminy-1",
        )
        result = json.loads(applied.stdout)
        self.assertEqual(result["transition_id"], "review-approved")
        self.assertEqual(result["state"]["node"], "merge")
        self.assertEqual(result["state"]["jiminy_runner_session_id"], "jiminy-1")

    def test_graph_transition_rejects_replacing_bound_jiminy_runner(self) -> None:
        self.register("gepetto")
        self.register(
            "jiminy", "--coordinator-thread-id", "session-1", session_id="jiminy-1"
        )
        self.register(
            "jiminy", "--coordinator-thread-id", "session-1", session_id="jiminy-2"
        )
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "review-lane",
            "--json", f'{{"node":"merge","head_sha":"{SHA}","jiminy_runner_session_id":"jiminy-1"}}',
        )
        context = json.dumps({"packet": valid_packets()["JIMINY_PR_RESULT"]})

        blocked = self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "review-lane",
            "--current-node", "merge", "--event", "JIMINY_PR_RESULT",
            "--context-json", context, "--runner-session-id", "jiminy-2", check=False,
        )

        self.assertEqual(blocked.returncode, 1)
        self.assertIn("bound to Jiminy runner jiminy-1", blocked.stderr)
        lane = self.session_state()["ledger"]["review-lane"]
        self.assertEqual(lane["node"], "merge")
        self.assertEqual(lane["jiminy_runner_session_id"], "jiminy-1")

    def test_graph_transition_accepts_bound_jiminy_checkpoint_successor(self) -> None:
        self.register("gepetto")
        self.register(
            "jiminy", "--coordinator-thread-id", "session-1", session_id="jiminy-1"
        )
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "review-lane",
            "--json", f'{{"node":"merge","head_sha":"{SHA}","jiminy_runner_session_id":"jiminy-1"}}',
        )
        self.state_command(
            "continue", "--source-id", "jiminy-1", "--successor-id", "jiminy-2",
        )
        context = json.dumps({"packet": valid_packets()["JIMINY_PR_RESULT"]})

        applied = self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "review-lane",
            "--current-node", "merge", "--event", "JIMINY_PR_RESULT",
            "--context-json", context, "--runner-session-id", "jiminy-2",
        )

        result = json.loads(applied.stdout)
        self.assertEqual(result["state"]["node"], "merge")
        self.assertEqual(result["state"]["jiminy_runner_session_id"], "jiminy-2")

    def test_graph_transition_rejects_merge_result_for_unreviewed_head(self) -> None:
        self.register("gepetto")
        self.register(
            "jiminy", "--coordinator-thread-id", "session-1", session_id="jiminy-1"
        )
        self.state_command(
            "ledger", "set", "--session-id", "session-1", "--lane", "review-lane",
            "--json", f'{{"node":"merge","head_sha":"{SHA}","jiminy_runner_session_id":"jiminy-1"}}',
        )
        packet = valid_packets()["JIMINY_PR_RESULT"]
        packet["reviewed_head_sha"] = "b" * 40
        context = json.dumps({
            "packet": packet,
            "persisted": {"head_sha": "b" * 40},
        })

        blocked = self.state_command(
            "graph", "apply", "--session-id", "session-1", "--lane", "review-lane",
            "--current-node", "merge", "--event", "JIMINY_PR_RESULT",
            "--context-json", context, "--runner-session-id", "jiminy-1", check=False,
        )

        self.assertEqual(blocked.returncode, 1)
        self.assertIn("found 0", blocked.stderr)
        self.assertEqual(self.session_state()["ledger"]["review-lane"]["head_sha"], SHA)

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
