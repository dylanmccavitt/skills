"""Tests for deterministic protocol replay and raw evidence contracts."""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import replay


SHA_A = "a" * 40
DIGEST_1 = "sha256:" + ("1" * 64)
DIGEST_2 = "sha256:" + ("2" * 64)
DIGEST_3 = "sha256:" + ("3" * 64)


def _graph(
    *,
    bounded_review: bool = True,
    merge_review: bool = False,
    content_guard: bool = False,
) -> dict:
    review_transition = {
        "id": "review-found-issues",
        "from": ["review", *(["merge"] if merge_review else [])],
        "event": "ACTIONABLE_FINDINGS",
        "to": "fixer",
    }
    if bounded_review:
        review_transition["all"] = [
            {
                "path": "review_fix_cycles",
                "less_than_policy": "max_review_fix_cycles",
            }
        ]
        review_transition["increment"] = {"path": "review_fix_cycles", "by": 1}
    research_conditions = [
        {"path": "packet.artifact.status", "equals": "persisted"},
        {"path": "packet.delivery_issue_urls", "non_empty": True},
    ]
    if content_guard:
        research_conditions.append(
            {"path": "persisted.validated_delivery_spec_digest", "content_ref": True}
        )
    return {
        "version": 1,
        "workflow": "test-delivery",
        "initial_node": "research",
        "policies": {"max_review_fix_cycles": 3},
        "nodes": {
            name: {}
            for name in (
                "research",
                "implementation",
                "review",
                "fixer",
                "merge",
                "blocked",
                "complete",
            )
        },
        "transitions": [
            {
                "id": "research-approved",
                "from": ["research"],
                "event": "RESEARCH_PACKET",
                "to": "implementation",
                "all": research_conditions,
            },
            {
                "id": "implementation-proved",
                "from": ["implementation"],
                "event": "IMPLEMENTATION_PACKET",
                "to": "review",
                "all": [
                    {"path": "packet.artifact.status", "equals": "persisted"},
                    {
                        "path": "packet.pr_head_sha",
                        "equals_path": "live.pr_head_sha",
                    },
                ],
            },
            review_transition,
            {
                "id": "fix-pushed",
                "from": ["fixer"],
                "event": "FIX_PUSHED",
                "to": "review",
            },
            {
                "id": "merge-verified",
                "from": ["merge"],
                "event": "MERGES_VERIFIED",
                "to": "complete",
                "all": [
                    {
                        "path": "packet.merge_results",
                        "map_keys_equal_path": "ready.expected_pr_urls",
                    },
                    {"path": "packet.merge_results", "values_full_sha": True},
                ],
                "set": {"proof.verified": True},
            },
            {
                "id": "lane-blocked",
                "from": ["research", "implementation", "review", "fixer", "merge"],
                "event": "FLOW_BLOCKED",
                "to": "blocked",
            },
            {
                "id": "resume",
                "from": ["blocked"],
                "event": "RESUME_AUTHORIZED",
                "to_path": "resume_node",
            },
        ],
    }


def _trace(
    events: list[dict],
    *,
    node: str = "research",
    data: dict | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "trace_id": "test-trace",
        "trace_version": 1,
        "suite": {"id": "test-suite", "version": 1, "content_sha256": DIGEST_1},
        "fixture": {
            "id": "test-fixture",
            "version": 1,
            "content_sha256": DIGEST_2,
        },
        "initial_repository_sha": SHA_A,
        "execution": {
            "kind": "protocol-replay",
            "model": "non-agent",
            "reasoning": "none",
            "time_budget_seconds": 60,
            "environment_contract_sha256": DIGEST_3,
            "trial_id": "trial-one",
        },
        "initial_state": {"node": node, "data": data or {}},
        "events": events,
    }


def _event(
    event_id: str,
    name: str,
    node: str,
    context: dict | None = None,
    expected: dict | None = None,
) -> dict:
    value = {
        "id": event_id,
        "event": name,
        "current_node": node,
        "context": context or {},
    }
    if expected is not None:
        value["expected"] = expected
    return value


