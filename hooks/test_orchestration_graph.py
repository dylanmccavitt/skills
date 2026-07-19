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


if __name__ == "__main__":
    unittest.main()
