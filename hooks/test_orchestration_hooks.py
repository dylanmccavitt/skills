#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


HOOK = Path(__file__).with_name("orchestration_hook.py")
STATE = Path(__file__).with_name("orchestration_state.py")
CONFIG = Path(__file__).with_name("hooks.json")
PYTHON = "python3"
HOOK_COMMAND = '/usr/bin/env python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_hook.py"'
SHA = "a" * 40


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
        ]
        self.assertTrue(commands)
        self.assertTrue(all(command == HOOK_COMMAND for command in commands))

    def register(self, role: str, *extra: str) -> None:
        subprocess.run(
            [PYTHON, str(STATE), "register", "--session-id", "session-1", "--role", role, *extra],
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )

    def hook(self, event: str, **fields: object) -> dict[str, object] | None:
        result = subprocess.run(
            [PYTHON, str(HOOK)],
            input=json.dumps({"session_id": "session-1", "hook_event_name": event, **fields}),
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

    def test_research_subagent_must_return_receipt_once(self) -> None:
        self.register("research")
        start = self.hook("SubagentStart", agent_id="agent-1", agent_type="default")
        context = start["hookSpecificOutput"]["additionalContext"]
        self.assertIn("code-read-only", context)
        self.assertIn("issue create/update remains allowed", context)
        self.assertIn("split, consolidate", context)
        stop = self.hook("SubagentStop", agent_id="agent-1", agent_type="default", last_assistant_message="done", stop_hook_active=False)
        self.assertEqual(stop["decision"], "block")
        retry = self.hook("SubagentStop", agent_id="agent-1", agent_type="default", last_assistant_message="done", stop_hook_active=True)
        self.assertEqual(retry, {})

    def test_review_nested_agent_is_a_fixer(self) -> None:
        self.register("review")
        self.hook("SubagentStart", agent_id="reviewer", agent_type="default")
        fixer = self.hook("SubagentStart", agent_id="fixer", agent_type="default")
        self.assertIn("assigned fixes", fixer["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.hook("SubagentStop", agent_id="fixer", last_assistant_message="fixed", stop_hook_active=False), {})

    def test_lane_stop_requires_packet(self) -> None:
        self.register("implementation")
        blocked = self.hook("Stop", last_assistant_message="implemented", stop_hook_active=False)
        self.assertEqual(blocked["decision"], "block")
        allowed = self.hook("Stop", last_assistant_message="IMPLEMENTATION_PACKET:\n  pr_url: x", stop_hook_active=False)
        self.assertEqual(allowed, {})
        self.assertIsNone(self.hook("SessionStart", source="compact"))

    def test_force_push_is_denied(self) -> None:
        self.register("implementation")
        result = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "git push --force-with-lease origin branch"})
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

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
        for role in ("gepetto", "jiminy", "research"):
            self.register(role)
            for tool in ("Edit", "Write"):
                result = self.hook("PreToolUse", tool_name=tool, tool_input={"file_path": "/tmp/x", "content": "y"})
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

    def test_status_lists_sessions(self) -> None:
        self.register("gepetto")
        self.state_command("register", "--session-id", "session-2", "--role", "review", "--coordinator-thread-id", "session-1")
        self.state_command("complete", "--session-id", "session-2")
        lines = self.state_command("status").stdout.splitlines()
        self.assertIn("session-1 role=gepetto active=true coordinator=-", lines)
        self.assertIn("session-2 role=review active=false coordinator=session-1", lines)

    def test_merge_requires_authorized_jiminy_and_bound_head(self) -> None:
        self.register("gepetto")
        denied = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": f"gh pr merge 1 --squash --match-head-commit {SHA}"})
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

        self.register("jiminy", "--merge-authorized")
        unbound = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "gh pr merge 1 --squash"})
        self.assertEqual(unbound["hookSpecificOutput"]["permissionDecision"], "deny")
        allowed = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": f"gh pr merge 1 --squash --match-head-commit {SHA}"})
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


if __name__ == "__main__":
    unittest.main()
