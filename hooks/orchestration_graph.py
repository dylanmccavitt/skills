#!/usr/bin/env python3
"""Validate and evaluate the machine-readable Gepetto delivery graph."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from orchestration_packets import PACKET_TYPES, validate_packet


DEFAULT_WORKFLOW = Path(__file__).parents[1] / "gepetto" / "references" / "workflow.json"

WORKFLOW_KEYS = {"version", "workflow", "initial_node", "policies", "packet_types", "nodes", "transitions"}
NODE_KEYS = {
    "owner", "lane_role", "fanout", "serial_per_pull_request",
    "dependency_ordered", "resumable", "terminal",
}
TRANSITION_KEYS = {"id", "from", "event", "to", "to_path", "packet_type", "all", "increment", "set"}
CONDITION_OPERATORS = {
    "equals", "equals_path", "non_empty", "less_than_policy",
    "map_keys_equal_path", "values_full_sha",
}


def _exact_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{label} has unknown keys: {sorted(unknown)}")


def _path(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value or any(not part for part in value.split(".")):
        raise ValueError(f"{label} must be a dotted path")


def _positive_integer(value: Any, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


def _validate_policies(policies: Any) -> None:
    if not isinstance(policies, dict):
        raise ValueError("workflow policies must be an object")
    required = {
        "max_review_fix_cycles", "one_writer_per_leaf", "proof_bound_to_live_head",
        "inline_research", "packet_authoritative_when_head_matches", "context_addressing",
        "registration", "terminal_receipts", "supervision",
    }
    if set(policies) != required:
        raise ValueError("workflow policies must contain exactly the supported policy keys")
    _positive_integer(policies["max_review_fix_cycles"], "max_review_fix_cycles")
    for name in ("one_writer_per_leaf", "proof_bound_to_live_head", "packet_authoritative_when_head_matches"):
        if not isinstance(policies[name], bool):
            raise ValueError(f"policy {name} must be a boolean")
    if policies["inline_research"] != "single_leaf_keep":
        raise ValueError("unsupported inline_research policy")

    context = policies["context_addressing"]
    expected_context = {
        "algorithm": "sha256", "input": "exact_file_bytes", "digest_version": 1,
        "includes_arity": True, "reload": "digest_change_only", "survives_continuation": True,
    }
    if context != expected_context:
        raise ValueError("unsupported context_addressing policy")
    if policies["registration"] != {
        "authority": "coordinator", "child_action": "verify", "conflicts": "reject"
    }:
        raise ValueError("unsupported registration policy")
    if policies["terminal_receipts"] != {
        "delivery": "task_final_result", "exactly_once": True,
        "separate_send": False, "jiminy_intermediate_notifications": True,
    }:
        raise ValueError("unsupported terminal_receipts policy")

    supervision = policies["supervision"]
    expected_supervision = {
        "heartbeat_ttl_seconds", "max_lane_restarts", "recycle_context_ratio",
        "recycle_state_bytes", "recycle_after_events", "pressure_ttl_seconds",
        "event_count_role",
    }
    if not isinstance(supervision, dict) or set(supervision) != expected_supervision:
        raise ValueError("supervision must contain exactly the supported keys")
    ttl = supervision["heartbeat_ttl_seconds"]
    if not isinstance(ttl, dict) or set(ttl) != {"gepetto", "research", "implementation", "review", "jiminy"}:
        raise ValueError("heartbeat_ttl_seconds must cover every supervised role")
    for role, seconds in ttl.items():
        _positive_integer(seconds, f"heartbeat_ttl_seconds.{role}")
    if not isinstance(supervision["max_lane_restarts"], int) or isinstance(supervision["max_lane_restarts"], bool) or supervision["max_lane_restarts"] < 0:
        raise ValueError("max_lane_restarts must be a non-negative integer")
    ratio = supervision["recycle_context_ratio"]
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not 0 < ratio <= 1:
        raise ValueError("recycle_context_ratio must be between zero and one")
    for name in ("recycle_state_bytes", "recycle_after_events", "pressure_ttl_seconds"):
        _positive_integer(supervision[name], name)
    if supervision["event_count_role"] != "compatibility_fallback":
        raise ValueError("unsupported event_count_role policy")


def _validate_condition(condition: Any, transition_id: str, policies: dict[str, Any]) -> None:
    if not isinstance(condition, dict):
        raise ValueError(f"transition {transition_id} conditions must be objects")
    _exact_keys(condition, {"path"} | CONDITION_OPERATORS, f"transition {transition_id} condition")
    _path(condition.get("path"), f"transition {transition_id} condition path")
    operators = set(condition) & CONDITION_OPERATORS
    if len(operators) != 1:
        raise ValueError(f"transition {transition_id} condition requires exactly one operator")
    operator = next(iter(operators))
    operand = condition[operator]
    if operator in {"equals_path", "map_keys_equal_path"}:
        _path(operand, f"transition {transition_id} {operator}")
    elif operator == "less_than_policy":
        if not isinstance(operand, str) or operand not in policies:
            raise ValueError(f"transition {transition_id} references unknown policy {operand!r}")
        if not isinstance(policies[operand], (int, float)) or isinstance(policies[operand], bool):
            raise ValueError(f"transition {transition_id} policy {operand!r} must be numeric")
    elif operator in {"non_empty", "values_full_sha"} and operand is not True:
        raise ValueError(f"transition {transition_id} {operator} must equal true")


def _validate_node(name: str, node: Any) -> None:
    if not isinstance(node, dict) or not node:
        raise ValueError(f"node {name} must be a non-empty object")
    _exact_keys(node, NODE_KEYS, f"node {name}")
    if "owner" in node and node["owner"] not in {"gepetto", "pinocchio", "reviewer", "jiminy"}:
        raise ValueError(f"node {name} has an unsupported owner")
    if "lane_role" in node and node["lane_role"] not in {
        "research", "implementation", "review", "fixer", "jiminy"
    }:
        raise ValueError(f"node {name} has an unsupported lane_role")
    if "fanout" in node and node["fanout"] not in {"approved_leaves", "pull_requests"}:
        raise ValueError(f"node {name} has an unsupported fanout")
    for key in ("serial_per_pull_request", "dependency_ordered", "resumable", "terminal"):
        if key in node and node[key] is not True:
            raise ValueError(f"node {name} {key} must equal true")
    if ("owner" in node) == (node.get("terminal") is True):
        raise ValueError(f"node {name} must be either owned or terminal")


def load_workflow(path: Path = DEFAULT_WORKFLOW) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        workflow = json.load(handle)
    validate_workflow(workflow)
    return workflow


def validate_workflow(workflow: dict[str, Any]) -> None:
    if not isinstance(workflow, dict):
        raise ValueError("workflow must be an object")
    _exact_keys(workflow, WORKFLOW_KEYS, "workflow")
    if workflow.get("version") != 1:
        raise ValueError("unsupported workflow version")
    if not isinstance(workflow.get("workflow"), str) or not workflow["workflow"]:
        raise ValueError("workflow requires a name")
    _validate_policies(workflow.get("policies"))
    packet_types = workflow.get("packet_types")
    if (
        not isinstance(packet_types, list)
        or not all(isinstance(item, str) for item in packet_types)
        or set(packet_types) != PACKET_TYPES
        or len(packet_types) != len(PACKET_TYPES)
    ):
        raise ValueError("packet_types must list every supported packet type exactly once")
    nodes = workflow.get("nodes")
    transitions = workflow.get("transitions")
    if not isinstance(nodes, dict) or not nodes:
        raise ValueError("workflow nodes must be a non-empty object")
    if workflow.get("initial_node") not in nodes:
        raise ValueError("initial_node must name a workflow node")
    if not isinstance(transitions, list) or not transitions:
        raise ValueError("workflow transitions must be a non-empty array")
    for name, node in nodes.items():
        if not isinstance(name, str) or not name:
            raise ValueError("workflow node names must be non-empty strings")
        _validate_node(name, node)

    transition_ids: set[str] = set()
    for transition in transitions:
        if not isinstance(transition, dict):
            raise ValueError("workflow transitions must be objects")
        _exact_keys(transition, TRANSITION_KEYS, "transition")
        transition_id = transition.get("id")
        if not isinstance(transition_id, str) or not transition_id:
            raise ValueError("every transition requires an id")
        if transition_id in transition_ids:
            raise ValueError(f"duplicate transition id: {transition_id}")
        transition_ids.add(transition_id)

        sources = transition.get("from")
        if (
            not isinstance(sources, list) or not sources
            or not all(isinstance(source, str) and source for source in sources)
            or len(sources) != len(set(sources))
        ):
            raise ValueError(f"transition {transition_id} requires source nodes")
        unknown_sources = set(sources) - set(nodes)
        if unknown_sources:
            raise ValueError(f"transition {transition_id} has unknown sources: {sorted(unknown_sources)}")
        if not isinstance(transition.get("event"), str) or not transition["event"]:
            raise ValueError(f"transition {transition_id} requires an event")
        packet_type = transition.get("packet_type")
        if transition["event"] in PACKET_TYPES:
            if packet_type != transition["event"]:
                raise ValueError(f"transition {transition_id} must reference packet type {transition['event']}")
        elif packet_type is not None:
            raise ValueError(f"transition {transition_id} has an unexpected packet reference")
        elif transition["event"].endswith("_PACKET") or transition["event"].startswith("JIMINY_"):
            raise ValueError(f"transition {transition_id} references an unknown packet event")

        has_static_target = "to" in transition
        has_dynamic_target = "to_path" in transition
        if has_static_target == has_dynamic_target:
            raise ValueError(f"transition {transition_id} requires exactly one target")
        if has_static_target and transition["to"] not in nodes:
            raise ValueError(f"transition {transition_id} has an unknown target")
        if has_dynamic_target:
            _path(transition["to_path"], f"transition {transition_id} to_path")

        conditions = transition.get("all", [])
        if not isinstance(conditions, list):
            raise ValueError(f"transition {transition_id} all must be an array")
        for condition in conditions:
            _validate_condition(condition, transition_id, workflow["policies"])
        if "increment" in transition:
            increment = transition["increment"]
            if not isinstance(increment, dict) or set(increment) != {"path", "by"}:
                raise ValueError(f"transition {transition_id} increment must contain path and by")
            _path(increment["path"], f"transition {transition_id} increment path")
            _positive_integer(increment["by"], f"transition {transition_id} increment by")
        if "set" in transition and (not isinstance(transition["set"], dict) or not transition["set"]):
            raise ValueError(f"transition {transition_id} set must be a non-empty object")


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
    matches: list[dict[str, Any]] = []
    for transition in workflow["transitions"]:
        if current_node not in transition["from"] or transition["event"] != event:
            continue
        packet_type = transition.get("packet_type")
        if packet_type:
            try:
                validate_packet(packet_type, context.get("packet"))
            except ValueError:
                continue
        if all(
            _condition_passes(condition, context, workflow.get("policies", {}))
            for condition in transition.get("all", [])
        ):
            matches.append(transition)
    return matches


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
