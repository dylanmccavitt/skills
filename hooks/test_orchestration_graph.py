#!/usr/bin/env python3

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from orchestration_graph import eligible_transitions, load_workflow, resolve_target, validate_workflow
from test_orchestration_packets import CONTENT_REF, SHA, valid_packets


class OrchestrationGraphTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow()

    def test_graph_is_valid_and_preserves_existing_phase_order(self) -> None:
        self.assertEqual(self.workflow["initial_node"], "research")
        expected = {
            ("research", "implementation"),
            ("implementation", "review"),
            ("review", "fixer"),
            ("fixer", "review"),
            ("review", "merge"),
            ("merge", "integration_verification"),
            ("integration_verification", "complete"),
        }
        actual = {
            (source, transition["to"])
            for transition in self.workflow["transitions"]
            if "to" in transition
            for source in transition["from"]
        }
        self.assertTrue(expected <= actual)

    def test_implementation_requires_persisted_exact_head_proof(self) -> None:
        packet = valid_packets()["IMPLEMENTATION_PACKET"]
        context = {
            "packet": packet,
            "live": {"pr_head_sha": SHA},
        }
        matches = eligible_transitions(
            self.workflow, "implementation", "IMPLEMENTATION_PACKET", context
        )
        self.assertEqual([transition["id"] for transition in matches], ["implementation-proved"])

        context["live"]["pr_head_sha"] = "b" * 40
        self.assertEqual(
            eligible_transitions(self.workflow, "implementation", "IMPLEMENTATION_PACKET", context),
            [],
        )

    def test_temporary_artifact_cannot_satisfy_persistence_transition(self) -> None:
        packet = valid_packets()["IMPLEMENTATION_PACKET"]
        packet["artifact"] = {
            "kind": "tmp_markdown",
            "status": "persisted",
            "marker": None,
            "content_ref": CONTENT_REF,
            "path": "/tmp/implementation.md",
        }
        self.assertEqual(
            eligible_transitions(
                self.workflow,
                "implementation",
                "IMPLEMENTATION_PACKET",
                {"packet": packet, "live": {"pr_head_sha": SHA}},
            ),
            [],
        )

    def test_propose_only_research_cannot_claim_persisted_issue_artifact(self) -> None:
        packet = valid_packets()["RESEARCH_PACKET"]
        packet["issue_write_authority"] = "propose-only"
        self.assertEqual(
            eligible_transitions(
                self.workflow,
                "research",
                "RESEARCH_PACKET",
                {"packet": packet},
            ),
            [],
        )

    def test_contradictory_ready_review_cannot_advance_to_merge(self) -> None:
        packet = valid_packets()["REVIEW_PACKET"]
        packet["ci_checks"][0]["conclusion"] = "failure"
        packet["blockers"] = ["CI failed"]
        self.assertEqual(
            eligible_transitions(
                self.workflow,
                "review",
                "REVIEW_PACKET",
                {"packet": packet, "live": {"pr_head_sha": SHA}},
            ),
            [],
        )

    def test_post_merge_failure_returns_to_research_as_remediation(self) -> None:
        matches = eligible_transitions(
            self.workflow,
            "integration_verification",
            "JIMINY_INTEGRATION_FAILED",
            {"packet": valid_packets()["JIMINY_INTEGRATION_FAILED"]},
        )
        self.assertEqual(matches[0]["to"], "research")
        self.assertEqual(matches[0]["set"]["research_mode"], "remediation_leaf")

    def test_blocked_flow_resumes_at_recorded_node(self) -> None:
        matches = eligible_transitions(
            self.workflow, "blocked", "RESUME_AUTHORIZED", {"resume_node": "review"}
        )
        self.assertEqual(resolve_target(matches[0], {"resume_node": "review"}, self.workflow["nodes"]), "review")

    def test_validator_rejects_unknown_transition_target(self) -> None:
        invalid = copy.deepcopy(self.workflow)
        invalid["transitions"][0]["to"] = "missing"
        with self.assertRaisesRegex(ValueError, "unknown target"):
            validate_workflow(invalid)

    def test_validator_rejects_malformed_policies_guards_and_packet_references(self) -> None:
        invalid_policy = copy.deepcopy(self.workflow)
        invalid_policy["policies"]["unknown"] = True
        malformed_guard = copy.deepcopy(self.workflow)
        malformed_guard["transitions"][0]["all"][0]["non_empty"] = True
        packet_mismatch = copy.deepcopy(self.workflow)
        packet_mismatch["transitions"][0]["packet_type"] = "REVIEW_PACKET"
        unknown_packet = copy.deepcopy(self.workflow)
        unknown_packet["transitions"][0]["event"] = "UNKNOWN_PACKET"
        unknown_packet["transitions"][0].pop("packet_type")
        unknown_transition_key = copy.deepcopy(self.workflow)
        unknown_transition_key["transitions"][0]["surprise"] = True
        for workflow in (
            invalid_policy, malformed_guard, packet_mismatch, unknown_packet,
            unknown_transition_key,
        ):
            with self.subTest(workflow=workflow), self.assertRaises(ValueError):
                validate_workflow(workflow)

    def test_validator_rejects_non_positive_or_fractional_increments(self) -> None:
        transition_index = next(
            index
            for index, transition in enumerate(self.workflow["transitions"])
            if transition["id"] == "review-found-issues"
        )
        for amount in (0, -1, 0.5):
            invalid = copy.deepcopy(self.workflow)
            invalid["transitions"][transition_index]["increment"]["by"] = amount
            with self.subTest(amount=amount), self.assertRaisesRegex(
                ValueError, "must be a positive integer"
            ):
                validate_workflow(invalid)

    def test_packet_driven_transition_rejects_invalid_packet_before_guards(self) -> None:
        packet = valid_packets()["IMPLEMENTATION_PACKET"]
        packet["pr_head_sha"] = "abc"
        context = {"packet": packet, "live": {"pr_head_sha": "abc"}}
        self.assertEqual(
            eligible_transitions(
                self.workflow, "implementation", "IMPLEMENTATION_PACKET", context
            ),
            [],
        )

    def test_jiminy_completion_and_merge_set_use_packet_root_guards(self) -> None:
        complete = eligible_transitions(
            self.workflow,
            "integration_verification",
            "JIMINY_COMPLETE",
            {"packet": valid_packets()["JIMINY_COMPLETE"]},
        )
        self.assertEqual([item["id"] for item in complete], ["integration-passed"])
        merges = eligible_transitions(
            self.workflow,
            "merge",
            "MERGES_VERIFIED",
            {
                "ready": {"expected_pr_urls": ["https://pr/1"]},
                "packet": {"merge_results": {"https://pr/1": "a" * 40}},
            },
        )
        self.assertEqual([item["id"] for item in merges], ["merge-set-verified"])
        mismatch = {
            "ready": {"expected_pr_urls": ["https://pr/1"]},
            "packet": {"merge_results": {"https://pr/2": "b" * 40}},
        }
        self.assertEqual(eligible_transitions(self.workflow, "merge", "MERGES_VERIFIED", mismatch), [])

    def test_fixer_head_change_invalidates_pass_and_review_cycles_are_bounded(self) -> None:
        changed = eligible_transitions(self.workflow, "fixer", "PR_HEAD_CHANGED", {})
        self.assertEqual(changed[0]["to"], "review")
        self.assertTrue(changed[0]["set"]["fixer_pass_invalidated"])
        allowed = eligible_transitions(
            self.workflow, "review", "ACTIONABLE_FINDINGS", {"review_fix_cycles": 2}
        )
        blocked = eligible_transitions(
            self.workflow, "review", "ACTIONABLE_FINDINGS", {"review_fix_cycles": 3}
        )
        self.assertEqual(allowed[0]["increment"], {"path": "review_fix_cycles", "by": 1})
        self.assertEqual(blocked, [])


if __name__ == "__main__":
    unittest.main()
