import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from voice_state import StateError, create_task, handle_hook, mutate_task


TOKENS = {
    "coordinator": "coordinator-secret",
    "writer": "writer-secret",
    "successor": "successor-secret",
    "reviewer": "reviewer-secret",
}


def contract():
    return {
        "intent": "fix",
        "scope": ["PR 42"],
        "non_scope": ["other PRs"],
        "repo": "owner/repo",
        "pr": "42",
        "owner": "coordinator",
        "branch": "feature",
        "acceptance": ["tests"],
        "actors": {
            "coordinator": ["coordinator"],
            "implement": ["writer", "successor"],
            "review_gate": ["reviewer"],
        },
    }


def bash(command):
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


class VoiceHookTests(unittest.TestCase):
    def test_direct_delivery_forms_are_blocked_in_favor_of_typed_runner(self):
        payloads = [
            bash("gh pr merge 42 --repo owner/repo"),
            bash("gh api --method PUT repos/owner/repo/pulls/42/merge"),
            bash("pr=42; gh api --method PUT repos/owner/repo/pulls/$pr/merge"),
            bash("gh api graphql -f query='mutation { mergePullRequest(input: {}) }'"),
            bash("bash -lc 'gh pr merge 42 --repo owner/repo'"),
            bash('"gh" pr merge 42 --repo owner/repo'),
            bash("git push origin HEAD:main"),
            bash("git -C . push origin main"),
            bash("git push origin +main"),
            bash('python3 -c \'import subprocess; subprocess.run(["gh","pr","merge","42"])\''),
            bash("bash -lc 'npm publish'"),
            bash("gh pr merge 42 --repo owner/repo && gh issue close 9"),
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "mcp__github__merge_pull_request",
                "tool_input": {
                    "owner": "owner",
                    "repo": "repo",
                    "pull_number": 42,
                },
            },
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "mcp__codex_apps__github__close_issue",
                "tool_input": {"repo": "owner/repo", "issue_number": 9},
            },
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(StateError, "typed voice_state.py deliver"):
                    handle_hook(payload)

    def test_safe_branch_push_is_not_blocked(self):
        handle_hook(bash("git push origin feat/voice-first-control-plane"))

    def test_durable_write_tools_require_the_active_writer_lease(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            create_task(path, "task-1", contract(), TOKENS)
            mutate_task(
                path,
                "task-1",
                1,
                "claim",
                credential=TOKENS["writer"],
                actor="writer",
                branch="feature",
                worktree=directory,
            )
            writer_environment = {
                "CODEX_ORCHESTRATION_STATE": str(path),
                "CODEX_ORCHESTRATION_TASK": "task-1",
                "CODEX_ORCHESTRATION_ACTOR": "writer",
                "CODEX_ORCHESTRATION_CREDENTIAL": TOKENS["writer"],
                "CODEX_ORCHESTRATION_WORKTREE": directory,
                "PWD": directory,
            }
            handle_hook(bash("npm test"), writer_environment)
            with self.assertRaisesRegex(StateError, "worktree"):
                handle_hook(
                    bash("npm test"),
                    {**writer_environment, "PWD": str(Path(directory) / "other")},
                )

            reviewer_environment = {
                **writer_environment,
                "CODEX_ORCHESTRATION_ACTOR": "reviewer",
                "CODEX_ORCHESTRATION_CREDENTIAL": TOKENS["reviewer"],
            }
            handle_hook(bash("npm test && git diff --check"), reviewer_environment)
            with self.assertRaisesRegex(StateError, "read-only"):
                handle_hook(bash("echo changed > tracked.txt"), reviewer_environment)
            with self.assertRaisesRegex(StateError, "read-only"):
                handle_hook(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": "apply_patch",
                        "tool_input": {},
                    },
                    reviewer_environment,
                )

            mutate_task(
                path,
                "task-1",
                2,
                "checkpoint",
                credential=TOKENS["writer"],
                actor="writer",
                successor="successor",
                next_action="continue",
            )
            with self.assertRaisesRegex(StateError, "writer lease"):
                handle_hook(bash("npm test"), writer_environment)


if __name__ == "__main__":
    unittest.main()
