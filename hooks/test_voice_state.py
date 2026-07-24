import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from voice_state import (
    HeadDriftError,
    StateError,
    create_task,
    load_task,
    mutate_task,
    observe_pr_head,
    run_delivery,
    validate_lanes,
    work_class,
)


HEAD = "a" * 40
TOKENS = {
    "coordinator": "coordinator-secret",
    "user": "user-secret",
    "writer": "writer-secret",
    "successor": "successor-secret",
    "reviewer": "reviewer-secret",
}


def contract():
    return {
        "intent": "fix",
        "scope": ["x"],
        "non_scope": ["y"],
        "repo": "owner/repo",
        "pr": "42",
        "owner": "coordinator",
        "branch": "feature",
        "acceptance": ["test"],
        "commands": {
            "implement": [
                "npm test",
                "git diff --check",
                "git push origin feature",
            ]
        },
        "actors": {
            "coordinator": ["coordinator"],
            "user": ["user"],
            "implement": ["writer", "successor"],
            "review_gate": ["reviewer"],
        },
    }


def action(pr="42", repo="owner/repo"):
    return {
        "kind": "github_merge",
        "repo": repo,
        "pr": pr,
        "method": "squash",
    }


def create(path):
    return create_task(path, "task-1", contract(), TOKENS)


def transition(path, revision, operation, **arguments):
    actor = arguments.get("actor")
    return mutate_task(
        path,
        "task-1",
        revision,
        operation,
        credential=TOKENS.get(actor, ""),
        **arguments,
    )


def create_reviewed_task(path):
    create(path)
    transition(
        path,
        1,
        "claim",
        actor="writer",
        branch="feature",
        worktree="writer-tree",
    )
    transition(
        path,
        2,
        "implemented",
        actor="writer",
        head=HEAD,
        checks=["npm test"],
    )
    return transition(
        path,
        3,
        "review",
        actor="reviewer",
        head=HEAD,
        passed=True,
    )


