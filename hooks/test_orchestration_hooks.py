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
SHA = "a" * 40


class OrchestrationHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, CODEX_ORCHESTRATION_STATE_DIR=self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def register(self, role: str, *extra: str) -> None:
        subprocess.run(
            ["python3", str(STATE), "register", "--session-id", "session-1", "--role", role, *extra],
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )

    def hook(self, event: str, **fields: object) -> dict[str, object] | None:
        result = subprocess.run(
            ["python3", str(HOOK)],
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

    def test_complete_disables_compaction_handoff(self) -> None:
        self.register("jiminy")
        subprocess.run(
            ["python3", str(STATE), "complete", "--session-id", "session-1"],
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
        self.assertIn("assigned finding", fixer["hookSpecificOutput"]["additionalContext"])
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
            ["python3", str(STATE), "continue", "--source-id", "session-1", "--successor-id", "session-2"],
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIsNone(self.hook("SessionStart", source="compact"))
        result = subprocess.run(
            ["python3", str(HOOK)],
            input=json.dumps({"session_id": "session-2", "hook_event_name": "SessionStart", "source": "compact"}),
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("review", result.stdout)

    def test_merge_requires_authorized_jiminy_and_bound_head(self) -> None:
        self.register("gepetto")
        denied = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": f"gh pr merge 1 --squash --match-head-commit {SHA}"})
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

        self.register("jiminy", "--merge-authorized")
        unbound = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "gh pr merge 1 --squash"})
        self.assertEqual(unbound["hookSpecificOutput"]["permissionDecision"], "deny")
        allowed = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": f"gh pr merge 1 --squash --match-head-commit {SHA}"})
        self.assertIsNone(allowed)


if __name__ == "__main__":
    unittest.main()
