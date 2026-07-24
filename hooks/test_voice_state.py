import json
import os
import platform
import shutil
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
    digest,
    load_task,
    mutate_task,
    observe_pr_head,
    observe_pr_status,
    recover_delivery,
    run_painter_command,
    run_delivery,
    validate_lanes,
    work_class,
)


HEAD = "a" * 40
WRITER_TREE = str((Path.cwd() / "writer-tree").resolve())
TOKENS = {
    "coordinator": "coordinator-secret",
    "user": "user-secret",
    "writer": "writer-secret",
    "successor": "successor-secret",
    "reviewer": "reviewer-secret",
}


def contract(worktree=WRITER_TREE):
    return {
        "intent": "fix",
        "scope": ["x"],
        "non_scope": ["y"],
        "repo": "owner/repo",
        "pr": "42",
        "owner": "coordinator",
        "branch": "feature",
        "worktree": worktree,
        "write_roots": ["."],
        "acceptance": ["test"],
        "commands": {
            "painter": [
                "npm test",
                "git diff --check",
                "git push origin feature",
            ]
        },
        "actors": {
            "coordinator": ["coordinator"],
            "user": ["user"],
            "painter": ["writer", "successor"],
            "vigil": ["reviewer"],
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


def create_reviewed_task(path, worktree=WRITER_TREE):
    create_task(path, "task-1", contract(worktree), TOKENS)
    transition(
        path,
        1,
        "claim",
        actor="writer",
        branch="feature",
        worktree=worktree,
    )
    transition(
        path,
        2,
        "painted",
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


def fake_gh(directory):
    binary_root = Path(directory) / "bin"
    binary_root.mkdir()
    log = Path(directory) / "gh-calls.jsonl"
    status = Path(directory) / "gh-status"
    status.write_text("OPEN")
    binary = binary_root / "gh"
    binary.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

arguments = sys.argv[1:]
log = Path(os.environ["FAKE_GH_LOG"])
with log.open("a") as handle:
    handle.write(json.dumps(arguments) + "\\n")
head = os.environ["FAKE_GH_HEAD"]
status_path = Path(os.environ["FAKE_GH_STATUS"])
if arguments[:2] == ["pr", "view"]:
    if "--jq" in arguments:
        print(head)
    else:
        state = status_path.read_text().strip()
        print(json.dumps({
            "headRefOid": head,
            "state": state,
            "mergedAt": "2026-07-24T00:00:00Z" if state == "MERGED" else None,
        }))
    raise SystemExit(0)
if arguments[:2] == ["pr", "merge"]:
    exit_code = int(os.environ.get("FAKE_GH_MERGE_EXIT", "0"))
    if exit_code == 0:
        status_path.write_text("MERGED")
    raise SystemExit(exit_code)
raise SystemExit(64)
"""
    )
    binary.chmod(0o755)
    return binary_root, log, status


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

    @patch("voice_state.subprocess.run")
    def test_default_recovery_observer_queries_github(self, run):
        run.return_value.stdout = json.dumps(
            {
                "headRefOid": "c" * 40,
                "state": "MERGED",
                "mergedAt": "2026-07-24T00:00:00Z",
            }
        )
        self.assertEqual(
            observe_pr_status("owner/repo", "42"),
            {"head": "c" * 40, "state": "MERGED", "merged": True},
        )
        run.assert_called_once_with(
            [
                "gh",
                "pr",
                "view",
                "42",
                "--repo",
                "owner/repo",
                "--json",
                "headRefOid,state,mergedAt",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_contract_requires_independent_roles_and_credentials(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            retired = contract()
            retired["actors"]["implement"] = retired["actors"].pop("painter")
            retired["actors"]["review_gate"] = retired["actors"].pop("vigil")
            with self.assertRaisesRegex(StateError, "painter|vigil"):
                create_task(path, "retired-roles", retired, TOKENS)

            retired = contract()
            retired["commands"]["implement"] = retired["commands"].pop("painter")
            with self.assertRaisesRegex(StateError, "only Painter"):
                create_task(path, "retired-command", retired, TOKENS)

            invalid = contract()
            invalid["actors"]["vigil"] = ["writer"]
            with self.assertRaisesRegex(StateError, "must be independent"):
                create_task(path, "task-1", invalid, TOKENS)

            invalid = contract()
            invalid["actors"]["vigil"] = ["coordinator"]
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
                invalid["commands"]["painter"] = [command]
                with self.subTest(command=command):
                    with self.assertRaisesRegex(
                        StateError,
                        "composition|scoped command policy",
                    ):
                        create_task(path, f"invalid-{len(command)}", invalid, TOKENS)

    def test_actor_credential_prevents_writer_from_impersonating_coordinator(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            create_reviewed_task(path, directory)
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
                    worktree=WRITER_TREE,
                )
            claimed = transition(
                path,
                1,
                "claim",
                actor="writer",
                branch="feature",
                worktree=WRITER_TREE,
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
                worktree=WRITER_TREE,
            )
            transition(
                path,
                2,
                "painted",
                actor="writer",
                head=HEAD,
                checks=["npm test"],
            )
            with self.assertRaisesRegex(StateError, "vigil"):
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
            create_reviewed_task(path, directory)
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
                observe_status=lambda _repo, _pr: {
                    "head": HEAD,
                    "state": "MERGED",
                    "merged": True,
                },
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

    def test_ambiguous_delivery_failure_requires_recovery_and_is_not_replayed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            create_reviewed_task(path)
            granted = grant(path)
            with self.assertRaisesRegex(StateError, "recover-delivery is required"):
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
            self.assertEqual(failed["state"], "delivery_started")
            self.assertEqual(failed["delivery_attempt"]["status"], "prepared")
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
            recovered = recover_delivery(
                path,
                "task-1",
                failed["revision"],
                "coordinator",
                TOKENS["coordinator"],
                granted["authority"]["id"],
                observe_status=lambda _repo, _pr: {
                    "head": HEAD,
                    "state": "CLOSED",
                    "merged": False,
                },
                run_command=lambda _command, check: self.fail(
                    "closed recovery must not replay"
                ),
            )
            self.assertEqual(recovered["state"], "delivery_failed")

    def test_delivery_recovery_retries_prepared_action_after_pre_action_crash(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            create_reviewed_task(path)
            granted = grant(path)
            with self.assertRaisesRegex(RuntimeError, "crash before action"):
                run_delivery(
                    path,
                    "task-1",
                    5,
                    "coordinator",
                    TOKENS["coordinator"],
                    granted["authority"]["id"],
                    action(),
                    observe_head=lambda _repo, _pr: HEAD,
                    run_command=lambda _command, check: (
                        _ for _ in ()
                    ).throw(RuntimeError("crash before action")),
                )
            stranded = load_task(path, "task-1")
            self.assertEqual(stranded["state"], "delivery_started")
            self.assertEqual(stranded["delivery_attempt"]["status"], "prepared")
            commands = []
            observations = iter(
                [
                    {"head": HEAD, "state": "OPEN", "merged": False},
                    {"head": HEAD, "state": "MERGED", "merged": True},
                ]
            )
            recovered = recover_delivery(
                path,
                "task-1",
                stranded["revision"],
                "coordinator",
                TOKENS["coordinator"],
                granted["authority"]["id"],
                observe_status=lambda _repo, _pr: next(observations),
                run_command=lambda command, check: commands.append(command)
                or SimpleNamespace(returncode=0),
            )
            self.assertEqual(len(commands), 1)
            self.assertEqual(recovered["state"], "complete")
            self.assertEqual(
                recovered["delivery_attempt"]["status"],
                "completed",
            )

    def test_delivery_recovery_observes_merge_after_post_action_crash(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            create_reviewed_task(path)
            granted = grant(path)
            with self.assertRaisesRegex(RuntimeError, "crash after action"):
                run_delivery(
                    path,
                    "task-1",
                    5,
                    "coordinator",
                    TOKENS["coordinator"],
                    granted["authority"]["id"],
                    action(),
                    observe_head=lambda _repo, _pr: HEAD,
                    run_command=lambda _command, check: SimpleNamespace(
                        returncode=0
                    ),
                    after_command=lambda: (_ for _ in ()).throw(
                        RuntimeError("crash after action")
                    ),
                )
            stranded = load_task(path, "task-1")
            recovered = recover_delivery(
                path,
                "task-1",
                stranded["revision"],
                "coordinator",
                TOKENS["coordinator"],
                granted["authority"]["id"],
                observe_status=lambda _repo, _pr: {
                    "head": HEAD,
                    "state": "MERGED",
                    "merged": True,
                },
                run_command=lambda _command, check: self.fail(
                    "merged recovery must not replay the action"
                ),
            )
            self.assertEqual(recovered["state"], "complete")
            self.assertTrue(recovered["delivery_attempt"]["recovered"])

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
            self.assertEqual(recovered["state"], "painting")
            self.assertIsNone(recovered["review"])
            self.assertIsNone(recovered["authority"])

    def test_delivery_completion_requires_authenticated_recovery_observation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            create_reviewed_task(path)
            granted = grant(path)
            with self.assertRaisesRegex(StateError, "not yet observably merged"):
                run_delivery(
                    path,
                    "task-1",
                    5,
                    "coordinator",
                    TOKENS["coordinator"],
                    granted["authority"]["id"],
                    action(),
                    observe_head=lambda _repo, _pr: HEAD,
                    observe_status=lambda _repo, _pr: {
                        "head": HEAD,
                        "state": "OPEN",
                        "merged": False,
                    },
                    run_command=lambda _command, check: SimpleNamespace(
                        returncode=0
                    ),
                )
            stranded = load_task(path, "task-1")
            self.assertEqual(stranded["state"], "delivery_started")
            revision = stranded["revision"]
            with self.assertRaisesRegex(StateError, "unknown operation"):
                mutate_task(
                    path,
                    "task-1",
                    revision,
                    "finish-delivery",
                    grant_id=granted["authority"]["id"],
                    succeeded=True,
                    exit_code=0,
                )
            self.assertEqual(load_task(path, "task-1")["revision"], revision)
            recovered = recover_delivery(
                path,
                "task-1",
                revision,
                "coordinator",
                TOKENS["coordinator"],
                granted["authority"]["id"],
                observe_status=lambda _repo, _pr: {
                    "head": HEAD,
                    "state": "MERGED",
                    "merged": True,
                },
            )
            self.assertEqual(recovered["state"], "complete")

    def test_writer_reservations_are_registry_wide_for_branch_and_worktree(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            first_tree = Path(directory) / "first"
            second_tree = Path(directory) / "second"
            third_tree = Path(directory) / "third"
            for tree in (first_tree, second_tree, third_tree):
                tree.mkdir()
            create_task(path, "task-1", contract(str(first_tree)), TOKENS)
            second_contract = contract(str(second_tree))
            create_task(path, "task-2", second_contract, TOKENS)
            other_repo_contract = contract(str(first_tree))
            other_repo_contract["repo"] = "other/repo"
            other_repo_contract["branch"] = "other-feature"
            other_repo_contract["commands"]["painter"][-1] = (
                "git push origin other-feature"
            )
            create_task(path, "task-3", other_repo_contract, TOKENS)
            independent_contract = contract(str(third_tree))
            independent_contract["repo"] = "other/repo"
            independent_contract["branch"] = "independent"
            independent_contract["commands"]["painter"][-1] = (
                "git push origin independent"
            )
            create_task(path, "task-4", independent_contract, TOKENS)

            mutate_task(
                path,
                "task-1",
                1,
                "claim",
                credential=TOKENS["writer"],
                actor="writer",
                branch="feature",
                worktree=str(first_tree / "."),
            )
            for task_id, branch, worktree in [
                ("task-2", "feature", str(second_tree)),
                ("task-3", "other-feature", str(first_tree)),
            ]:
                with self.subTest(task=task_id):
                    with self.assertRaisesRegex(StateError, "reservation conflicts"):
                        mutate_task(
                            path,
                            task_id,
                            1,
                            "claim",
                            credential=TOKENS["writer"],
                            actor="writer",
                            branch=branch,
                            worktree=worktree,
                        )
            claimed = mutate_task(
                path,
                "task-4",
                1,
                "claim",
                credential=TOKENS["writer"],
                actor="writer",
                branch="independent",
                worktree=str(third_tree),
            )
            self.assertEqual(
                claimed["writer"]["worktree"],
                str(third_tree.resolve()),
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
            with self.assertRaisesRegex(StateError, "reservation conflicts"):
                mutate_task(
                    path,
                    "task-2",
                    1,
                    "claim",
                    credential=TOKENS["writer"],
                    actor="writer",
                    branch="feature",
                    worktree=str(second_tree),
                )

    def test_role_tampering_cannot_reuse_a_painter_credential(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            create(path)
            store = json.loads(path.read_text())
            task = store["tasks"]["task-1"]
            task["contract"]["actors"]["painter"].remove("writer")
            task["contract"]["actors"]["coordinator"].append("writer")
            task["contract_digest"] = digest(task["contract"])
            path.write_text(json.dumps(store))

            with self.assertRaisesRegex(StateError, "credential is invalid"):
                mutate_task(
                    path,
                    "task-1",
                    1,
                    "approve-lanes",
                    credential=TOKENS["writer"],
                    actor="writer",
                    origin="coordinator",
                    lanes=[{"id": "one", "domain": "src"}],
                )

    def test_mutable_wrapper_runs_without_network_or_registry_write_access(self):
        with TemporaryDirectory() as directory:
            worktree = Path(directory).resolve()
            path = worktree / "state.json"
            specification = contract(str(worktree))
            specification["commands"]["painter"] = ["python3 runner.py"]
            create_task(path, "task-1", specification, TOKENS)
            mutate_task(
                path,
                "task-1",
                1,
                "claim",
                credential=TOKENS["writer"],
                actor="writer",
                branch="feature",
                worktree=str(worktree),
            )
            (worktree / "runner.py").write_text(
                """import json
import socket
from pathlib import Path

result = {}
try:
    Path("state.json").write_text("tampered")
    result["state_write"] = "allowed"
except OSError:
    result["state_write"] = "denied"
try:
    connection = socket.create_connection(("github.com", 443), timeout=0.2)
    connection.close()
    result["network"] = "allowed"
except OSError:
    result["network"] = "denied"
Path("sandbox-result.json").write_text(json.dumps(result))
"""
            )
            if platform.system() == "Linux" and not shutil.which("bwrap"):
                with self.assertRaisesRegex(StateError, "requires bubblewrap"):
                    run_painter_command(
                        path,
                        "task-1",
                        "writer",
                        TOKENS["writer"],
                        "python3 runner.py",
                        str(worktree),
                    )
                return

            result = run_painter_command(
                path,
                "task-1",
                "writer",
                TOKENS["writer"],
                "python3 runner.py",
                str(worktree),
            )

            self.assertEqual(result["exit_code"], 0, result["stderr"])
            self.assertEqual(
                json.loads((worktree / "sandbox-result.json").read_text()),
                {"network": "denied", "state_write": "denied"},
            )
            self.assertEqual(load_task(path, "task-1")["revision"], 2)

    def test_branch_push_adapter_binds_repo_branch_head_and_disables_hooks(self):
        with TemporaryDirectory() as directory:
            worktree = Path(directory).resolve()
            path = worktree / "state.json"
            create_task(path, "task-1", contract(str(worktree)), TOKENS)
            mutate_task(
                path,
                "task-1",
                1,
                "claim",
                credential=TOKENS["writer"],
                actor="writer",
                branch="feature",
                worktree=str(worktree),
            )
            calls = []

            def run(command, **options):
                calls.append((command, options))
                if command[-5:] == [
                    "remote",
                    "get-url",
                    "--push",
                    "--all",
                    "origin",
                ]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout="git@github.com:owner/repo.git\n",
                        stderr="",
                    )
                if command[-2:] == ["rev-parse", "HEAD"]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=f"{HEAD}\n",
                        stderr="",
                    )
                return SimpleNamespace(returncode=0, stdout="ok", stderr="")

            result = run_painter_command(
                path,
                "task-1",
                "writer",
                TOKENS["writer"],
                "git push origin feature",
                str(worktree),
                run_command=run,
            )

            self.assertEqual(result["exit_code"], 0)
            push = calls[-1][0]
            self.assertIn("core.hooksPath=/dev/null", push)
            self.assertIn("credential.helper=", push)
            self.assertIn("protocol.ext.allow=never", push)
            self.assertEqual(
                push[-2:],
                [
                    "git@github.com:owner/repo.git",
                    f"{HEAD}:refs/heads/feature",
                ],
            )
            self.assertEqual(
                calls[-1][1]["env"]["GIT_CONFIG_GLOBAL"],
                "/dev/null",
            )
            self.assertNotIn(
                "CODEX_ORCHESTRATION_CREDENTIAL",
                calls[-1][1]["env"],
            )

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
                worktree=WRITER_TREE,
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
                    "painted",
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
                worktree=WRITER_TREE,
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
                    {
                        "state": str(path),
                        "task": "task-1",
                        "contract": contract(directory),
                    }
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
                            "worktree": directory,
                        },
                    }
                ),
                text=True,
                capture_output=True,
                check=True,
                cwd=directory,
                env={
                    **os.environ,
                    "CODEX_ORCHESTRATION_STATE": str(path),
                    "CODEX_ORCHESTRATION_TASK": "task-1",
                    "CODEX_ORCHESTRATION_ACTOR": "writer",
                    "CODEX_ORCHESTRATION_CREDENTIAL": provisioned[
                        "credentials"
                    ]["writer"],
                    "CODEX_ORCHESTRATION_WORKTREE": directory,
                },
            )
            self.assertEqual(json.loads(claimed.stdout)["writer"]["actor"], "writer")
            stored = path.read_text()
            self.assertNotIn(provisioned["credentials"]["writer"], stored)

    def test_run_cli_cannot_swap_the_ambient_task_registry(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            script = Path(__file__).with_name("voice_state.py")
            create_task(path, "task-1", contract(directory), TOKENS)
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
            payload = {
                "state": str(Path(directory) / "attacker-state.json"),
                "task": "task-1",
                "actor": "writer",
                "credential": TOKENS["writer"],
                "command": "git diff --check",
                "worktree": directory,
            }
            denied = subprocess.run(
                [sys.executable, str(script), "run"],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=False,
                cwd=directory,
                env={
                    **os.environ,
                    "CODEX_ORCHESTRATION_STATE": str(path),
                    "CODEX_ORCHESTRATION_TASK": "task-1",
                    "CODEX_ORCHESTRATION_ACTOR": "writer",
                    "CODEX_ORCHESTRATION_CREDENTIAL": TOKENS["writer"],
                    "CODEX_ORCHESTRATION_WORKTREE": directory,
                },
            )

            self.assertEqual(denied.returncode, 2)
            self.assertIn("does not match durable task context", denied.stderr)

    def test_deliver_cli_runs_the_exact_merge_and_observes_completion(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            script = Path(__file__).with_name("voice_state.py")
            create_reviewed_task(path, directory)
            granted = grant(path)
            binary_root, log, status = fake_gh(directory)
            environment = {
                **os.environ,
                "PATH": f"{binary_root}{os.pathsep}{os.environ['PATH']}",
                "FAKE_GH_LOG": str(log),
                "FAKE_GH_HEAD": HEAD,
                "FAKE_GH_STATUS": str(status),
                "CODEX_ORCHESTRATION_STATE": str(path),
                "CODEX_ORCHESTRATION_TASK": "task-1",
                "CODEX_ORCHESTRATION_ACTOR": "coordinator",
                "CODEX_ORCHESTRATION_CREDENTIAL": TOKENS["coordinator"],
                "CODEX_ORCHESTRATION_WORKTREE": directory,
            }
            payload = {
                "state": str(path),
                "task": "task-1",
                "revision": 5,
                "actor": "coordinator",
                "credential": TOKENS["coordinator"],
                "grant": granted["authority"]["id"],
                "action": action(),
            }

            delivered = subprocess.run(
                [sys.executable, str(script), "deliver"],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=False,
                env=environment,
                cwd=directory,
            )

            self.assertEqual(delivered.returncode, 0, delivered.stderr)
            self.assertEqual(json.loads(delivered.stdout)["state"], "complete")
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(
                calls,
                [
                    [
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
                    [
                        "pr",
                        "merge",
                        "42",
                        "--repo",
                        "owner/repo",
                        "--squash",
                        "--match-head-commit",
                        HEAD,
                    ],
                    [
                        "pr",
                        "view",
                        "42",
                        "--repo",
                        "owner/repo",
                        "--json",
                        "headRefOid,state,mergedAt",
                    ],
                ],
            )

    def test_recover_delivery_cli_replays_only_the_prepared_exact_action(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            script = Path(__file__).with_name("voice_state.py")
            create_reviewed_task(path, directory)
            granted = grant(path)
            binary_root, log, status = fake_gh(directory)
            environment = {
                **os.environ,
                "PATH": f"{binary_root}{os.pathsep}{os.environ['PATH']}",
                "FAKE_GH_LOG": str(log),
                "FAKE_GH_HEAD": HEAD,
                "FAKE_GH_STATUS": str(status),
                "FAKE_GH_MERGE_EXIT": "1",
                "CODEX_ORCHESTRATION_STATE": str(path),
                "CODEX_ORCHESTRATION_TASK": "task-1",
                "CODEX_ORCHESTRATION_ACTOR": "coordinator",
                "CODEX_ORCHESTRATION_CREDENTIAL": TOKENS["coordinator"],
                "CODEX_ORCHESTRATION_WORKTREE": directory,
            }
            payload = {
                "state": str(path),
                "task": "task-1",
                "revision": 5,
                "actor": "coordinator",
                "credential": TOKENS["coordinator"],
                "grant": granted["authority"]["id"],
                "action": action(),
            }
            failed = subprocess.run(
                [sys.executable, str(script), "deliver"],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=False,
                env=environment,
                cwd=directory,
            )
            self.assertEqual(failed.returncode, 2)
            self.assertIn("recover-delivery is required", failed.stderr)
            stranded = load_task(path, "task-1")
            self.assertEqual(stranded["delivery_attempt"]["status"], "prepared")

            recovery_payload = {
                "state": str(path),
                "task": "task-1",
                "revision": stranded["revision"],
                "actor": "coordinator",
                "credential": TOKENS["coordinator"],
                "grant": granted["authority"]["id"],
            }
            recovered = subprocess.run(
                [sys.executable, str(script), "recover-delivery"],
                input=json.dumps(recovery_payload),
                text=True,
                capture_output=True,
                check=False,
                env={**environment, "FAKE_GH_MERGE_EXIT": "0"},
                cwd=directory,
            )

            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(json.loads(recovered.stdout)["state"], "complete")
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            merge_calls = [call for call in calls if call[:2] == ["pr", "merge"]]
            self.assertEqual(len(merge_calls), 2)
            self.assertEqual(
                merge_calls[-1],
                [
                    "pr",
                    "merge",
                    "42",
                    "--repo",
                    "owner/repo",
                    "--squash",
                    "--match-head-commit",
                    HEAD,
                ],
            )

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
        lanes = [
            {"id": "api", "domain": "src/api"},
            {
                "id": "docs",
                "domain": "docs",
                "depends_on": ["api"],
            },
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            create(path)
            approved = transition(
                path,
                1,
                "approve-lanes",
                actor="coordinator",
                origin="coordinator",
                lanes=lanes,
            )
            self.assertEqual(approved["lane_map"]["actor"], "coordinator")
            payload = {
                "state": str(path),
                "task": "task-1",
                "actor": "coordinator",
                "credential": TOKENS["coordinator"],
                "lanes": lanes,
            }
            environment = {
                **os.environ,
                "CODEX_ORCHESTRATION_STATE": str(path),
                "CODEX_ORCHESTRATION_TASK": "task-1",
                "CODEX_ORCHESTRATION_ACTOR": "coordinator",
                "CODEX_ORCHESTRATION_CREDENTIAL": TOKENS["coordinator"],
                "CODEX_ORCHESTRATION_WORKTREE": directory,
            }
            orchestrated = subprocess.run(
                [sys.executable, str(script), "orchestrate"],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=True,
                env=environment,
                cwd=directory,
            )
            receipt = json.loads(orchestrated.stdout)
            self.assertEqual(receipt["status"], "validated")
            self.assertEqual(receipt["lanes"][1]["depends_on"], ["api"])
            for invalid in [
                {**payload, "actor": "reviewer", "credential": TOKENS["reviewer"]},
                {
                    **payload,
                    "lanes": [{"id": "other", "domain": "other"}],
                },
            ]:
                denied = subprocess.run(
                    [sys.executable, str(script), "orchestrate"],
                    input=json.dumps(invalid),
                    text=True,
                    capture_output=True,
                    check=False,
                    env={
                        **environment,
                        "CODEX_ORCHESTRATION_ACTOR": invalid["actor"],
                        "CODEX_ORCHESTRATION_CREDENTIAL": invalid[
                            "credential"
                        ],
                    },
                    cwd=directory,
                )
                self.assertEqual(denied.returncode, 2)


if __name__ == "__main__":
    unittest.main()