def grant(path, revision=4, merge_action=None):
    return transition(
        path,
        revision,
        "grant-delivery",
        actor="coordinator",
        origin="coordinator",
        head=HEAD,
        action=merge_action or action(),
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

    def test_contract_requires_independent_roles_and_credentials(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            invalid = contract()
            invalid["actors"]["review_gate"] = ["writer"]
            with self.assertRaisesRegex(StateError, "must be independent"):
                create_task(path, "task-1", invalid, TOKENS)

            invalid = contract()
            invalid["actors"]["review_gate"] = ["coordinator"]
            with self.assertRaisesRegex(StateError, "must remain read-only"):
                create_task(path, "task-1", invalid, TOKENS)

            with self.assertRaisesRegex(StateError, "every registered actor"):
                create_task(path, "task-1", contract(), {"writer": "secret"})

            for command in [
                r"g\h pr merge 42",
                "npm test && gh pr merge 42",
                "gh api repos/owner/repo/pulls/42/merge",
                "git push origin main",
                "bash deploy.sh",
            ]:
                invalid = contract()
                invalid["commands"]["implement"] = [command]
                with self.subTest(command=command):
                    with self.assertRaisesRegex(
                        StateError,
                        "composition|scoped command policy",
                    ):
                        create_task(path, f"invalid-{len(command)}", invalid, TOKENS)

    def test_actor_credential_prevents_writer_from_impersonating_coordinator(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            create_reviewed_task(path)
            with self.assertRaisesRegex(StateError, "credential is invalid"):
                mutate_task(
                    path,
                    "task-1",
                    4,
                    "grant-delivery",
                    credential=TOKENS["writer"],
                    actor="coordinator",
                    origin="coordinator",
                    head=HEAD,
                    action=action(),
                )
            self.assertIsNone(load_task(path, "task-1")["authority"])

    def test_claim_requires_registered_writer_approved_branch_and_current_revision(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            create(path)
            with self.assertRaisesRegex(StateError, "approved contract"):
                transition(
                    path,
                    1,
                    "claim",
                    actor="writer",
                    branch="other",
                    worktree="writer-tree",
                )
            claimed = transition(
                path,
                1,
                "claim",
                actor="writer",
                branch="feature",
                worktree="writer-tree",
            )
            self.assertEqual(claimed["revision"], 2)
            with self.assertRaisesRegex(StateError, "revision conflict"):
                transition(
                    path,
                    1,
                    "checkpoint",
                    actor="writer",
                    successor="successor",
                    next_action="continue",
                )

    def test_only_credentialed_independent_reviewer_can_review_current_head(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            create(path)
            transition(
                path,
                1,
                "claim",
                actor="writer",
                branch="feature",
                worktree="writer-tree",
            )
            transition(
                path,
                2,
                "implemented",
                actor="writer",
                head=HEAD,
                checks=["npm test"],
            )
            with self.assertRaisesRegex(StateError, "review_gate"):
                transition(
                    path,
                    3,
                    "review",
                    actor="writer",
                    head=HEAD,
                    passed=True,
                )
            reviewed = transition(
                path,
                3,
                "review",
                actor="reviewer",
                head=HEAD,
                passed=True,
            )
            self.assertEqual(reviewed["state"], "review_passed")

    def test_typed_grant_cannot_target_a_different_repo(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            create_reviewed_task(path)
            with self.assertRaisesRegex(StateError, "outside the approved task scope"):
                grant(path, merge_action=action("99", "attacker/other"))
            self.assertIsNone(load_task(path, "task-1")["authority"])

    def test_typed_grant_cannot_target_a_different_pr_in_the_same_repo(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            create_reviewed_task(path)
            with self.assertRaisesRegex(StateError, "outside the approved task scope"):
                grant(path, merge_action=action("99", "owner/repo"))
            self.assertIsNone(load_task(path, "task-1")["authority"])

    def test_typed_delivery_refreshes_head_and_matches_it_in_merge_command(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            create_reviewed_task(path)
            granted = grant(path)
            observed = []
            commands = []

            result = run_delivery(
                path,
                "task-1",
                5,
                "coordinator",
                TOKENS["coordinator"],
                granted["authority"]["id"],
                action(),
                observe_head=lambda repo, pr: observed.append((repo, pr)) or HEAD,
                run_command=lambda command, check: commands.append((command, check))
                or SimpleNamespace(returncode=0),
            )

            self.assertEqual(observed, [("owner/repo", "42")])
            self.assertEqual(
                commands,
                [
                    (
                        [
                            "gh",
                            "pr",
                            "merge",
                            "42",
                            "--repo",
                            "owner/repo",
                            "--squash",
                            "--match-head-commit",
                            HEAD,
                        ],
                        False,
                    )
                ],
            )
            self.assertEqual(result["state"], "complete")
            self.assertEqual(result["authority"]["status"], "consumed")

    def test_typed_delivery_requires_the_exact_granting_decision_actor(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            create_reviewed_task(path)
            granted = grant(path)
            for actor in ["writer", "reviewer", "user"]:
                with self.subTest(actor=actor):
                    with self.assertRaisesRegex(
                        StateError,
                        "decision actor|credential",
                    ):
                        run_delivery(
                            path,
                            "task-1",
                            5,
                            actor,
                            TOKENS[actor],
                            granted["authority"]["id"],
                            action(),
                            observe_head=lambda _repo, _pr: self.fail(
                                "unauthenticated actor must not refresh the head"
                            ),
                            run_command=lambda _command, check: self.fail(
                                "unauthenticated actor must not run delivery"
                            ),
                        )
            self.assertEqual(load_task(path, "task-1")["revision"], 5)

    def test_failed_typed_delivery_is_persisted_and_not_replayed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            create_reviewed_task(path)
            granted = grant(path)
            with self.assertRaisesRegex(StateError, "failed with exit 1"):
                run_delivery(
                    path,
                    "task-1",
                    5,
                    "coordinator",
                    TOKENS["coordinator"],
                    granted["authority"]["id"],
                    action(),
                    observe_head=lambda _repo, _pr: HEAD,
                    run_command=lambda _command, check: SimpleNamespace(returncode=1),
                )
            failed = load_task(path, "task-1")
            self.assertEqual(failed["state"], "delivery_failed")
            with self.assertRaisesRegex(StateError, "one-shot"):
                run_delivery(
                    path,
                    "task-1",
                    failed["revision"],
                    "coordinator",
                    TOKENS["coordinator"],
                    granted["authority"]["id"],
                    action(),
                    observe_head=lambda _repo, _pr: HEAD,
                    run_command=lambda _command, check: SimpleNamespace(returncode=0),
                )

    def test_head_drift_atomically_invalidates_review_and_authority(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            create_reviewed_task(path)
            granted = grant(path)
            with self.assertRaisesRegex(HeadDriftError, "live PR head"):
                run_delivery(
                    path,
                    "task-1",
                    5,
                    "coordinator",
                    TOKENS["coordinator"],
                    granted["authority"]["id"],
                    action(),
                    observe_head=lambda _repo, _pr: "b" * 40,
                    run_command=lambda _command, check: self.fail(
                        "command must not run"
                    ),
                )
            recovered = load_task(path, "task-1")
            self.assertEqual(recovered["revision"], 6)
            self.assertEqual(recovered["state"], "implementing")
            self.assertIsNone(recovered["review"])
            self.assertIsNone(recovered["authority"])

    def test_checkpoint_freezes_outgoing_writer_then_transfers_ownership(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            create(path)
            transition(
                path,
                1,
                "claim",
                actor="writer",
                branch="feature",
                worktree="writer-tree",
            )
            checkpointed = transition(
                path,
                2,
                "checkpoint",
                actor="writer",
                successor="successor",
                next_action="continue focused tests",
            )
            self.assertIsNone(checkpointed["writer"])
            with self.assertRaisesRegex(StateError, "claimed writer"):
                transition(
                    path,
                    3,
                    "implemented",
                    actor="writer",
                    head=HEAD,
                    checks=[],
                )
            resumed = transition(
                path,
                3,
                "resume-checkpoint",
                actor="successor",
            )
            self.assertEqual(resumed["checkpoint"]["status"], "resumed")
            self.assertEqual(resumed["writer"]["actor"], "successor")

    def test_store_compare_and_swap_rejects_stale_mutation_without_partial_state(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            create(path)
            transition(
                path,
                1,
                "claim",
                actor="writer",
                branch="feature",
                worktree="writer-tree",
            )
            with self.assertRaisesRegex(StateError, "revision conflict"):
                transition(
                    path,
                    1,
                    "checkpoint",
                    actor="writer",
                    successor="successor",
                    next_action="continue",
                )
            recovered = load_task(path, "task-1")
            self.assertEqual(recovered["revision"], 2)
            self.assertNotIn("checkpoint", recovered)

    def test_lane_domains_reject_nested_overlap(self):
        with self.assertRaisesRegex(StateError, "ownership domains"):
            validate_lanes(
                [
                    {"id": "a", "domain": "src"},
                    {"id": "b", "domain": "src/api"},
                ],
                True,
            )
        validate_lanes(
            [{"id": "a", "domain": "src"}, {"id": "b", "domain": "docs"}],
            True,
        )
        for lanes in [
            [{"id": "root", "domain": "."}, {"id": "child", "domain": "src"}],
            [{"id": "a", "domain": "src/../docs"}, {"id": "b", "domain": "docs"}],
        ]:
            with self.subTest(lanes=lanes):
                with self.assertRaisesRegex(StateError, "ownership domains"):
                    validate_lanes(lanes, True)

    def test_lane_dependencies_are_known_and_acyclic(self):
        validated = validate_lanes(
            [
                {"id": "api", "domain": "src/api"},
                {
                    "id": "docs",
                    "domain": "docs",
                    "depends_on": ["api"],
                },
            ],
            True,
        )
        self.assertEqual(validated[1]["depends_on"], ["api"])
        for lanes in [
            [{"id": "a", "domain": "a", "depends_on": ["missing"]}],
            [
                {"id": "a", "domain": "a", "depends_on": ["b"]},
                {"id": "b", "domain": "b", "depends_on": ["a"]},
            ],
        ]:
            with self.subTest(lanes=lanes):
                with self.assertRaisesRegex(StateError, "dependencies"):
                    validate_lanes(lanes, True)

    def test_control_cli_can_create_and_transition_a_task(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            script = Path(__file__).with_name("voice_state.py")
            created = subprocess.run(
                [sys.executable, str(script), "create"],
                input=json.dumps(
                    {"state": str(path), "task": "task-1", "contract": contract()}
                ),
                text=True,
                capture_output=True,
                check=True,
            )
            provisioned = json.loads(created.stdout)
            claimed = subprocess.run(
                [sys.executable, str(script), "transition"],
                input=json.dumps(
                    {
                        "state": str(path),
                        "task": "task-1",
                        "revision": 1,
                        "operation": "claim",
                        "credential": provisioned["credentials"]["writer"],
                        "arguments": {
                            "actor": "writer",
                            "branch": "feature",
                            "worktree": "writer-tree",
                        },
                    }
                ),
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(claimed.stdout)["writer"]["actor"], "writer")
            stored = path.read_text()
            self.assertNotIn(provisioned["credentials"]["writer"], stored)

    def test_work_class_keeps_orchestration_opt_in(self):
        self.assertEqual(work_class(False, False, False, False), "ordinary")
        self.assertEqual(work_class(True, False, False, False), "durable")
        self.assertEqual(work_class(False, True, False, False), "complex")
        with self.assertRaises(StateError):
            validate_lanes([{"id": "a", "domain": "src"}], False)

    def test_classification_and_approved_lane_map_are_available_through_cli(self):
        script = Path(__file__).with_name("voice_state.py")
        classified = subprocess.run(
            [sys.executable, str(script), "classify"],
            input=json.dumps(
                {
                    "needs_branch": False,
                    "concurrent": True,
                    "external_effect": False,
                    "high_risk": False,
                }
            ),
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(json.loads(classified.stdout), {"class": "complex"})
        orchestrated = subprocess.run(
            [sys.executable, str(script), "orchestrate"],
            input=json.dumps(
                {
                    "approved": True,
                    "lanes": [
                        {"id": "api", "domain": "src/api"},
                        {
                            "id": "docs",
                            "domain": "docs",
                            "depends_on": ["api"],
                        },
                    ],
                }
            ),
            text=True,
            capture_output=True,
            check=True,
        )
        receipt = json.loads(orchestrated.stdout)
        self.assertEqual(receipt["status"], "validated")
        self.assertEqual(receipt["lanes"][1]["depends_on"], ["api"])


if __name__ == "__main__":
    unittest.main()
