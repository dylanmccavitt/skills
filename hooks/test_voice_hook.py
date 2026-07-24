import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from voice_state import (
    StateError,
    create_task,
    delivery_request,
    handle_hook,
    load_task,
    mutate_task,
)


HEAD = "a" * 40


def contract():
    return {
        "intent": "merge reviewed work",
        "scope": ["PR 42"],
        "non_scope": ["deploy"],
        "repo": "owner/repo",
        "owner": "coordinator",
        "branch": "feature",
        "acceptance": ["tests pass"],
        "actors": {
            "coordinator": ["coordinator"],
            "user": ["user"],
            "implement": ["writer"],
            "review_gate": ["reviewer"],
        },
    }


def payload(pr="42"):
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": f"gh pr merge {pr} --repo owner/repo"},
    }


def prepare_grant(path):
    create_task(path, "task-1", contract())
    mutate_task(
        path,
        "task-1",
        1,
        "claim",
        actor="writer",
        branch="feature",
        worktree="writer-tree",
    )
    mutate_task(
        path,
        "task-1",
        2,
        "implemented",
        actor="writer",
        head=HEAD,
        checks=["npm test"],
    )
    mutate_task(
        path,
        "task-1",
        3,
        "review",
        actor="reviewer",
        head=HEAD,
        passed=True,
    )
    request = delivery_request(payload())
    return mutate_task(
        path,
        "task-1",
        4,
        "grant-delivery",
        actor="coordinator",
        origin="coordinator",
        effect=request["effect"],
        repo="owner/repo",
        task_id="task-1",
        pr="42",
        head=HEAD,
        request_digest=request["digest"],
    )


class VoiceHookTests(unittest.TestCase):
    def test_direct_delivery_without_kernel_context_fails_closed(self):
        with self.assertRaisesRegex(StateError, "missing delivery gate context"):
            handle_hook(payload(), {}, lambda _repo, _pr: HEAD)

    def test_hook_consumes_exact_action_grant_after_live_head_refresh(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            granted = prepare_grant(path)
            observations = []
            environment = {
                "CODEX_ORCHESTRATION_STATE": str(path),
                "CODEX_ORCHESTRATION_TASK": "task-1",
                "CODEX_ORCHESTRATION_REVISION": "5",
                "CODEX_ORCHESTRATION_GRANT": granted["authority"]["id"],
            }

            handle_hook(
                payload(),
                environment,
                lambda repo, pr: observations.append((repo, pr)) or HEAD,
            )

            state = load_task(path, "task-1")
            self.assertEqual(observations, [("owner/repo", "42")])
            self.assertEqual(state["authority"]["status"], "consumed")
            self.assertEqual(state["state"], "delivery_started")
            self.assertEqual(state["revision"], 6)

    def test_hook_rejects_a_different_action_before_observing_head(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            granted = prepare_grant(path)
            observations = []
            environment = {
                "CODEX_ORCHESTRATION_STATE": str(path),
                "CODEX_ORCHESTRATION_TASK": "task-1",
                "CODEX_ORCHESTRATION_REVISION": "5",
                "CODEX_ORCHESTRATION_GRANT": granted["authority"]["id"],
            }

            with self.assertRaisesRegex(StateError, "exact action"):
                handle_hook(
                    payload("99"),
                    environment,
                    lambda repo, pr: observations.append((repo, pr)) or HEAD,
                )

            self.assertEqual(observations, [])
            state = load_task(path, "task-1")
            self.assertEqual(state["authority"]["status"], "active")
            self.assertEqual(state["revision"], 5)

    def test_hook_rejects_authority_for_a_different_external_effect(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            granted = prepare_grant(path)
            environment = {
                "CODEX_ORCHESTRATION_STATE": str(path),
                "CODEX_ORCHESTRATION_TASK": "task-1",
                "CODEX_ORCHESTRATION_REVISION": "5",
                "CODEX_ORCHESTRATION_GRANT": granted["authority"]["id"],
            }
            publish = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "npm publish"},
            }
            with self.assertRaisesRegex(StateError, "exact action"):
                handle_hook(publish, environment, lambda _repo, _pr: HEAD)


if __name__ == "__main__":
    unittest.main()
