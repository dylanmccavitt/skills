#!/usr/bin/env python3

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from orchestration_graph import eligible_transitions, load_workflow, resolve_target, validate_workflow


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
        context = {
            "packet": {"artifact": {"status": "persisted"}, "pr_head_sha": "abc"},
            "live": {"pr_head_sha": "abc"},
        }
        matches = eligible_transitions(
            self.workflow, "implementation", "IMPLEMENTATION_PACKET", context
        )
        self.assertEqual([transition["id"] for transition in matches], ["implementation-proved"])

        context["live"]["pr_head_sha"] = "def"
        self.assertEqual(
            eligible_transitions(self.workflow, "implementation", "IMPLEMENTATION_PACKET", context),
            [],
        )

    def test_post_merge_failure_returns_to_research_as_remediation(self) -> None:
        matches = eligible_transitions(
            self.workflow, "integration_verification", "JIMINY_INTEGRATION_FAILED", {}
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

    def test_jiminy_completion_and_merge_set_use_packet_root_guards(self) -> None:
        complete = eligible_transitions(
            self.workflow,
            "integration_verification",
            "JIMINY_COMPLETE",
            {"packet": {"integration": {
                "expected_merges_present": True,
                "required_checks_green": True,
                "linked_issues_verified": True,
                "runtime_ready_for_completion": True,
            }}},
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
