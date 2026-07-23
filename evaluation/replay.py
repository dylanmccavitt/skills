#!/usr/bin/env python3
"""Deterministically replay protocol traces against local Git workflow refs."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


WORKFLOW_PATH = "gepetto/references/workflow.json"
EVALUATOR_VERSION = 1
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
EVENT_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

WORKFLOW_KEYS = {
    "version",
    "workflow",
    "initial_node",
    "policies",
    "packet_types",
    "nodes",
    "transitions",
}
NODE_KEYS = {
    "owner",
    "lane_role",
    "fanout",
    "serial_per_pull_request",
    "dependency_ordered",
    "resumable",
    "terminal",
}
TRANSITION_KEYS = {
    "id",
    "from",
    "event",
    "to",
    "to_path",
    "packet_type",
    "all",
    "increment",
    "set",
}
CONDITION_OPERATORS = {
    "equals",
    "equals_path",
    "non_empty",
    "less_than_policy",
    "map_keys_equal_path",
    "values_full_sha",
    "content_ref",
}
DISPOSITIONS = {"accepted", "rejected", "unsupported", "error"}
COMPARABILITY_PATHS = (
    "suite.id",
    "suite.version",
    "suite.content_sha256",
    "fixture.id",
    "fixture.version",
    "fixture.content_sha256",
    "trace.id",
    "trace.version",
    "trace.content_sha256",
    "evaluator.commit_sha",
    "evaluator.version",
    "initial_repository_sha",
    "execution.kind",
    "execution.model",
    "execution.reasoning",
    "execution.time_budget_seconds",
    "execution.environment_contract_sha256",
    "execution.trial_id",
)


class ReplayError(ValueError):
    """A deterministic replay contract or execution failure."""


def _reject_constant(value: str) -> None:
    raise ReplayError(f"non-JSON numeric constant: {value}")


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReplayError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def loads_json(content: bytes | str, label: str) -> dict[str, Any]:
    try:
        if isinstance(content, bytes):
            content = content.decode("utf-8")
        value = json.loads(
            content,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ReplayError(f"{label}: invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ReplayError(f"{label}: document must be an object")
    return value


def load_json(path: Path) -> dict[str, Any]:
    try:
        return loads_json(path.read_bytes(), str(path))
    except OSError as error:
        raise ReplayError(f"{path}: cannot read JSON: {error}") from error


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def framed_digest(domain: str, value: Any) -> str:
    content = canonical_bytes(value)
    hasher = hashlib.sha256()
    hasher.update(domain.encode("ascii"))
    hasher.update(b"\x00")
    hasher.update(len(content).to_bytes(8, "big"))
    hasher.update(content)
    return "sha256:" + hasher.hexdigest()


def _exact_keys(
    value: Any,
    label: str,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReplayError(f"{label}: must be an object")
    optional = optional or set()
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise ReplayError(f"{label}: missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ReplayError(f"{label}: unknown fields: {', '.join(sorted(unknown))}")
    return value


def _nonblank(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReplayError(f"{label}: must be non-blank text")
    return value


def _identifier(value: Any, label: str) -> str:
    text = _nonblank(value, label)
    if not ID_RE.fullmatch(text):
        raise ReplayError(f"{label}: invalid identifier: {text!r}")
    return text


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise ReplayError(f"{label}: invalid full commit SHA")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise ReplayError(f"{label}: invalid SHA-256 content ref")
    return value


def _version(value: Any, label: str) -> int:
    if value != 1 or isinstance(value, bool):
        raise ReplayError(f"{label}: unsupported version: {value!r}")
    return 1


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ReplayError(f"{label}: must be an integer >= {minimum}")
    return value


def _dotted_path(value: Any, label: str) -> str:
    text = _nonblank(value, label)
    if any(not part for part in text.split(".")):
        raise ReplayError(f"{label}: invalid dotted path")
    return text


def _safe_artifact_path(value: Any, label: str) -> str:
    text = _nonblank(value, label)
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or "\\" in text
        or any(part in {"", ".", ".."} for part in path.parts)
        or len(path.parts) != 1
    ):
        raise ReplayError(f"{label}: unsafe artifact path: {text!r}")
    return text


def _validate_identity(value: Any, label: str) -> dict[str, Any]:
    item = _exact_keys(value, label, {"id", "version", "content_sha256"})
    _identifier(item["id"], f"{label}.id")
    _version(item["version"], f"{label}.version")
    _digest(item["content_sha256"], f"{label}.content_sha256")
    return item


def _validate_execution(value: Any, label: str) -> dict[str, Any]:
    item = _exact_keys(
        value,
        label,
        {
            "kind",
            "model",
            "reasoning",
            "time_budget_seconds",
            "environment_contract_sha256",
            "trial_id",
        },
    )
    if item["kind"] != "protocol-replay":
        raise ReplayError(f"{label}.kind: must be 'protocol-replay'")
    if item["model"] != "non-agent" or item["reasoning"] != "none":
        raise ReplayError(f"{label}: replay must use non-agent/none configuration")
    _integer(item["time_budget_seconds"], f"{label}.time_budget_seconds", 1)
    _digest(
        item["environment_contract_sha256"],
        f"{label}.environment_contract_sha256",
    )
    _identifier(item["trial_id"], f"{label}.trial_id")
    return item


def _validate_context_value(value: Any, label: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ReplayError(f"{label}: non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_context_value(item, f"{label}[{index}]")
        return
    if isinstance(value, dict):
        forbidden = {
            "argv",
            "command",
            "hostname",
            "password",
            "private_key",
            "secret",
            "timestamp",
            "token",
        }
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ReplayError(f"{label}: context keys must be non-blank text")
            if key.lower() in forbidden:
                raise ReplayError(f"{label}.{key}: forbidden replay context field")
            _validate_context_value(item, f"{label}.{key}")
        return
    raise ReplayError(f"{label}: unsupported context value")


def validate_trace(trace: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(
        trace,
        "trace",
        {
            "schema_version",
            "trace_id",
            "trace_version",
            "suite",
            "fixture",
            "initial_repository_sha",
            "execution",
            "initial_state",
            "events",
        },
    )
    _version(trace["schema_version"], "trace.schema_version")
    _identifier(trace["trace_id"], "trace.trace_id")
    _version(trace["trace_version"], "trace.trace_version")
    _validate_identity(trace["suite"], "trace.suite")
    _validate_identity(trace["fixture"], "trace.fixture")
    _sha(trace["initial_repository_sha"], "trace.initial_repository_sha")
    _validate_execution(trace["execution"], "trace.execution")
    initial = _exact_keys(trace["initial_state"], "trace.initial_state", {"node", "data"})
    if not isinstance(initial["node"], str) or not NODE_RE.fullmatch(initial["node"]):
        raise ReplayError("trace.initial_state.node: invalid node")
    if not isinstance(initial["data"], dict):
        raise ReplayError("trace.initial_state.data: must be an object")
    events = trace["events"]
    if not isinstance(events, list) or not events:
        raise ReplayError("trace.events: must be a non-empty array")
    seen: set[str] = set()
    for index, event in enumerate(events):
        label = f"trace.events[{index}]"
        item = _exact_keys(
            event,
            label,
            {"id", "event", "current_node", "context"},
            {"expected"},
        )
        event_id = _identifier(item["id"], f"{label}.id")
        if event_id in seen:
            raise ReplayError(f"{label}.id: duplicate event ID: {event_id}")
        seen.add(event_id)
        if not isinstance(item["event"], str) or not EVENT_RE.fullmatch(item["event"]):
            raise ReplayError(f"{label}.event: invalid event name")
        if not isinstance(item["current_node"], str) or not NODE_RE.fullmatch(
            item["current_node"]
        ):
            raise ReplayError(f"{label}.current_node: invalid node")
        if not isinstance(item["context"], dict):
            raise ReplayError(f"{label}.context: must be an object")
        _validate_context_value(item["context"], f"{label}.context")
        if "expected" in item:
            expected = _exact_keys(
                item["expected"],
                f"{label}.expected",
                {"disposition"},
                {"transition_id", "target_node", "error_code"},
            )
            if expected["disposition"] not in DISPOSITIONS:
                raise ReplayError(f"{label}.expected.disposition: invalid disposition")
            for key in ("transition_id", "target_node", "error_code"):
                if key in expected and expected[key] is not None:
                    _nonblank(expected[key], f"{label}.expected.{key}")
    return trace


def validate_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    unknown = workflow.keys() - WORKFLOW_KEYS
    required = {"version", "workflow", "initial_node", "policies", "nodes", "transitions"}
    missing = required - workflow.keys()
    if unknown or missing:
        detail = []
        if missing:
            detail.append(f"missing fields: {', '.join(sorted(missing))}")
        if unknown:
            detail.append(f"unsupported fields: {', '.join(sorted(unknown))}")
        raise ReplayError("workflow: " + "; ".join(detail))
    _version(workflow["version"], "workflow.version")
    _nonblank(workflow["workflow"], "workflow.workflow")
    if not isinstance(workflow["policies"], dict):
        raise ReplayError("workflow.policies: must be an object")
    nodes = workflow["nodes"]
    if not isinstance(nodes, dict) or not nodes:
        raise ReplayError("workflow.nodes: must be a non-empty object")
    for name, node in nodes.items():
        if not isinstance(name, str) or not NODE_RE.fullmatch(name):
            raise ReplayError(f"workflow.nodes: invalid node name: {name!r}")
        if not isinstance(node, dict):
            raise ReplayError(f"workflow.nodes.{name}: must be an object")
        node_unknown = node.keys() - NODE_KEYS
        if node_unknown:
            raise ReplayError(
                f"workflow.nodes.{name}: unsupported fields: "
                + ", ".join(sorted(node_unknown))
            )
    if workflow["initial_node"] not in nodes:
        raise ReplayError("workflow.initial_node: unknown node")
    if "packet_types" in workflow:
        packet_types = workflow["packet_types"]
        if (
            not isinstance(packet_types, list)
            or not all(isinstance(item, str) and item for item in packet_types)
            or len(packet_types) != len(set(packet_types))
        ):
            raise ReplayError("workflow.packet_types: invalid packet type list")
    transitions = workflow["transitions"]
    if not isinstance(transitions, list) or not transitions:
        raise ReplayError("workflow.transitions: must be a non-empty array")
    seen: set[str] = set()
    for index, transition in enumerate(transitions):
        label = f"workflow.transitions[{index}]"
        if not isinstance(transition, dict):
            raise ReplayError(f"{label}: must be an object")
        unknown_transition = transition.keys() - TRANSITION_KEYS
        if unknown_transition:
            raise ReplayError(
                f"{label}: unsupported fields: "
                + ", ".join(sorted(unknown_transition))
            )
        required_transition = {"id", "from", "event"}
        if required_transition - transition.keys():
            raise ReplayError(f"{label}: missing transition identity fields")
        transition_id = _identifier(transition["id"], f"{label}.id")
        if transition_id in seen:
            raise ReplayError(f"{label}.id: duplicate transition ID: {transition_id}")
        seen.add(transition_id)
        sources = transition["from"]
        if (
            not isinstance(sources, list)
            or not sources
            or not all(isinstance(item, str) and item in nodes for item in sources)
            or len(sources) != len(set(sources))
        ):
            raise ReplayError(f"{label}.from: invalid or duplicate source nodes")
        if not isinstance(transition["event"], str) or not EVENT_RE.fullmatch(
            transition["event"]
        ):
            raise ReplayError(f"{label}.event: invalid event")
        if ("to" in transition) == ("to_path" in transition):
            raise ReplayError(f"{label}: requires exactly one of to or to_path")
        if "to" in transition and transition["to"] not in nodes:
            raise ReplayError(f"{label}.to: unknown target node")
        if "to_path" in transition:
            _dotted_path(transition["to_path"], f"{label}.to_path")
        if "packet_type" in transition:
            if (
                transition["packet_type"] != transition["event"]
                or transition["packet_type"] not in workflow.get("packet_types", [])
            ):
                raise ReplayError(f"{label}.packet_type: inconsistent declaration")
        conditions = transition.get("all", [])
        if not isinstance(conditions, list):
            raise ReplayError(f"{label}.all: must be an array")
        for condition_index, condition in enumerate(conditions):
            condition_label = f"{label}.all[{condition_index}]"
            if not isinstance(condition, dict):
                raise ReplayError(f"{condition_label}: must be an object")
            unknown_condition = condition.keys() - ({"path"} | CONDITION_OPERATORS)
            operators = condition.keys() & CONDITION_OPERATORS
            if (
                unknown_condition
                or "path" not in condition
                or len(operators) != 1
            ):
                raise ReplayError(f"{condition_label}: unsupported condition vocabulary")
            _dotted_path(condition["path"], f"{condition_label}.path")
            operator = next(iter(operators))
            operand = condition[operator]
            if operator in {"equals_path", "map_keys_equal_path"}:
                _dotted_path(operand, f"{condition_label}.{operator}")
            elif operator == "less_than_policy":
                if (
                    not isinstance(operand, str)
                    or operand not in workflow["policies"]
                    or not isinstance(workflow["policies"][operand], (int, float))
                    or isinstance(workflow["policies"][operand], bool)
                ):
                    raise ReplayError(
                        f"{condition_label}: invalid numeric policy reference"
                    )
            elif operator in {"non_empty", "values_full_sha", "content_ref"}:
                if operand is not True:
                    raise ReplayError(f"{condition_label}.{operator}: must equal true")
        if "increment" in transition:
            increment = _exact_keys(
                transition["increment"],
                f"{label}.increment",
                {"path", "by"},
            )
            _dotted_path(increment["path"], f"{label}.increment.path")
            _integer(increment["by"], f"{label}.increment.by", 1)
        if "set" in transition:
            if not isinstance(transition["set"], dict) or not transition["set"]:
                raise ReplayError(f"{label}.set: must be a non-empty object")
            for path in transition["set"]:
                _dotted_path(path, f"{label}.set path")
    return workflow


def _lookup(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


def _assign(value: dict[str, Any], path: str, item: Any) -> None:
    segments = path.split(".")
    current = value
    for segment in segments[:-1]:
        child = current.get(segment)
        if child is None:
            child = {}
            current[segment] = child
        if not isinstance(child, dict):
            raise ReplayError(f"state path collides with non-object: {path}")
        current = child
    current[segments[-1]] = copy.deepcopy(item)


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
        limit = policies[condition["less_than_policy"]]
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value < limit
        )
    if "map_keys_equal_path" in condition:
        expected = _lookup(context, condition["map_keys_equal_path"])
        return (
            isinstance(value, dict)
            and isinstance(expected, list)
            and len(value) == len(expected)
            and set(value) == set(expected)
        )
    if condition.get("values_full_sha") is True:
        return (
            isinstance(value, dict)
            and all(
                isinstance(item, str) and SHA_RE.fullmatch(item)
                for item in value.values()
            )
        )
    if condition.get("content_ref") is True:
        return isinstance(value, str) and DIGEST_RE.fullmatch(value) is not None
    raise ReplayError(f"unsupported condition: {condition}")


def _state_digest(state: dict[str, Any]) -> str:
    return framed_digest("protocol-replay-state-v1", state)


def _event_context(state: dict[str, Any], supplied: dict[str, Any]) -> dict[str, Any]:
    context = copy.deepcopy(state["data"])
    for key, value in supplied.items():
        context[key] = copy.deepcopy(value)
    return context


def _event_record(
    run_id: str,
    sequence: int,
    event: dict[str, Any],
    state: dict[str, Any],
    workflow: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = state["node"]
    before = _state_digest(state)
    disposition = "error"
    transition_id: str | None = None
    target: str | None = None
    error_code: str | None = None
    next_state = copy.deepcopy(state)
    if event["current_node"] != source:
        error_code = "current-node-mismatch"
    else:
        candidates = [
            transition
            for transition in workflow["transitions"]
            if source in transition["from"] and transition["event"] == event["event"]
        ]
        if not candidates:
            disposition = "unsupported"
            error_code = "unsupported-event"
        else:
            context = _event_context(state, event["context"])
            eligible = [
                transition
                for transition in candidates
                if all(
                    _condition_passes(condition, context, workflow["policies"])
                    for condition in transition.get("all", [])
                )
            ]
            if not eligible:
                disposition = "rejected"
                error_code = "guard-rejected"
            elif len(eligible) > 1:
                error_code = "ambiguous-transition"
            else:
                transition = eligible[0]
                transition_id = transition["id"]
                target_value = (
                    transition.get("to")
                    if "to" in transition
                    else _lookup(context, transition["to_path"])
                )
                if target_value not in workflow["nodes"]:
                    error_code = "invalid-target"
                else:
                    target = target_value
                    try:
                        for path, value in transition.get("set", {}).items():
                            _assign(next_state["data"], path, value)
                        if "increment" in transition:
                            increment = transition["increment"]
                            current = _lookup(next_state["data"], increment["path"])
                            if current is None:
                                current = 0
                            if not isinstance(current, int) or isinstance(current, bool):
                                raise ReplayError("increment target is not an integer")
                            _assign(
                                next_state["data"],
                                increment["path"],
                                current + increment["by"],
                            )
                        next_state["node"] = target
                        disposition = "accepted"
                    except ReplayError:
                        next_state = copy.deepcopy(state)
                        target = None
                        error_code = "state-mutation-error"
    after = _state_digest(next_state)
    if disposition != "accepted" and after != before:
        raise ReplayError("non-accepted event mutated replay state")
    record = {
        "schema_version": 1,
        "run_id": run_id,
        "sequence": sequence,
        "input_event_id": event["id"],
        "source_node": source,
        "event": event["event"],
        "disposition": disposition,
        "transition_id": transition_id,
        "target_node": target,
        "error_code": error_code,
        "before_state_sha256": before,
        "after_state_sha256": after,
    }
    return record, next_state


def _assert_expected(event: dict[str, Any], record: dict[str, Any]) -> None:
    expected = event.get("expected")
    if not expected:
        return
    field_map = {
        "disposition": "disposition",
        "transition_id": "transition_id",
        "target_node": "target_node",
        "error_code": "error_code",
    }
    mismatches = [
        key
        for key, record_key in field_map.items()
        if key in expected and expected[key] != record[record_key]
    ]
    if mismatches:
        raise ReplayError(
            f"event {event['id']}: expected assertion mismatch: "
            + ", ".join(sorted(mismatches))
        )


def replay_events(
    workflow: dict[str, Any], trace: dict[str, Any], run_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    validate_workflow(workflow)
    validate_trace(trace)
    state = copy.deepcopy(trace["initial_state"])
    if state["node"] not in workflow["nodes"]:
        raise ReplayError("trace.initial_state.node: absent from workflow")
    initial_state = copy.deepcopy(state)
    records: list[dict[str, Any]] = []
    for sequence, event in enumerate(trace["events"]):
        record, state = _event_record(run_id, sequence, event, state, workflow)
        _assert_expected(event, record)
        records.append(record)
    return records, initial_state, state


def _run_git(repo: Path, arguments: list[str], *, binary: bool = False) -> bytes | str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            capture_output=True,
            text=not binary,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = ""
        if isinstance(error, subprocess.CalledProcessError):
            stderr = error.stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            detail = f": {str(stderr).strip()}"
        raise ReplayError(f"local git {' '.join(arguments)} failed{detail}") from error
    return completed.stdout


def resolve_ref(repo: Path, ref: str) -> str:
    _nonblank(ref, "workflow ref")
    if ref.startswith("-") or any(character in ref for character in "\x00\n\r"):
        raise ReplayError(f"workflow ref: unsafe ref: {ref!r}")
    if not SHA_RE.fullmatch(ref):
        matching = _run_git(
            repo,
            [
                "for-each-ref",
                "--format=%(refname)",
                f"refs/heads/{ref}",
                f"refs/tags/{ref}",
                f"refs/remotes/{ref}",
            ],
        )
        assert isinstance(matching, str)
        names = [line for line in matching.splitlines() if line]
        if len(names) > 1:
            raise ReplayError(
                f"workflow ref: ambiguous ref {ref!r}: {', '.join(sorted(names))}"
            )
    output = _run_git(
        repo,
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
    )
    assert isinstance(output, str)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 1 or not SHA_RE.fullmatch(lines[0]):
        raise ReplayError(f"workflow ref: ambiguous or non-commit ref: {ref!r}")
    return lines[0]


def load_workflow_ref(repo: Path, ref: str) -> tuple[str, bytes, dict[str, Any]]:
    commit = resolve_ref(repo, ref)
    content = _run_git(repo, ["show", f"{commit}:{WORKFLOW_PATH}"], binary=True)
    assert isinstance(content, bytes)
    workflow = loads_json(content, f"{ref}:{WORKFLOW_PATH}")
    validate_workflow(workflow)
    return commit, content, workflow


def verify_evaluator_commit(repo: Path, commit: str) -> None:
    committed = _run_git(repo, ["show", f"{commit}:evaluation/replay.py"], binary=True)
    assert isinstance(committed, bytes)
    try:
        running = Path(__file__).read_bytes()
    except OSError as error:
        raise ReplayError(f"cannot read running evaluator: {error}") from error
    if committed != running:
        raise ReplayError(
            "running evaluator bytes do not match evaluation/replay.py "
            f"at evaluator commit {commit}"
        )


def _manifest_for(
    *,
    trace: dict[str, Any],
    trace_sha256: str,
    requested_ref: str,
    workflow_commit: str,
    workflow_sha256: str,
    evaluator_commit: str,
) -> dict[str, Any]:
    identity = {
        "suite": copy.deepcopy(trace["suite"]),
        "fixture": copy.deepcopy(trace["fixture"]),
        "trace": {
            "id": trace["trace_id"],
            "version": trace["trace_version"],
            "content_sha256": trace_sha256,
        },
        "workflow": {
            "commit_sha": workflow_commit,
            "content_sha256": workflow_sha256,
        },
        "evaluator": {"commit_sha": evaluator_commit, "version": EVALUATOR_VERSION},
        "initial_repository_sha": trace["initial_repository_sha"],
        "execution": copy.deepcopy(trace["execution"]),
    }
    run_key = framed_digest("protocol-replay-run-key-v1", identity)
    run_id = "run-" + run_key[7:31]
    return {
        "schema_version": 1,
        "run_id": run_id,
        "run_key_sha256": run_key,
        "requested_ref": requested_ref,
        **identity,
        "artifacts": {"event_trace": "events.jsonl", "result": "result.json"},
    }


def _lookup_required(value: dict[str, Any], path: str) -> Any:
    result = _lookup(value, path)
    if result is None:
        raise ReplayError(f"manifest comparability key missing: {path}")
    return result


def comparability_mismatches(
    first: dict[str, Any], second: dict[str, Any]
) -> list[str]:
    return [
        path
        for path in COMPARABILITY_PATHS
        if _lookup_required(first, path) != _lookup_required(second, path)
    ]


def require_comparable(manifests: Iterable[dict[str, Any]]) -> None:
    items = list(manifests)
    if not items:
        raise ReplayError("comparability requires at least one manifest")
    first = items[0]
    for manifest in items[1:]:
        mismatches = comparability_mismatches(first, manifest)
        if mismatches:
            raise ReplayError(
                "runs are not comparable; mismatched keys: "
                + ", ".join(mismatches)
            )


def _jsonl_bytes(records: list[dict[str, Any]]) -> bytes:
    return b"".join(canonical_bytes(record) for record in records)


def _result_for(
    manifest: dict[str, Any],
    manifest_bytes: bytes,
    event_bytes: bytes,
    records: list[dict[str, Any]],
    initial_state: dict[str, Any],
    final_state: dict[str, Any],
) -> dict[str, Any]:
    counts = Counter(record["disposition"] for record in records)
    return {
        "schema_version": 1,
        "run_id": manifest["run_id"],
        "manifest_sha256": digest_bytes(manifest_bytes),
        "event_trace_sha256": digest_bytes(event_bytes),
        "event_count": len(records),
        "counts": {name: counts[name] for name in sorted(DISPOSITIONS)},
        "initial_state_sha256": _state_digest(initial_state),
        "final_state_sha256": _state_digest(final_state),
    }


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(
        manifest,
        "manifest",
        {
            "schema_version",
            "run_id",
            "run_key_sha256",
            "requested_ref",
            "suite",
            "fixture",
            "trace",
            "workflow",
            "evaluator",
            "initial_repository_sha",
            "execution",
            "artifacts",
        },
    )
    _version(manifest["schema_version"], "manifest.schema_version")
    if not isinstance(manifest["run_id"], str) or not re.fullmatch(
        r"run-[0-9a-f]{24}", manifest["run_id"]
    ):
        raise ReplayError("manifest.run_id: invalid run ID")
    _digest(manifest["run_key_sha256"], "manifest.run_key_sha256")
    _nonblank(manifest["requested_ref"], "manifest.requested_ref")
    _validate_identity(manifest["suite"], "manifest.suite")
    _validate_identity(manifest["fixture"], "manifest.fixture")
    _validate_identity(manifest["trace"], "manifest.trace")
    workflow = _exact_keys(
        manifest["workflow"],
        "manifest.workflow",
        {"commit_sha", "content_sha256"},
    )
    _sha(workflow["commit_sha"], "manifest.workflow.commit_sha")
    _digest(workflow["content_sha256"], "manifest.workflow.content_sha256")
    evaluator = _exact_keys(
        manifest["evaluator"], "manifest.evaluator", {"commit_sha", "version"}
    )
    _sha(evaluator["commit_sha"], "manifest.evaluator.commit_sha")
    _version(evaluator["version"], "manifest.evaluator.version")
    _sha(manifest["initial_repository_sha"], "manifest.initial_repository_sha")
    _validate_execution(manifest["execution"], "manifest.execution")
    artifacts = _exact_keys(
        manifest["artifacts"],
        "manifest.artifacts",
        {"event_trace", "result"},
    )
    if _safe_artifact_path(
        artifacts["event_trace"], "manifest.artifacts.event_trace"
    ) != "events.jsonl":
        raise ReplayError("manifest.artifacts.event_trace: unsupported filename")
    if _safe_artifact_path(
        artifacts["result"], "manifest.artifacts.result"
    ) != "result.json":
        raise ReplayError("manifest.artifacts.result: unsupported filename")
    identity = {
        "suite": manifest["suite"],
        "fixture": manifest["fixture"],
        "trace": manifest["trace"],
        "workflow": manifest["workflow"],
        "evaluator": manifest["evaluator"],
        "initial_repository_sha": manifest["initial_repository_sha"],
        "execution": manifest["execution"],
    }
    expected_run_key = framed_digest("protocol-replay-run-key-v1", identity)
    if manifest["run_key_sha256"] != expected_run_key:
        raise ReplayError("manifest.run_key_sha256: run identity digest mismatch")
    if manifest["run_id"] != "run-" + expected_run_key[7:31]:
        raise ReplayError("manifest.run_id: run identity binding mismatch")
    return manifest


def validate_event_records(
    records: list[dict[str, Any]], run_id: str
) -> list[dict[str, Any]]:
    if not records:
        raise ReplayError("event trace: must contain at least one record")
    previous_after: str | None = None
    for index, record in enumerate(records):
        label = f"event trace record {index}"
        _exact_keys(
            record,
            label,
            {
                "schema_version",
                "run_id",
                "sequence",
                "input_event_id",
                "source_node",
                "event",
                "disposition",
                "transition_id",
                "target_node",
                "error_code",
                "before_state_sha256",
                "after_state_sha256",
            },
        )
        _version(record["schema_version"], f"{label}.schema_version")
        if record["run_id"] != run_id:
            raise ReplayError(f"{label}.run_id: dangling manifest binding")
        if record["sequence"] != index:
            raise ReplayError(f"{label}.sequence: inconsistent sequence")
        _identifier(record["input_event_id"], f"{label}.input_event_id")
        if not isinstance(record["source_node"], str) or not NODE_RE.fullmatch(
            record["source_node"]
        ):
            raise ReplayError(f"{label}.source_node: invalid node")
        if not isinstance(record["event"], str) or not EVENT_RE.fullmatch(
            record["event"]
        ):
            raise ReplayError(f"{label}.event: invalid event")
        if record["disposition"] not in DISPOSITIONS:
            raise ReplayError(f"{label}.disposition: invalid disposition")
        for field in ("transition_id", "target_node", "error_code"):
            if record[field] is not None:
                _nonblank(record[field], f"{label}.{field}")
        if record["transition_id"] is not None:
            _identifier(record["transition_id"], f"{label}.transition_id")
        if record["target_node"] is not None and not NODE_RE.fullmatch(
            record["target_node"]
        ):
            raise ReplayError(f"{label}.target_node: invalid node")
        if record["error_code"] is not None:
            _identifier(record["error_code"], f"{label}.error_code")
        before = _digest(record["before_state_sha256"], f"{label}.before_state_sha256")
        after = _digest(record["after_state_sha256"], f"{label}.after_state_sha256")
        if previous_after is not None and before != previous_after:
            raise ReplayError(f"{label}: dangling state binding")
        previous_after = after
        accepted = record["disposition"] == "accepted"
        if accepted:
            if (
                record["transition_id"] is None
                or record["target_node"] is None
                or record["error_code"] is not None
            ):
                raise ReplayError(f"{label}: invalid accepted disposition fields")
        elif (
            record["target_node"] is not None
            or record["error_code"] is None
            or before != after
        ):
            raise ReplayError(f"{label}: invalid non-accepted state/disposition")
    return records


def parse_jsonl(content: bytes, label: str) -> list[dict[str, Any]]:
    if not content or not content.endswith(b"\n"):
        raise ReplayError(f"{label}: JSONL must end with one newline")
    records: list[dict[str, Any]] = []
    for index, line in enumerate(content.splitlines()):
        if not line:
            raise ReplayError(f"{label}: blank JSONL line at {index}")
        records.append(loads_json(line, f"{label} line {index}"))
    if _jsonl_bytes(records) != content:
        raise ReplayError(f"{label}: JSONL is not canonical")
    return records


def validate_result(
    result: dict[str, Any],
    manifest: dict[str, Any],
    records: list[dict[str, Any]],
    manifest_bytes: bytes,
    event_bytes: bytes,
) -> dict[str, Any]:
    _exact_keys(
        result,
        "result",
        {
            "schema_version",
            "run_id",
            "manifest_sha256",
            "event_trace_sha256",
            "event_count",
            "counts",
            "initial_state_sha256",
            "final_state_sha256",
        },
    )
    _version(result["schema_version"], "result.schema_version")
    if result["run_id"] != manifest["run_id"]:
        raise ReplayError("result.run_id: dangling manifest binding")
    if result["manifest_sha256"] != digest_bytes(manifest_bytes):
        raise ReplayError("result.manifest_sha256: manifest digest mismatch")
    if result["event_trace_sha256"] != digest_bytes(event_bytes):
        raise ReplayError("result.event_trace_sha256: event trace digest mismatch")
    if result["event_count"] != len(records):
        raise ReplayError("result.event_count: inconsistent event count")
    counts = _exact_keys(result["counts"], "result.counts", DISPOSITIONS)
    expected = Counter(record["disposition"] for record in records)
    for disposition in DISPOSITIONS:
        _integer(counts[disposition], f"result.counts.{disposition}")
        if counts[disposition] != expected[disposition]:
            raise ReplayError(
                f"result.counts.{disposition}: inconsistent disposition count"
            )
    if sum(counts.values()) != result["event_count"]:
        raise ReplayError("result.counts: inconsistent total")
    if result["initial_state_sha256"] != records[0]["before_state_sha256"]:
        raise ReplayError("result.initial_state_sha256: dangling state binding")
    if result["final_state_sha256"] != records[-1]["after_state_sha256"]:
        raise ReplayError("result.final_state_sha256: dangling state binding")
    return result


def validate_run_directory(run_dir: Path) -> None:
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise ReplayError(f"{run_dir}: run directory must be a real directory")
    expected_names = {"manifest.json", "events.jsonl", "result.json"}
    actual_names = {path.name for path in run_dir.iterdir()}
    if actual_names != expected_names:
        raise ReplayError(f"{run_dir}: missing or unexpected evidence artifacts")
    manifest_path = run_dir / "manifest.json"
    event_path = run_dir / "events.jsonl"
    result_path = run_dir / "result.json"
    manifest_bytes = manifest_path.read_bytes()
    event_bytes = event_path.read_bytes()
    result_bytes = result_path.read_bytes()
    manifest = loads_json(manifest_bytes, str(manifest_path))
    result = loads_json(result_bytes, str(result_path))
    if canonical_bytes(manifest) != manifest_bytes:
        raise ReplayError(f"{manifest_path}: manifest is not canonical")
    if canonical_bytes(result) != result_bytes:
        raise ReplayError(f"{result_path}: result is not canonical")
    validate_manifest(manifest)
    records = parse_jsonl(event_bytes, str(event_path))
    validate_event_records(records, manifest["run_id"])
    validate_result(result, manifest, records, manifest_bytes, event_bytes)


def _validate_output_root(path: Path) -> Path:
    if path.exists() and path.is_symlink():
        raise ReplayError(f"output path must not be a symlink: {path}")
    resolved = path.resolve()
    if resolved == Path(resolved.anchor):
        raise ReplayError("output path must not be a filesystem root")
    if resolved.name in {"", ".", "..", ".git"}:
        raise ReplayError(f"unsafe output path: {path}")
    if resolved.exists() and not resolved.is_dir():
        raise ReplayError(f"output path is not a directory: {path}")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _atomic_replace(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _persist_run(
    output_root: Path,
    manifest: dict[str, Any],
    event_bytes: bytes,
    result: dict[str, Any],
) -> Path:
    validate_manifest(manifest)
    run_dir = output_root / manifest["run_id"]
    if run_dir.exists() and (run_dir.is_symlink() or not run_dir.is_dir()):
        raise ReplayError(f"unsafe run directory: {run_dir}")
    run_dir.mkdir(mode=0o755, exist_ok=True)
    expected = {
        "manifest.json": canonical_bytes(manifest),
        "events.jsonl": event_bytes,
        "result.json": canonical_bytes(result),
    }
    unexpected = {path.name for path in run_dir.iterdir()} - expected.keys()
    if unexpected:
        raise ReplayError(
            f"refusing run directory with unexpected artifacts: "
            + ", ".join(sorted(unexpected))
        )
    for name, content in expected.items():
        path = run_dir / name
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise ReplayError(f"unsafe evidence path: {path}")
            if path.read_bytes() != content:
                raise ReplayError(
                    f"refusing to overwrite conflicting evidence: {path}"
                )
    for name, content in expected.items():
        path = run_dir / name
        if not path.exists():
            _atomic_replace(path, content)
    validate_run_directory(run_dir)
    return run_dir


def build_run(
    *,
    repo: Path,
    trace: dict[str, Any],
    trace_sha256: str,
    requested_ref: str,
    evaluator_commit: str,
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    workflow_commit, workflow_content, workflow = load_workflow_ref(
        repo, requested_ref
    )
    manifest = _manifest_for(
        trace=trace,
        trace_sha256=trace_sha256,
        requested_ref=requested_ref,
        workflow_commit=workflow_commit,
        workflow_sha256=digest_bytes(workflow_content),
        evaluator_commit=evaluator_commit,
    )
    records, initial_state, final_state = replay_events(
        workflow, trace, manifest["run_id"]
    )
    validate_event_records(records, manifest["run_id"])
    manifest_bytes = canonical_bytes(manifest)
    event_bytes = _jsonl_bytes(records)
    result = _result_for(
        manifest,
        manifest_bytes,
        event_bytes,
        records,
        initial_state,
        final_state,
    )
    validate_result(result, manifest, records, manifest_bytes, event_bytes)
    return manifest, event_bytes, result


def run_replay(
    repo: Path,
    trace_path: Path,
    output_path: Path,
    refs: list[str],
    evaluator_ref: str = "HEAD",
) -> list[Path]:
    repo = repo.resolve()
    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        raise ReplayError(f"repository path is not a Git worktree: {repo}")
    if not refs:
        raise ReplayError("at least one workflow ref is required")
    if len(refs) != len(set(refs)):
        raise ReplayError("workflow refs must not contain duplicates")
    trace = load_json(trace_path)
    validate_trace(trace)
    trace_content = canonical_bytes(trace)
    if trace_path.read_bytes() != trace_content:
        raise ReplayError(f"{trace_path}: trace must use canonical JSON bytes")
    trace_sha256 = digest_bytes(trace_content)
    evaluator_commit = resolve_ref(repo, evaluator_ref)
    verify_evaluator_commit(repo, evaluator_commit)
    planned = [
        build_run(
            repo=repo,
            trace=trace,
            trace_sha256=trace_sha256,
            requested_ref=ref,
            evaluator_commit=evaluator_commit,
        )
        for ref in refs
    ]
    require_comparable(item[0] for item in planned)
    output_root = _validate_output_root(output_path)
    return [
        _persist_run(output_root, manifest, events, result)
        for manifest, events, result in planned
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay one canonical trace against local Git workflow refs."
    )
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ref", action="append", dest="refs", required=True)
    parser.add_argument("--evaluator-ref", default="HEAD")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        paths = run_replay(
            arguments.repo,
            arguments.trace,
            arguments.output,
            arguments.refs,
            arguments.evaluator_ref,
        )
    except ReplayError as error:
        print(f"replay error: {error}", file=sys.stderr)
        return 2
    for path in paths:
        result = load_json(path / "result.json")
        counts = result["counts"]
        print(
            f"{path.name} {result['event_count']} events "
            + " ".join(f"{name}={counts[name]}" for name in sorted(counts))
            + f" path={path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
