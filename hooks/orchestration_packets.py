"""Strict, versioned packet contracts for orchestration task handoffs."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable
from urllib.parse import urlsplit


JsonObject = dict[str, Any]
Validator = Callable[[Any, str], None]

PACKET_VERSION = 1
FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
CONTENT_REF = re.compile(r"sha256:[0-9a-f]{64}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def _fail(path: str, message: str) -> None:
    raise ValueError(f"{path}: {message}")


def _object(
    value: Any,
    path: str,
    required: dict[str, Validator],
    optional: dict[str, Validator] | None = None,
) -> JsonObject:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    optional = optional or {}
    allowed = set(required) | set(optional)
    unknown = set(value) - allowed
    missing = set(required) - set(value)
    if unknown:
        _fail(path, f"contains unknown keys: {sorted(unknown)}")
    if missing:
        _fail(path, f"is missing required keys: {sorted(missing)}")
    for key, validator in required.items():
        validator(value[key], f"{path}.{key}")
    for key, validator in optional.items():
        if key in value:
            validator(value[key], f"{path}.{key}")
    return value


def _string(value: Any, path: str) -> None:
    if not isinstance(value, str) or not value.strip():
        _fail(path, "must be a non-empty string")


def _literal(expected: Any) -> Validator:
    def validate(value: Any, path: str) -> None:
        if value != expected or type(value) is not type(expected):
            _fail(path, f"must equal {expected!r}")
    return validate


def _enum(*values: str) -> Validator:
    def validate(value: Any, path: str) -> None:
        if value not in values or not isinstance(value, str):
            _fail(path, f"must be one of {list(values)}")
    return validate


def _boolean(value: Any, path: str) -> None:
    if not isinstance(value, bool):
        _fail(path, "must be a boolean")


def _integer_or_unknown(value: Any, path: str) -> None:
    if value == "unknown":
        return
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _fail(path, "must be a non-negative integer or 'unknown'")


def _bool_or_unknown(value: Any, path: str) -> None:
    if value != "unknown" and not isinstance(value, bool):
        _fail(path, "must be a boolean or 'unknown'")


def _url(value: Any, path: str) -> None:
    if not isinstance(value, str) or any(character in value for character in "[]<>\n\r\t"):
        _fail(path, "must be a raw URL")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or " " in value:
        _fail(path, "must be a raw HTTP(S) URL")


def _repository(value: Any, path: str) -> None:
    if not isinstance(value, str) or not REPOSITORY.fullmatch(value):
        _fail(path, "must be an owner/name repository")


def _sha(value: Any, path: str) -> None:
    if not isinstance(value, str) or not FULL_SHA.fullmatch(value):
        _fail(path, "must be a full 40-character SHA")


def _content_ref(value: Any, path: str) -> None:
    if not isinstance(value, str) or not CONTENT_REF.fullmatch(value):
        _fail(path, "must be a lowercase sha256: content reference")


def _absolute_path(value: Any, path: str) -> None:
    _string(value, path)
    if not os.path.isabs(value):
        _fail(path, "must be an absolute path")


def _nullable_string(value: Any, path: str) -> None:
    if value is not None:
        _string(value, path)


def _list_of(validator: Validator, *, non_empty: bool = False) -> Validator:
    def validate(value: Any, path: str) -> None:
        if not isinstance(value, list) or (non_empty and not value):
            _fail(path, "must be a non-empty array" if non_empty else "must be an array")
        for index, item in enumerate(value):
            validator(item, f"{path}[{index}]")
    return validate


def _artifact_location(value: Any, path: str) -> None:
    location = _object(
        value,
        path,
        {},
        {"issue_url": _url, "observed_updated_at": _string, "path": _absolute_path},
    )
    github_fields = {"issue_url", "observed_updated_at"}
    if set(location) not in (github_fields, {"path"}):
        _fail(path, "must contain either issue_url plus observed_updated_at, or path")


def _research_artifact(value: Any, path: str) -> None:
    artifact = _object(
        value,
        path,
        {
            "kind": _enum("github_issue", "tmp_markdown"),
            "status": _enum("persisted", "propose-only", "blocked"),
            "marker": _nullable_string,
            "content_ref": _content_ref,
            "locations": _list_of(_artifact_location, non_empty=True),
        },
    )
    if artifact["kind"] == "github_issue":
        if artifact["marker"] != "gepetto-research":
            _fail(f"{path}.marker", "must equal 'gepetto-research' for a GitHub artifact")
        if any("issue_url" not in location for location in artifact["locations"]):
            _fail(f"{path}.locations", "must contain only GitHub issue locations")
    else:
        if artifact["marker"] is not None:
            _fail(f"{path}.marker", "must be null for a temporary artifact")
        if any(set(location) != {"path"} for location in artifact["locations"]):
            _fail(f"{path}.locations", "must contain only absolute paths")


def _implementation_artifact(value: Any, path: str) -> None:
    artifact = _object(
        value,
        path,
        {
            "kind": _enum("github_issue", "tmp_markdown"),
            "status": _enum("persisted", "blocked"),
            "marker": _nullable_string,
            "content_ref": _content_ref,
        },
        {"issue_url": _url, "observed_updated_at": _string, "path": _absolute_path},
    )
    if artifact["kind"] == "github_issue":
        if artifact["marker"] != "gepetto-implementation":
            _fail(f"{path}.marker", "must equal 'gepetto-implementation' for a GitHub artifact")
        if set(artifact) != {
            "kind", "status", "marker", "content_ref", "issue_url", "observed_updated_at"
        }:
            _fail(path, "GitHub artifacts require issue_url and observed_updated_at only")
    else:
        if artifact["marker"] is not None:
            _fail(f"{path}.marker", "must be null for a temporary artifact")
        if set(artifact) != {"kind", "status", "marker", "content_ref", "path"}:
            _fail(path, "temporary artifacts require path only")


def _research_packet(value: Any, path: str) -> None:
    packet = _object(
        value,
        path,
        {
            "packet_version": _literal(PACKET_VERSION),
            "issue_url": _url,
            "repository": _repository,
            "base_sha": _sha,
            "issue_write_authority": _enum("persist", "propose-only"),
            "decision": _enum("keep", "split", "consolidate", "clarify", "block"),
            "delivery_issue_urls": _list_of(_url),
            "artifact": _research_artifact,
        },
    )
    count = len(packet["delivery_issue_urls"])
    if count != len(set(packet["delivery_issue_urls"])):
        _fail(f"{path}.delivery_issue_urls", "must not contain duplicates")
    expected = packet["decision"]
    if expected == "block" and count != 0:
        _fail(f"{path}.delivery_issue_urls", "must be empty for block")
    if expected == "split" and count < 2:
        _fail(f"{path}.delivery_issue_urls", "must contain at least two URLs for split")
    if expected in {"keep", "consolidate", "clarify"} and count != 1:
        _fail(f"{path}.delivery_issue_urls", f"must contain exactly one URL for {expected}")


def _implementation_packet(value: Any, path: str) -> None:
    _object(
        value,
        path,
        {
            "packet_version": _literal(PACKET_VERSION),
            "issue_url": _url,
            "task_role": _literal("pinocchio"),
            "pr_url": _url,
            "pr_head_sha": _sha,
            "artifact": _implementation_artifact,
        },
    )


def _finding(value: Any, path: str) -> None:
    _object(value, path, {
        "id": _string,
        "severity": _enum("critical", "high", "medium", "low"),
        "disposition": _enum("fixed", "blocked", "accepted-by-user"),
        "proof": _string,
    })


def _local_check(value: Any, path: str) -> None:
    _object(value, path, {"command": _string, "result": _enum("pass", "fail", "blocked")})


def _ci_check(value: Any, path: str) -> None:
    _object(value, path, {
        "name": _string,
        "conclusion": _enum("success", "failure", "pending", "skipped"),
    })


def _pr_state(value: Any, path: str) -> None:
    _object(value, path, {
        "draft": _boolean,
        "mergeable": _bool_or_unknown,
        "approvals_satisfied": _bool_or_unknown,
        "unresolved_required_threads": _integer_or_unknown,
    })


def _review_packet(value: Any, path: str) -> None:
    _object(value, path, {
        "packet_version": _literal(PACKET_VERSION),
        "issue_url": _url,
        "pr_url": _url,
        "reviewed_head_sha": _sha,
        "findings": _list_of(_finding),
        "local_checks": _list_of(_local_check, non_empty=True),
        "ci_checks": _list_of(_ci_check, non_empty=True),
        "pr_state": _pr_state,
        "blockers": _list_of(_string),
        "ready_for_jiminy": _boolean,
    })


def _artifact_locator(value: Any, path: str) -> None:
    artifact = _object(value, path, {"locator": _string, "content_ref": _content_ref})
    locator = artifact["locator"]
    if not os.path.isabs(locator):
        _url(locator, f"{path}.locator")


def _ready_gates(value: Any, path: str) -> None:
    _object(value, path, {
        "review_packet_verified": _boolean,
        "required_checks_green": _boolean,
        "approvals_satisfied": _bool_or_unknown,
        "unresolved_required_threads": _integer_or_unknown,
        "mergeable": _bool_or_unknown,
    })


def _ready_pull_request(value: Any, path: str) -> None:
    _object(value, path, {
        "issue_url": _url,
        "pr_url": _url,
        "branch": _string,
        "reviewed_head_sha": _sha,
        "reviewer_task_id": _string,
        "research_artifact": _artifact_locator,
        "implementation_artifact": _artifact_locator,
        "dependencies": _list_of(_url),
        "gates": _ready_gates,
    })


def _jiminy_ready(value: Any, path: str) -> None:
    packet = _object(value, path, {
        "packet_version": _literal(PACKET_VERSION),
        "coordinator_thread_id": _string,
        "repository": _repository,
        "merge_authority": _enum("merge", "monitoring-only"),
        "merge_order": _list_of(_url, non_empty=True),
        "expected_pr_urls": _list_of(_url, non_empty=True),
        "pull_requests": _list_of(_ready_pull_request, non_empty=True),
        "gepetto_merged": _literal(False),
    })
    pull_request_urls = [item["pr_url"] for item in packet["pull_requests"]]
    if len(pull_request_urls) != len(set(pull_request_urls)):
        _fail(f"{path}.pull_requests", "must not contain duplicate PR URLs")
    if packet["expected_pr_urls"] != pull_request_urls:
        _fail(f"{path}.expected_pr_urls", "must equal pull_requests in order")
    if (
        len(packet["merge_order"]) != len(pull_request_urls)
        or set(packet["merge_order"]) != set(pull_request_urls)
    ):
        _fail(f"{path}.merge_order", "must contain every pull request exactly once")


def _jiminy_pr_result(value: Any, path: str) -> None:
    _object(value, path, {
        "packet_version": _literal(PACKET_VERSION),
        "pr_url": _url,
        "state": _literal("MERGED"),
        "reviewed_head_sha": _sha,
        "merge_commit_sha": _sha,
        "linked_issue_url": _url,
        "linked_issue_state": _enum("OPEN", "CLOSED"),
    })


def _failed_check(value: Any, path: str) -> None:
    _object(value, path, {
        "name": _string,
        "result": _enum("failure", "blocked"),
        "evidence": _string,
    })


def _jiminy_integration_failed(value: Any, path: str) -> None:
    _object(value, path, {
        "packet_version": _literal(PACKET_VERSION),
        "coordinator_thread_id": _string,
        "repository": _repository,
        "default_branch": _string,
        "observed_head_sha": _sha,
        "expected_merge_commits": _list_of(_sha),
        "failed_checks": _list_of(_failed_check, non_empty=True),
        "remediation_required": _literal(True),
    })


def _complete_pull_request(value: Any, path: str) -> None:
    _object(value, path, {
        "pr_url": _url,
        "state": _literal("MERGED"),
        "merge_commit_sha": _sha,
    })


def _complete_integration(value: Any, path: str) -> None:
    _object(value, path, {
        "expected_merges_present": _literal(True),
        "required_checks_green": _literal(True),
        "linked_issues_verified": _literal(True),
        "runtime_ready_for_completion": _literal(True),
    })


def _jiminy_complete(value: Any, path: str) -> None:
    packet = _object(value, path, {
        "packet_version": _literal(PACKET_VERSION),
        "coordinator_thread_id": _string,
        "repository": _repository,
        "default_branch": _string,
        "verified_default_head_sha": _sha,
        "pull_requests": _list_of(_complete_pull_request),
        "integration": _complete_integration,
        "blockers": _list_of(_string),
        "private_log_path": _absolute_path,
    })
    if packet["blockers"]:
        _fail(f"{path}.blockers", "must be empty for JIMINY_COMPLETE")


PACKET_VALIDATORS: dict[str, Validator] = {
    "RESEARCH_PACKET": _research_packet,
    "IMPLEMENTATION_PACKET": _implementation_packet,
    "REVIEW_PACKET": _review_packet,
    "JIMINY_READY": _jiminy_ready,
    "JIMINY_PR_RESULT": _jiminy_pr_result,
    "JIMINY_INTEGRATION_FAILED": _jiminy_integration_failed,
    "JIMINY_COMPLETE": _jiminy_complete,
}
PACKET_TYPES = frozenset(PACKET_VALIDATORS)
TERMINAL_PACKET_TYPES = frozenset({
    "RESEARCH_PACKET", "IMPLEMENTATION_PACKET", "REVIEW_PACKET", "JIMINY_COMPLETE"
})


def validate_packet(packet_type: str, payload: Any) -> JsonObject:
    validator = PACKET_VALIDATORS.get(packet_type)
    if validator is None:
        raise ValueError(f"unknown packet type: {packet_type}")
    validator(payload, packet_type)
    return payload


def parse_packet_message(message: Any, expected_type: str | None = None) -> tuple[str, JsonObject]:
    if not isinstance(message, str):
        raise ValueError("packet message must be text")
    lines = message.splitlines()
    first_index = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first_index is None:
        raise ValueError("packet message is empty")
    header = lines[first_index].strip()
    if not header.endswith(":"):
        raise ValueError("packet message must begin with a packet header")
    packet_type = header[:-1]
    if packet_type not in PACKET_TYPES:
        raise ValueError(f"unknown packet type: {packet_type}")
    if expected_type is not None and packet_type != expected_type:
        raise ValueError(f"expected {expected_type}, got {packet_type}")
    for line in lines[first_index + 1:]:
        if line.strip().endswith(":") and line.strip()[:-1] in PACKET_TYPES:
            raise ValueError("packet message contains more than one packet header")
    payload_text = "\n".join(lines[first_index + 1:]).strip()
    if not payload_text:
        raise ValueError("packet message has no payload")
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"packet payload must be valid JSON: {error.msg}") from error
    return packet_type, validate_packet(packet_type, payload)
