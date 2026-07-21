#!/usr/bin/env python3
"""Validate and evaluate the machine-readable Gepetto delivery graph."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_WORKFLOW = Path(__file__).parents[1] / "gepetto" / "references" / "workflow.json"


def load_workflow(path: Path = DEFAULT_WORKFLOW) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        workflow = json.load(handle)
    validate_workflow(workflow)
    return workflow


def validate_workflow(workflow: dict[str, Any]) -> None:
    if workflow.get("version") != 1:
        raise ValueError("unsupported workflow version")
    nodes = workflow.get("nodes")
    transitions = workflow.get("transitions")
    if not isinstance(nodes, dict) or not nodes:
        raise ValueError("workflow nodes must be a non-empty object")
    if workflow.get("initial_node") not in nodes:
        raise ValueError("initial_node must name a workflow node")
    if not isinstance(transitions, list) or not transitions:
        raise ValueError("workflow transitions must be a non-empty array")

    transition_ids: set[str] = set()
    for transition in transitions:
        transition_id = transition.get("id")
        if not isinstance(transition_id, str) or not transition_id:
            raise ValueError("every transition requires an id")
        if transition_id in transition_ids:
            raise ValueError(f"duplicate transition id: {transition_id}")
        transition_ids.add(transition_id)

        sources = transition.get("from")
        if not isinstance(sources, list) or not sources:
            raise ValueError(f"transition {transition_id} requires source nodes")
        unknown_sources = set(sources) - set(nodes)
        if unknown_sources:
            raise ValueError(f"transition {transition_id} has unknown sources: {sorted(unknown_sources)}")
        if not isinstance(transition.get("event"), str) or not transition["event"]:
            raise ValueError(f"transition {transition_id} requires an event")

        has_static_target = "to" in transition
        has_dynamic_target = "to_path" in transition
        if has_static_target == has_dynamic_target:
            raise ValueError(f"transition {transition_id} requires exactly one target")
        if has_static_target and transition["to"] not in nodes:
            raise ValueError(f"transition {transition_id} has an unknown target")


def _lookup(context: dict[str, Any], path: str) -> Any:
    value: Any = context
    for segment in path.split("."):
        if not isinstance(value, dict) or segment not in value:
            return None
        value = value[segment]
    return value


def _condition_passes(
    condition: dict[str, Any], context: dict[str, Any], policies: dict[str, Any]
) -> bool:
    value = _lookup(context, condition["path"])
    if "equals" in condition:
        return value == condition["equals"]
    if "equals_path" in condition:
        return value == _lookup(context, condition["equals_path"])
    if condition.get("non_empty") is True:
        return bool(value)
    if "less_than_policy" in condition:
        limit = policies.get(condition["less_than_policy"])
        return isinstance(value, (int, float)) and isinstance(limit, (int, float)) and value < limit
    if "map_keys_equal_path" in condition:
        expected = _lookup(context, condition["map_keys_equal_path"])
        return (
            isinstance(value, dict) and isinstance(expected, list)
            and len(value) == len(expected) and set(value) == set(expected)
        )
    if condition.get("values_full_sha") is True:
        return isinstance(value, dict) and all(
            isinstance(item, str) and len(item) == 40
            and all(character in "0123456789abcdefABCDEF" for character in item)
            for item in value.values()
        )
    raise ValueError(f"unsupported workflow condition: {condition}")


def eligible_transitions(
    workflow: dict[str, Any],
    current_node: str,
    event: str,
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    if current_node not in workflow["nodes"]:
        raise ValueError(f"unknown current node: {current_node}")
    return [
        transition
        for transition in workflow["transitions"]
        if current_node in transition["from"]
        and transition["event"] == event
        and all(
            _condition_passes(condition, context, workflow.get("policies", {}))
            for condition in transition.get("all", [])
        )
    ]


def resolve_target(transition: dict[str, Any], context: dict[str, Any], nodes: dict[str, Any]) -> str:
    target = transition.get("to") or _lookup(context, transition["to_path"])
    if target not in nodes:
        raise ValueError(f"transition resolved to unknown target: {target!r}")
    return target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", type=Path, default=DEFAULT_WORKFLOW)
    return parser


def main() -> int:
    workflow = load_workflow(_parser().parse_args().workflow)
    print(f"valid {workflow['workflow']} v{workflow['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
