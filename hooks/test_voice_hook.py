import json
import re
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
HEAD = "a" * 40
VOICE_STATE_PATH = Path(__file__).with_name("voice_state.py").resolve()


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
        "commands": {
            "implement": [
                "npm test",
                "git diff --check",
                "git push origin feature",
            ]
        },
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


def namespaced_exec(command):
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "functions__exec_command",
        "tool_input": {"cmd": command},
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
            bash(r"g\h pr merge 42 --repo owner/repo"),
            bash("'g''h' pr merge 42 --repo owner/repo"),
            bash('a=g; b=h; "$a$b" pr merge 42 --repo owner/repo'),
            bash(
                "python3 -c 'import subprocess as s;"
                "s.run([chr(103)+chr(104),chr(112)+chr(114),"
                "chr(109)+chr(101)+chr(114)+chr(103)+chr(101)])'"
            ),
            bash(r"npm test && g\h pr merge 42 --repo owner/repo"),
            bash('branch=main; git push origin "HEAD:$branch"'),
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
            namespaced_exec("gh pr merge 42 --repo owner/repo"),
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(StateError):
                    handle_hook(payload)

    def test_ordinary_bash_is_literal_read_only(self):
        handle_hook(bash("git status --short"))
        handle_hook(bash("gh pr view 42 --repo owner/repo"))
        for command in [
            "git push origin feature",
            "npm test",
            "git status; gh pr merge 42",
            "rg --pre malicious pattern .",
            "git diff --ext-diff",
        ]:
            with self.subTest(command=command):
                with self.assertRaises(StateError):
                    handle_hook(bash(command))

    def test_typed_delivery_hook_requires_the_granting_decision_actor(self):
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
            mutate_task(
                path,
                "task-1",
                2,
                "implemented",
                credential=TOKENS["writer"],
                actor="writer",
                head=HEAD,
                checks=["npm test"],
            )
            mutate_task(
                path,
                "task-1",
                3,
                "review",
                credential=TOKENS["reviewer"],
                actor="reviewer",
                head=HEAD,
                passed=True,
            )
            mutate_task(
                path,
                "task-1",
                4,
                "grant-delivery",
                credential=TOKENS["coordinator"],
                actor="coordinator",
                origin="coordinator",
                head=HEAD,
                action={
                    "kind": "github_merge",
                    "repo": "owner/repo",
                    "pr": "42",
                    "method": "squash",
                },
            )
            command = bash(
                f"python3 {VOICE_STATE_PATH} deliver < payload.json"
            )
            base_environment = {
                "CODEX_ORCHESTRATION_STATE": str(path),
                "CODEX_ORCHESTRATION_TASK": "task-1",
                "CODEX_ORCHESTRATION_WORKTREE": directory,
                "PWD": directory,
            }
            with self.assertRaisesRegex(StateError, "durable task context"):
                handle_hook(command, {})
            with self.assertRaisesRegex(StateError, "decision actor"):
                handle_hook(
                    command,
                    {
                        **base_environment,
                        "CODEX_ORCHESTRATION_ACTOR": "reviewer",
                        "CODEX_ORCHESTRATION_CREDENTIAL": TOKENS["reviewer"],
                    },
                )
            with self.assertRaisesRegex(StateError, "durable task context"):
                handle_hook(
                    bash(
                        "python3 /installed/$(echo>x)voice_state.py "
                        "transition < payload.json"
                    ),
                    {},
                )
            handle_hook(
                command,
                {
                    **base_environment,
                    "CODEX_ORCHESTRATION_ACTOR": "coordinator",
                    "CODEX_ORCHESTRATION_CREDENTIAL": TOKENS["coordinator"],
                },
            )

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
            handle_hook(namespaced_exec("npm test"), writer_environment)
            handle_hook(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "functions__apply_patch",
                    "tool_input": {},
                },
                writer_environment,
            )
            with self.assertRaisesRegex(StateError, "exact contract-approved"):
                handle_hook(bash("npm test && git diff --check"), writer_environment)
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
            handle_hook(
                bash(
                    f"python3 {VOICE_STATE_PATH} "
                    "transition < payload.json"
                ),
                reviewer_environment,
            )
            for alternate_path in (
                Path(directory) / "voice_state.py",
                Path(directory) / "safe" / ".." / "voice_state.py",
                Path("/installed/voice_state.py"),
            ):
                with self.subTest(alternate_path=alternate_path):
                    with self.assertRaisesRegex(StateError, "cannot use Bash"):
                        handle_hook(
                            bash(
                                f"python3 {alternate_path} "
                                "transition < payload.json"
                            ),
                            reviewer_environment,
                        )
            with self.assertRaisesRegex(StateError, "cannot use Bash"):
                handle_hook(bash("npm test"), reviewer_environment)
            with self.assertRaisesRegex(StateError, "cannot use Bash"):
                handle_hook(bash("git diff --check"), reviewer_environment)
            with self.assertRaisesRegex(StateError, "cannot use Bash"):
                handle_hook(
                    namespaced_exec("git diff --check"),
                    reviewer_environment,
                )
            with self.assertRaisesRegex(StateError, "cannot use Bash"):
                handle_hook(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": "mcp__filesystem__write",
                        "tool_input": {},
                    },
                    reviewer_environment,
                )
            with self.assertRaisesRegex(StateError, "cannot use Bash"):
                handle_hook(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": "mcp__filesystem__edit",
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

    def test_installed_matcher_covers_namespaced_write_tools(self):
        matcher = json.loads(Path(__file__).with_name("hooks.json").read_text())[
            "hooks"
        ]["PreToolUse"][0]["matcher"]
        for tool_name in [
            "Bash",
            "apply_patch",
            "functions__apply_patch",
            "mcp__filesystem__write",
            "mcp__filesystem__write_file",
            "mcp__filesystem__edit",
            "functions__exec_command",
            "mcp__shell__exec_command",
            "mcp__github__merge_pull_request",
        ]:
            with self.subTest(tool_name=tool_name):
                self.assertIsNotNone(re.fullmatch(matcher, tool_name))


if __name__ == "__main__":
    unittest.main()
