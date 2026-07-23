#!/usr/bin/env python3
"""Parse and validate versioned Gepetto delivery specifications."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SCHEMA_PATH = Path(__file__).parents[1] / "gepetto" / "references" / "delivery-spec.schema.json"
DELIVERY_SPEC_FENCE = re.compile(r"^```json[ \t]+delivery_spec[ \t]*$", re.MULTILINE)
DELIVERY_SPEC_BLOCK = re.compile(
    r"^```json[ \t]+delivery_spec[ \t]*\r?\n(?P<payload>.*?)\r?\n```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
CONTENT_REF = re.compile(r"sha256:[0-9a-f]{64}")
ISSUE_PATH = re.compile(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[1-9][0-9]*")
PATH_PREFIX = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*/?")


class _DuplicateKey(ValueError):
    pass


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _schema_error(path: str, message: str) -> None:
    raise ValueError(f"delivery_spec{path}: {message}")


def _resolve_reference(root: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise RuntimeError(f"unsupported delivery specification schema reference: {reference}")
    value: Any = root
    for segment in reference[2:].split("/"):
        if not isinstance(value, dict) or segment not in value:
            raise RuntimeError(f"broken delivery specification schema reference: {reference}")
        value = value[segment]
    if not isinstance(value, dict):
        raise RuntimeError(f"delivery specification schema reference is not an object: {reference}")
    return value


def _validate_schema(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str) -> None:
    if "$ref" in schema:
        _validate_schema(value, _resolve_reference(root, schema["$ref"]), root, path)
        return

    expected_type = schema.get("type")
    valid_type = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
    }.get(expected_type)
    if valid_type is False:
        _schema_error(path, f"must be a {expected_type}")
    if expected_type is not None and valid_type is None:
        raise RuntimeError(f"unsupported delivery specification schema type: {expected_type}")

    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        _schema_error(path, f"must equal {schema['const']!r}")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            _schema_error(path, "must not be empty")
        pattern = schema.get("pattern")
        if pattern is not None and re.fullmatch(pattern, value) is None:
            _schema_error(path, "has an invalid format")

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            _schema_error(path, "must not be empty")
        if schema.get("uniqueItems") is True:
            encoded = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(encoded) != len(set(encoded)):
                _schema_error(path, "must not contain duplicates")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                _validate_schema(item, item_schema, root, f"{path}[{index}]")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        missing = required - set(value)
        if missing:
            _schema_error(path, f"is missing required keys: {sorted(missing)}")
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                _schema_error(path, f"contains unknown keys: {sorted(unknown)}")
        for key, child in value.items():
            if key in properties:
                _validate_schema(child, properties[key], root, f"{path}.{key}")


def _require_nonblank(value: str, path: str) -> None:
    if not value.strip():
        _schema_error(path, "must contain non-whitespace text")


def _validate_issue_url(value: str, path: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or not ISSUE_PATH.fullmatch(parsed.path)
    ):
        _schema_error(path, "must be a raw live GitHub issue URL")


def _validate_path_prefix(value: str, path: str) -> None:
    if not PATH_PREFIX.fullmatch(value):
        _schema_error(path, "must be a relative POSIX path prefix")
    segments = value.rstrip("/").split("/")
    if any(segment in {".", ".."} for segment in segments):
        _schema_error(path, "must not contain dot segments")


def _validate_semantics(specification: dict[str, Any]) -> None:
    for key in ("intent", "observable_outcome"):
        _require_nonblank(specification[key], f".{key}")
    for key in ("non_goals", "invariants"):
        for index, item in enumerate(specification[key]):
            _require_nonblank(item, f".{key}[{index}]")

    for collection, fields in (
        ("architecture_decisions", ("domain", "decision")),
        ("decision_owners", ("domain", "owner", "constraint")),
        ("acceptance_criteria", ("id", "criterion")),
        ("external_effect_gates", ("effect", "authority")),
    ):
        for index, item in enumerate(specification[collection]):
            for field in fields:
                _require_nonblank(item[field], f".{collection}[{index}].{field}")

    leaf_ids: set[str] = set()
    issue_urls: set[str] = set()
    for index, leaf in enumerate(specification["leaves"]):
        leaf_id = leaf["id"]
        if leaf_id in leaf_ids:
            _schema_error(f".leaves[{index}].id", f"duplicates leaf id {leaf_id!r}")
        leaf_ids.add(leaf_id)
        _validate_issue_url(leaf["issue_url"], f".leaves[{index}].issue_url")
        if leaf["issue_url"] in issue_urls:
            _schema_error(f".leaves[{index}].issue_url", "duplicates another leaf issue URL")
        issue_urls.add(leaf["issue_url"])
        for key in ("owned_path_prefixes", "shared_paths"):
            for path_index, prefix in enumerate(leaf[key]):
                _validate_path_prefix(prefix, f".leaves[{index}].{key}[{path_index}]")

    graph: dict[str, list[str]] = {}
    for index, leaf in enumerate(specification["leaves"]):
        missing = set(leaf["dependencies"]) - leaf_ids
        if missing:
            _schema_error(
                f".leaves[{index}].dependencies",
                f"references missing leaf ids: {sorted(missing)}",
            )
        graph[leaf["id"]] = leaf["dependencies"]

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(leaf_id: str) -> None:
        if leaf_id in visiting:
            _schema_error(".leaves", f"dependency cycle includes {leaf_id!r}")
        if leaf_id in visited:
            return
        visiting.add(leaf_id)
        for dependency in graph[leaf_id]:
            visit(dependency)
        visiting.remove(leaf_id)
        visited.add(leaf_id)

    for leaf_id in graph:
        visit(leaf_id)

    criterion_ids: set[str] = set()
    for index, criterion in enumerate(specification["acceptance_criteria"]):
        if criterion["id"] in criterion_ids:
            _schema_error(
                f".acceptance_criteria[{index}].id",
                f"duplicates acceptance criterion id {criterion['id']!r}",
            )
        criterion_ids.add(criterion["id"])
        for validation_index, command in enumerate(criterion["validation"]):
            _require_nonblank(
                command,
                f".acceptance_criteria[{index}].validation[{validation_index}]",
            )


def load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        schema = json.load(handle, object_pairs_hook=_object_without_duplicate_keys)
    if not isinstance(schema, dict) or schema.get("$id") != "https://dylanmccavitt.github.io/skills/delivery-spec/v1.schema.json":
        raise RuntimeError("unsupported delivery specification schema")
    return schema


def parse_delivery_spec(artifact: str, schema_path: Path = SCHEMA_PATH) -> dict[str, Any]:
    openings = DELIVERY_SPEC_FENCE.findall(artifact)
    blocks = list(DELIVERY_SPEC_BLOCK.finditer(artifact))
    if len(openings) != 1 or len(blocks) != 1:
        raise ValueError("research artifact must contain exactly one readable json delivery_spec block")
    payload = blocks[0].group("payload")
    try:
        specification = json.loads(payload, object_pairs_hook=_object_without_duplicate_keys)
    except (json.JSONDecodeError, _DuplicateKey) as error:
        raise ValueError(f"invalid delivery_spec JSON: {error}") from error
    if not isinstance(specification, dict):
        raise ValueError("delivery_spec must be a JSON object")
    schema = load_schema(schema_path)
    _validate_schema(specification, schema, schema, "")
    _validate_semantics(specification)
    return specification


def canonical_delivery_spec_digest(specification: dict[str, Any]) -> str:
    payload = json.dumps(
        specification,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def validate_delivery_artifact_bytes(artifact_bytes: bytes) -> tuple[dict[str, Any], str]:
    try:
        artifact = artifact_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("research artifact must be valid UTF-8") from error
    specification = parse_delivery_spec(artifact)
    digest = canonical_delivery_spec_digest(specification)
    if not CONTENT_REF.fullmatch(digest):
        raise RuntimeError("canonical delivery specification digest is malformed")
    return specification, digest


def validate_delivery_packet_binding(
    specification: dict[str, Any], packet: dict[str, Any]
) -> None:
    """Bind the validated contract leaves to the research packet decision."""
    specification_urls = [leaf["issue_url"] for leaf in specification["leaves"]]
    packet_urls = packet["delivery_issue_urls"]
    if set(specification_urls) != set(packet_urls):
        raise ValueError(
            "delivery_spec leaf issue URLs must equal RESEARCH_PACKET delivery_issue_urls"
        )

    decision = packet["decision"]
    if decision in {"keep", "clarify"} and specification_urls != [packet["issue_url"]]:
        raise ValueError(
            f"delivery_spec for {decision} must contain the RESEARCH_PACKET source issue"
        )
    if decision == "split" and len(specification_urls) < 2:
        raise ValueError("delivery_spec for split must contain at least two leaves")
    if decision == "consolidate" and len(specification_urls) != 1:
        raise ValueError("delivery_spec for consolidate must contain exactly one leaf")


def validate_delivery_artifact(path: Path) -> tuple[dict[str, Any], str]:
    return validate_delivery_artifact_bytes(path.read_bytes())
