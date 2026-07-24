import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from voice_state import (
    HeadDriftError,
    StateError,
    create_task,
    delivery_request,
    load_task,
    mutate_task,
    observe_pr_head,
    requested_delivery_effect,
    validate_lanes,
    work_class,
)


HEAD = "a" * 40


def contract():
    return {
        "intent": "fix",
        "scope": ["x"],
        "non_scope": ["y"],
        "repo": "owner/repo",
        "owner": "coordinator",
        "branch": "feature",
        "acceptance": ["test"],
        "actors": {
            "coordinator": ["coordinator"],
            "user": ["user"],
            "implement": ["writer", "successor"],
            "review_gate": ["reviewer"],
        },
    }


def merge_payload(pr="42"):
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": f"gh pr merge {pr} --repo owner/repo"},
    }


def create_reviewed_task(path):
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
    return mutate_task(
        path,
        "task-1",
        3,
        "review",
        actor="reviewer",
        head=HEAD,
        passed=True,
    )


def grant_delivery(path, revision=4, payload=None):
    request = delivery_request(payload or merge_payload())
    return mutate_task(
        path,
        "task-1",
        revision,
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


class VoiceStateTests(unittest.TestCase):
    @patch("voice_state.subprocess.run")
    def test_default_head_observer_queries_github(self, run):
        run.return_value.stdout = f'{"c" * 40}\n'
        self.assertEqual(observe_pr_head("owner/repo", "42"), "c" * 40)
        run.assert_called_once_with(
            [
                "gh",
                "pr",
                "view",
                "42",
                "--repo",
                "owner/repo",
                "--json",
                "headRefOid",
                "--jq",
                ".headRefOid",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_contract_requires_separate_registered_roles_and_decision_owner(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice-state.json"
            invalid = contract()
            invalid["actors"]["review_gate"] = ["writer"]
            with self.assertRaisesRegex(StateError, "must be independent"):
                create_task(path, "task-1", invalid)

            invalid = contract()
            invalid["actors"]["coordinator"] = ["other"]
            with self.assertRaisesRegex(StateError, "task owner"):
                create_task(path, "task-1", invalid)

    def test_claim_requires_registered_writer_approved_branch_and_current_revision(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice-state.json"
            create_task(path, "task-1", contract())
            with self.assertRaisesRegex(StateError, "not registered"):
                mutate_task(
                    path,
                    "task-1",
                    1,
                    "claim",
                    actor="intruder",
                    branch="feature",
                    worktree="intruder-tree",
                )
            with self.assertRaisesRegex(StateError, "approved contract"):
                mutate_task(
                    path,
                    "task-1",
                    1,
                    "claim",
                    actor="writer",
                    branch="other",
                    worktree="writer-tree",
                )
            claimed = mutate_task(
                path,
                "task-1",
                1,
                "claim",
                actor="writer",
                branch="feature",
                worktree="writer-tree",
            )
            self.assertEqual(claimed["revision"], 2)
            with self.assertRaisesRegex(StateError, "revision conflict"):
                mutate_task(
                    path,
                    "task-1",
                    1,
                    "claim",
                    actor="successor",
                    branch="feature",
                    worktree="successor-tree",
                )
            self.assertEqual(load_task(path, "task-1")["writer"]["actor"], "writer")

    def test_only_registered_independent_reviewer_can_review_current_head(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice-state.json"
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
            with self.assertRaisesRegex(StateError, "not registered"):
                mutate_task(
                    path,
                    "task-1",
                    3,
                    "review",
                    actor="writer",
                    head=HEAD,
                    passed=True,
                )
            with self.assertRaisesRegex(StateError, "current implemented head"):
                mutate_task(
                    path,
                    "task-1",
                    3,
                    "review",
                    actor="reviewer",
                    head="b" * 40,
                    passed=True,
                )
            reviewed = mutate_task(
                path,
                "task-1",
                3,
                "review",
                actor="reviewer",
                head=HEAD,
                passed=True,
            )
            self.assertEqual(reviewed["state"], "review_passed")

    def test_implement_actor_cannot_grant_delivery(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice-state.json"
            create_reviewed_task(path)
            request = delivery_request(merge_payload())
            with self.assertRaisesRegex(StateError, "cannot grant"):
                mutate_task(
                    path,
                    "task-1",
                    4,
                    "grant-delivery",
                    actor="writer",
                    origin="coordinator",
                    effect="merge",
                    repo="owner/repo",
                    task_id="task-1",
                    pr="42",
                    head=HEAD,
                    request_digest=request["digest"],
                )
            self.assertEqual(load_task(path, "task-1")["revision"], 4)

    def test_unregistered_decision_actor_cannot_grant_delivery(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice-state.json"
            create_reviewed_task(path)
            request = delivery_request(merge_payload())
            with self.assertRaisesRegex(StateError, "not registered"):
                mutate_task(
                    path,
                    "task-1",
                    4,
                    "grant-delivery",
                    actor="intruder",
                    origin="coordinator",
                    effect="merge",
                    repo="owner/repo",
                    task_id="task-1",
                    pr="42",
                    head=HEAD,
                    request_digest=request["digest"],
                )

    def test_delivery_grant_is_exact_action_exact_head_and_one_shot(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice-state.json"
            create_reviewed_task(path)
            granted = grant_delivery(path)
            grant_id = granted["authority"]["id"]
            request = delivery_request(merge_payload())
            observed = []
            consumed = mutate_task(
                path,
                "task-1",
                5,
                "consume-delivery",
                grant_id=grant_id,
                effect=request["effect"],
                request_digest=request["digest"],
                observe_head=lambda repo, pr: observed.append((repo, pr)) or HEAD,
            )
            self.assertEqual(observed, [("owner/repo", "42")])
            self.assertEqual(consumed["authority"]["status"], "consumed")
            self.assertEqual(consumed["state"], "delivery_started")
            with self.assertRaisesRegex(StateError, "one-shot"):
                mutate_task(
                    path,
                    "task-1",
                    6,
                    "consume-delivery",
                    grant_id=grant_id,
                    effect=request["effect"],
                    request_digest=request["digest"],
                    observe_head=lambda _repo, _pr: HEAD,
                )

    def test_different_action_cannot_consume_grant_or_trigger_head_observation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice-state.json"
            create_reviewed_task(path)
            granted = grant_delivery(path)
            observations = []
            different = delivery_request(merge_payload("99"))
            with self.assertRaisesRegex(StateError, "exact action"):
                mutate_task(
                    path,
                    "task-1",
                    5,
                    "consume-delivery",
                    grant_id=granted["authority"]["id"],
                    effect=different["effect"],
                    request_digest=different["digest"],
                    observe_head=lambda repo, pr: observations.append((repo, pr)) or HEAD,
                )
            self.assertEqual(observations, [])
            self.assertEqual(load_task(path, "task-1")["authority"]["status"], "active")

    def test_head_drift_atomically_invalidates_review_and_authority(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice-state.json"
            create_reviewed_task(path)
            granted = grant_delivery(path)
            request = delivery_request(merge_payload())
            with self.assertRaisesRegex(HeadDriftError, "live PR head"):
                mutate_task(
                    path,
                    "task-1",
                    5,
                    "consume-delivery",
                    grant_id=granted["authority"]["id"],
                    effect=request["effect"],
                    request_digest=request["digest"],
                    observe_head=lambda _repo, _pr: "b" * 40,
                )
            recovered = load_task(path, "task-1")
            self.assertEqual(recovered["revision"], 6)
            self.assertEqual(recovered["state"], "implementing")
            self.assertIsNone(recovered["review"])
            self.assertIsNone(recovered["authority"])
            self.assertEqual(recovered["authority_history"][-1]["status"], "invalidated")

    def test_revoked_or_superseded_grant_cannot_be_consumed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice-state.json"
            create_reviewed_task(path)
            first = grant_delivery(path)
            first_id = first["authority"]["id"]
            revoked = mutate_task(
                path,
                "task-1",
                5,
                "revoke-delivery",
                actor="coordinator",
                grant_id=first_id,
            )
            request = delivery_request(merge_payload())
            with self.assertRaisesRegex(StateError, "one-shot"):
                mutate_task(
                    path,
                    "task-1",
                    6,
                    "consume-delivery",
                    grant_id=first_id,
                    effect=request["effect"],
                    request_digest=request["digest"],
                    observe_head=lambda _repo, _pr: HEAD,
                )
            self.assertEqual(revoked["authority"]["status"], "revoked")

            second = grant_delivery(path, revision=6)
            third = grant_delivery(path, revision=7)
            self.assertEqual(third["authority_history"][-1]["status"], "superseded")
            with self.assertRaisesRegex(StateError, "one-shot"):
                mutate_task(
                    path,
                    "task-1",
                    8,
                    "consume-delivery",
                    grant_id=second["authority"]["id"],
                    effect=request["effect"],
                    request_digest=request["digest"],
                    observe_head=lambda _repo, _pr: HEAD,
                )

    def test_checkpoint_recovers_and_transfers_single_writer_ownership(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice-state.json"
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
            with self.assertRaisesRegex(StateError, "registered implement successor"):
                mutate_task(
                    path,
                    "task-1",
                    2,
                    "checkpoint",
                    actor="writer",
                    successor="intruder",
                    next_action="continue focused tests",
                )
            mutate_task(
                path,
                "task-1",
                2,
                "checkpoint",
                actor="writer",
                successor="successor",
                next_action="continue focused tests",
            )

            recovered = load_task(path, "task-1")
            self.assertEqual(recovered["checkpoint"]["status"], "pending")
            self.assertEqual(recovered["writer"]["actor"], "writer")
            resumed = mutate_task(
                path,
                "task-1",
                3,
                "resume-checkpoint",
                actor="successor",
            )
            self.assertEqual(resumed["revision"], 4)
            self.assertEqual(resumed["checkpoint"]["status"], "resumed")
            self.assertEqual(
                resumed["checkpoint"]["previous_writer"]["actor"], "writer"
            )
            self.assertEqual(resumed["writer"]["actor"], "successor")
            self.assertEqual(resumed["writer"]["worktree"], "writer-tree")

    def test_store_compare_and_swap_rejects_stale_mutation_without_partial_state(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice-state.json"
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
            with self.assertRaisesRegex(StateError, "revision conflict"):
                mutate_task(
                    path,
                    "task-1",
                    1,
                    "checkpoint",
                    actor="writer",
                    successor="successor",
                    next_action="continue",
                )
            recovered = load_task(path, "task-1")
            self.assertEqual(recovered["revision"], 2)
            self.assertNotIn("checkpoint", recovered)

    def test_ordinary_bypasses_state_and_complex_requires_approved_lanes(self):
        self.assertEqual(work_class(False, False, False, False), "ordinary")
        self.assertEqual(work_class(True, False, False, False), "durable")
        self.assertEqual(work_class(False, True, False, False), "complex")
        with self.assertRaises(StateError):
            validate_lanes([{"id": "a", "domain": "src"}], False)
        with self.assertRaises(StateError):
            validate_lanes(
                [
                    {"id": "a", "domain": "src"},
                    {"id": "b", "domain": "src"},
                ],
                True,
            )
        validate_lanes(
            [{"id": "a", "domain": "src"}, {"id": "b", "domain": "docs"}],
            True,
        )

    def test_delivery_detection_covers_explicit_gh_global_repo_forms(self):
        for command in [
            "gh pr merge 42 --repo owner/repo",
            "gh --repo owner/repo pr merge 42",
            "gh -R owner/repo pr merge 42",
            "/usr/local/bin/gh pr merge 42 --repo owner/repo",
        ]:
            with self.subTest(command=command):
                self.assertEqual(
                    requested_delivery_effect(
                        {
                            "hook_event_name": "PreToolUse",
                            "tool_name": "Bash",
                            "tool_input": {"command": command},
                        }
                    ),
                    "merge",
                )


if __name__ == "__main__":
    unittest.main()
