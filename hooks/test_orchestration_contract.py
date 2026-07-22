#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from orchestration_contract import canonical_delivery_spec_digest, parse_delivery_spec


def valid_specification(*, multi_leaf: bool = False) -> dict[str, object]:
    leaves: list[dict[str, object]] = [{
        "id": "leaf-1",
        "issue_url": "https://github.com/owner/repo/issues/1",
        "dependencies": [],
        "owned_path_prefixes": ["hooks/orchestration_contract.py", "gepetto/"],
        "shared_paths": [],
    }]
    if multi_leaf:
        leaves.append({
            "id": "leaf-2",
            "issue_url": "https://github.com/owner/repo/issues/2",
            "dependencies": ["leaf-1"],
            "owned_path_prefixes": ["hooks/second.py"],
            "shared_paths": ["package.json"],
        })
    return {
        "version": 1,
        "intent": "Require a validated delivery contract.",
        "observable_outcome": "Implementation starts only after validation.",
        "non_goals": ["Do not enforce path ownership."],
        "invariants": ["Rejected contracts do not mutate coordinator state."],
        "architecture_decisions": [{
            "domain": "format",
            "decision": "Use a versioned JSON delivery specification.",
        }],
        "decision_owners": [{
            "domain": "implementation",
            "owner": "Pinocchio writer",
            "constraint": "Must preserve atomic state acceptance.",
        }],
        "leaves": leaves,
        "acceptance_criteria": [{
            "id": "AC1",
            "criterion": "The contract is validated.",
            "validation": ["python3 -m unittest hooks.test_orchestration_contract"],
        }],
        "external_effect_gates": [{
            "effect": "Open a pull request.",
            "authority": "authorized",
        }],
    }


def artifact_text(specification: dict[str, object] | None = None) -> str:
    specification = specification or valid_specification()
    return (
        "# Research contract\n\n"
        "```json delivery_spec\n"
        f"{json.dumps(specification, indent=2)}\n"
        "```\n"
    )


class OrchestrationContractTest(unittest.TestCase):
    def test_valid_single_and_multi_leaf_version_one_specs_pass(self) -> None:
        for multi_leaf in (False, True):
            with self.subTest(multi_leaf=multi_leaf):
                self.assertEqual(
                    parse_delivery_spec(artifact_text(valid_specification(multi_leaf=multi_leaf))),
                    valid_specification(multi_leaf=multi_leaf),
                )

    def test_cross_leaf_path_collisions_remain_out_of_scope(self) -> None:
        specification = valid_specification(multi_leaf=True)
        specification["leaves"][1]["owned_path_prefixes"] = ["gepetto/"]
        self.assertEqual(
            parse_delivery_spec(artifact_text(specification)),
            specification,
        )

    def test_required_contract_and_leaf_fields_fail_closed(self) -> None:
        cases = (
            ("intent", lambda spec: spec.pop("intent")),
            ("decision ownership", lambda spec: spec.__setitem__("decision_owners", [])),
            ("acceptance criteria", lambda spec: spec.__setitem__("acceptance_criteria", [])),
            (
                "criterion validation",
                lambda spec: spec["acceptance_criteria"][0].__setitem__("validation", []),
            ),
            ("leaf id", lambda spec: spec["leaves"][0].pop("id")),
            ("leaf issue", lambda spec: spec["leaves"][0].pop("issue_url")),
            ("owned paths", lambda spec: spec["leaves"][0].__setitem__("owned_path_prefixes", [])),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                specification = valid_specification()
                mutate(specification)
                with self.assertRaises(ValueError):
                    parse_delivery_spec(artifact_text(specification))

    def test_duplicate_ids_missing_dependencies_cycles_paths_and_versions_fail(self) -> None:
        duplicate = valid_specification(multi_leaf=True)
        duplicate["leaves"][1]["id"] = "leaf-1"
        missing = valid_specification(multi_leaf=True)
        missing["leaves"][1]["dependencies"] = ["missing"]
        cycle = valid_specification(multi_leaf=True)
        cycle["leaves"][0]["dependencies"] = ["leaf-2"]
        malformed_path = valid_specification()
        malformed_path["leaves"][0]["owned_path_prefixes"] = ["../hooks/"]
        unknown_version = valid_specification()
        unknown_version["version"] = 2
        for name, specification in (
            ("duplicate leaf", duplicate),
            ("missing dependency", missing),
            ("cycle", cycle),
            ("malformed path", malformed_path),
            ("unknown version", unknown_version),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                parse_delivery_spec(artifact_text(specification))

    def test_parser_rejects_duplicate_blocks_keys_unknown_fields_and_non_live_urls(self) -> None:
        duplicate_block = artifact_text() + artifact_text()
        duplicate_key = artifact_text().replace('"version": 1,', '"version": 1,\n  "version": 1,', 1)
        unknown = valid_specification()
        unknown["surprise"] = True
        markdown_url = valid_specification()
        markdown_url["leaves"][0]["issue_url"] = "[issue](https://github.com/owner/repo/issues/1)"
        for name, artifact in (
            ("duplicate block", duplicate_block),
            ("duplicate key", duplicate_key),
            ("unknown field", artifact_text(unknown)),
            ("markdown URL", artifact_text(markdown_url)),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                parse_delivery_spec(artifact)

    def test_canonical_digest_is_stable_across_json_formatting_and_key_order(self) -> None:
        specification = valid_specification()
        reordered = dict(reversed(list(specification.items())))
        compact_artifact = f"```json delivery_spec\n{json.dumps(reordered)}\n```"
        first = parse_delivery_spec(artifact_text(specification))
        second = parse_delivery_spec(compact_artifact)
        self.assertEqual(
            canonical_delivery_spec_digest(first),
            canonical_delivery_spec_digest(second),
        )
        self.assertRegex(canonical_delivery_spec_digest(first), r"^sha256:[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