class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.name", "Replay Test")
        self._git("config", "user.email", "replay@example.invalid")
        evaluator = self.repo / "evaluation/replay.py"
        evaluator.parent.mkdir()
        evaluator.write_bytes(Path(replay.__file__).read_bytes())
        self.refs: dict[str, str] = {}
        self.refs["v0.2.0"] = self._commit_graph(
            _graph(bounded_review=False), "v0.2.0"
        )
        self.refs["v0.4.0"] = self._commit_graph(_graph(), "v0.4.0")
        self.refs["current-main"] = self._commit_graph(
            _graph(content_guard=True), "current-main"
        )
        self.refs["frozen-candidate"] = self._commit_graph(
            _graph(content_guard=True, merge_review=True), "frozen-candidate"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _commit_graph(self, graph: dict, tag: str) -> str:
        path = self.repo / replay.WORKFLOW_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(replay.canonical_bytes(graph))
        self._git("add", "-A")
        self._git("commit", "-q", "-m", tag)
        commit = self._git("rev-parse", "HEAD")
        self._git("tag", tag)
        return commit

    def _write_trace(self, trace: dict) -> Path:
        path = self.root / "trace.json"
        path.write_bytes(replay.canonical_bytes(trace))
        return path

    def test_trace_contract_rejects_duplicates_unknowns_and_bad_identity(self) -> None:
        with self.assertRaisesRegex(replay.ReplayError, "duplicate JSON key"):
            replay.loads_json('{"schema_version":1,"schema_version":1}', "duplicate")
        trace = _trace([_event("event-one", "FLOW_BLOCKED", "research")])
        invalid = copy.deepcopy(trace)
        invalid["unknown"] = True
        with self.assertRaisesRegex(replay.ReplayError, "unknown fields"):
            replay.validate_trace(invalid)
        invalid = copy.deepcopy(trace)
        invalid["initial_repository_sha"] = "short"
        with self.assertRaisesRegex(replay.ReplayError, "invalid full commit SHA"):
            replay.validate_trace(invalid)
        invalid = copy.deepcopy(trace)
        invalid["events"].append(copy.deepcopy(invalid["events"][0]))
        with self.assertRaisesRegex(replay.ReplayError, "duplicate event ID"):
            replay.validate_trace(invalid)
        invalid = copy.deepcopy(trace)
        invalid["execution"]["model"] = "agent"
        with self.assertRaisesRegex(replay.ReplayError, "non-agent/none"):
            replay.validate_trace(invalid)
        invalid = copy.deepcopy(trace)
        invalid["events"][0]["context"] = {"command": "rm something"}
        with self.assertRaisesRegex(replay.ReplayError, "forbidden replay context"):
            replay.validate_trace(invalid)

    def test_supported_guards_mutations_recovery_and_raw_outcomes(self) -> None:
        graph = _graph(content_guard=True)
        trace = _trace(
            [
                _event(
                    "research-approved",
                    "RESEARCH_PACKET",
                    "research",
                    {
                        "packet": {
                            "artifact": {"status": "persisted"},
                            "delivery_issue_urls": ["issue"],
                        },
                        "persisted": {"validated_delivery_spec_digest": DIGEST_1},
                    },
                    {"disposition": "accepted", "target_node": "implementation"},
                ),
                _event(
                    "stale-head",
                    "IMPLEMENTATION_PACKET",
                    "implementation",
                    {
                        "packet": {
                            "artifact": {"status": "persisted"},
                            "pr_head_sha": "b" * 40,
                        },
                        "live": {"pr_head_sha": "c" * 40},
                    },
                    {"disposition": "rejected", "error_code": "guard-rejected"},
                ),
                _event("block", "FLOW_BLOCKED", "implementation"),
                _event(
                    "resume",
                    "RESUME_AUTHORIZED",
                    "blocked",
                    {"resume_node": "implementation"},
                ),
                _event("unsupported", "NOT_IN_GRAPH", "implementation"),
                _event("wrong-node", "FLOW_BLOCKED", "review"),
            ],
            data={"review_fix_cycles": 0},
        )
        records, _, final = replay.replay_events(
            graph, trace, "run-" + ("0" * 24)
        )
        self.assertEqual(
            [record["disposition"] for record in records],
            ["accepted", "rejected", "accepted", "accepted", "unsupported", "error"],
        )
        for record in records:
            if record["disposition"] != "accepted":
                self.assertEqual(
                    record["before_state_sha256"], record["after_state_sha256"]
                )
        self.assertEqual(final["node"], "implementation")

    def test_review_bound_and_all_condition_operators(self) -> None:
        graph = _graph()
        review_events: list[dict] = []
        for cycle in range(3):
            review_events.extend(
                [
                    _event(f"finding-{cycle}", "ACTIONABLE_FINDINGS", "review"),
                    _event(f"fix-{cycle}", "FIX_PUSHED", "fixer"),
                ]
            )
        review_events.append(
            _event(
                "finding-limit",
                "ACTIONABLE_FINDINGS",
                "review",
                expected={"disposition": "rejected", "error_code": "guard-rejected"},
            )
        )
        records, _, final = replay.replay_events(
            graph,
            _trace(review_events, node="review", data={"review_fix_cycles": 0}),
            "run-" + ("1" * 24),
        )
        self.assertEqual(records[-1]["disposition"], "rejected")
        self.assertEqual(final["data"]["review_fix_cycles"], 3)

        merge_trace = _trace(
            [
                _event(
                    "verified",
                    "MERGES_VERIFIED",
                    "merge",
                    {
                        "packet": {"merge_results": {"pr": "d" * 40}},
                        "ready": {"expected_pr_urls": ["pr"]},
                    },
                )
            ],
            node="merge",
        )
        _, _, final = replay.replay_events(
            graph, merge_trace, "run-" + ("2" * 24)
        )
        self.assertTrue(final["data"]["proof"]["verified"])

    def test_four_ref_replay_is_deterministic_and_exposes_known_difference(self) -> None:
        trace = _trace(
            [_event("candidate-difference", "ACTIONABLE_FINDINGS", "merge")],
            node="merge",
            data={"review_fix_cycles": 0},
        )
        trace_path = self._write_trace(trace)
        output = self.root / "output"
        refs = ["v0.2.0", "v0.4.0", "current-main", "frozen-candidate"]
        first = replay.run_replay(
            self.repo, trace_path, output, refs, "frozen-candidate"
        )
        snapshot = {
            path.name: {
                child.name: child.read_bytes()
                for child in path.iterdir()
                if child.is_file()
            }
            for path in first
        }
        second = replay.run_replay(
            self.repo, trace_path, output, refs, "frozen-candidate"
        )
        self.assertEqual([path.name for path in first], [path.name for path in second])
        for path in second:
            self.assertEqual(
                snapshot[path.name],
                {
                    child.name: child.read_bytes()
                    for child in path.iterdir()
                    if child.is_file()
                },
            )
        dispositions = {}
        for path in first:
            manifest = replay.load_json(path / "manifest.json")
            events = replay.parse_jsonl(
                (path / "events.jsonl").read_bytes(), "events"
            )
            dispositions[manifest["requested_ref"]] = events[0]["disposition"]
        self.assertEqual(dispositions["current-main"], "unsupported")
        self.assertEqual(dispositions["frozen-candidate"], "accepted")

    def test_refs_are_local_exact_and_fail_closed(self) -> None:
        self.assertEqual(
            replay.resolve_ref(self.repo, "v0.2.0"), self.refs["v0.2.0"]
        )
        with self.assertRaisesRegex(replay.ReplayError, "local git"):
            replay.resolve_ref(self.repo, "missing")
        self._git("branch", "v0.2.0", self.refs["frozen-candidate"])
        with self.assertRaisesRegex(replay.ReplayError, "ambiguous ref"):
            replay.resolve_ref(self.repo, "v0.2.0")
        (self.repo / replay.WORKFLOW_PATH).unlink()
        empty = self.repo / "empty.txt"
        empty.write_text("no workflow here\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "missing workflow")
        self._git("tag", "missing-workflow")
        with self.assertRaisesRegex(replay.ReplayError, "local git show"):
            replay.load_workflow_ref(self.repo, "missing-workflow")

    def test_unsupported_workflow_vocabulary_is_rejected(self) -> None:
        graph = _graph()
        graph["version"] = 2
        with self.assertRaisesRegex(replay.ReplayError, "unsupported version"):
            replay.validate_workflow(graph)
        graph = _graph()
        graph["transitions"][0]["any"] = []
        with self.assertRaisesRegex(replay.ReplayError, "unsupported fields"):
            replay.validate_workflow(graph)
        graph = _graph()
        graph["transitions"][0]["all"][0] = {
            "path": "packet",
            "regex": ".*",
        }
        with self.assertRaisesRegex(replay.ReplayError, "unsupported condition"):
            replay.validate_workflow(graph)

    def test_evaluator_commit_must_bind_running_bytes(self) -> None:
        evaluator = self.repo / "evaluation/replay.py"
        evaluator.write_text("# different evaluator\n", encoding="utf-8")
        self._git("add", "evaluation/replay.py")
        self._git("commit", "-q", "-m", "different evaluator")
        self._git("tag", "different-evaluator")
        trace_path = self._write_trace(
            _trace([_event("block", "FLOW_BLOCKED", "research")])
        )
        with self.assertRaisesRegex(replay.ReplayError, "do not match"):
            replay.run_replay(
                self.repo,
                trace_path,
                self.root / "output",
                ["v0.4.0"],
                "different-evaluator",
            )

    def test_comparability_names_exact_mismatch_keys(self) -> None:
        trace = _trace([_event("block", "FLOW_BLOCKED", "research")])
        trace_path = self._write_trace(trace)
        manifest, _, _ = replay.build_run(
            repo=self.repo,
            trace=trace,
            trace_sha256=replay.digest_bytes(trace_path.read_bytes()),
            requested_ref="v0.4.0",
            evaluator_commit=self.refs["frozen-candidate"],
        )
        changed = copy.deepcopy(manifest)
        changed["execution"]["trial_id"] = "trial-two"
        changed["workflow"]["commit_sha"] = self.refs["v0.2.0"]
        self.assertEqual(
            replay.comparability_mismatches(manifest, changed),
            ["execution.trial_id"],
        )
        with self.assertRaisesRegex(
            replay.ReplayError, r"execution\.trial_id"
        ):
            replay.require_comparable([manifest, changed])
        changed = copy.deepcopy(manifest)
        changed["fixture"]["content_sha256"] = "sha256:" + ("9" * 64)
        with self.assertRaisesRegex(replay.ReplayError, "run identity digest mismatch"):
            replay.validate_manifest(changed)

    def test_partial_recovery_conflict_tampering_and_unsafe_output(self) -> None:
        trace_path = self._write_trace(
            _trace([_event("block", "FLOW_BLOCKED", "research")])
        )
        output = self.root / "output"
        [run_dir] = replay.run_replay(
            self.repo, trace_path, output, ["v0.4.0"], "frozen-candidate"
        )
        (run_dir / "result.json").unlink()
        replay.run_replay(
            self.repo, trace_path, output, ["v0.4.0"], "frozen-candidate"
        )
        replay.validate_run_directory(run_dir)

        event_path = run_dir / "events.jsonl"
        original = event_path.read_bytes()
        event_path.write_bytes(original.replace(b'"accepted"', b'"rejected"', 1))
        with self.assertRaises(replay.ReplayError):
            replay.validate_run_directory(run_dir)
        with self.assertRaisesRegex(replay.ReplayError, "refusing to overwrite"):
            replay.run_replay(
                self.repo, trace_path, output, ["v0.4.0"], "frozen-candidate"
            )

        symlink = self.root / "output-link"
        symlink.symlink_to(output, target_is_directory=True)
        with self.assertRaisesRegex(replay.ReplayError, "must not be a symlink"):
            replay.run_replay(
                self.repo, trace_path, symlink, ["v0.4.0"], "frozen-candidate"
            )

    def test_malformed_jsonl_and_dangling_counts_are_rejected(self) -> None:
        with self.assertRaisesRegex(replay.ReplayError, "must end with one newline"):
            replay.parse_jsonl(b"{}", "events")
        with self.assertRaisesRegex(replay.ReplayError, "blank JSONL line"):
            replay.parse_jsonl(b"{}\n\n", "events")
        trace_path = self._write_trace(
            _trace([_event("block", "FLOW_BLOCKED", "research")])
        )
        [run_dir] = replay.run_replay(
            self.repo,
            trace_path,
            self.root / "output",
            ["v0.4.0"],
            "frozen-candidate",
        )
        result = replay.load_json(run_dir / "result.json")
        result["counts"]["accepted"] += 1
        (run_dir / "result.json").write_bytes(replay.canonical_bytes(result))
        with self.assertRaisesRegex(replay.ReplayError, "inconsistent disposition"):
            replay.validate_run_directory(run_dir)

    def test_trace_expected_assertions_fail_before_persistence(self) -> None:
        trace = _trace(
            [
                _event(
                    "bad-expectation",
                    "FLOW_BLOCKED",
                    "research",
                    expected={"disposition": "rejected"},
                )
            ]
        )
        trace_path = self._write_trace(trace)
        output = self.root / "output"
        with self.assertRaisesRegex(replay.ReplayError, "assertion mismatch"):
            replay.run_replay(
                self.repo, trace_path, output, ["v0.4.0"], "frozen-candidate"
            )
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
