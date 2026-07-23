#!/usr/bin/env python3
"""Validate immutable evaluation fixtures with Python's standard library."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parent
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FORBIDDEN_PUBLIC_TEXT = (
    "grader_tools/",
    "grader_tests.",
    "private rubric",
    "expected observation",
    "seeded-defect inventory",
)


class ContractError(ValueError):
    """A deterministic evaluation contract failure."""


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContractError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_pairs_no_duplicates,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ContractError(f"{path}: invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ContractError(f"{path}: document must be an object")
    return value


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def digest_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def document_digest(value: Any) -> str:
    return digest_bytes(canonical_bytes(value))


def _safe_relative_path(value: Any, label: str) -> PurePosixPath:
    text = _nonblank(value, label)
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or "\\" in text
        or text in {".", ".."}
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ContractError(f"{label}: unsafe or escaping path: {text!r}")
    return path


def _nonblank(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label}: must be non-blank text")
    return value


def _identifier(value: Any, label: str) -> str:
    text = _nonblank(value, label)
    if not ID_RE.fullmatch(text):
        raise ContractError(f"{label}: invalid identifier: {text!r}")
    return text


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise ContractError(f"{label}: invalid SHA-256 digest")
    return value


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractError(f"{label}: must be an integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"{label}: must be at least {minimum}")
    return value


def _exact_keys(
    value: Any, label: str, required: set[str], optional: set[str] | None = None
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label}: must be an object")
    optional = optional or set()
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise ContractError(f"{label}: missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ContractError(f"{label}: unknown fields: {', '.join(sorted(unknown))}")
    return value


def _version_one(value: Any, label: str) -> None:
    if value != 1 or isinstance(value, bool):
        raise ContractError(f"{label}: unsupported version: {value!r}")


def tree_digest(root: Path) -> str:
    if not root.is_dir() or root.is_symlink():
        raise ContractError(f"{root}: tree root must be a real directory")
    entries: list[tuple[str, bytes]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        _safe_relative_path(relative, f"{root} asset")
        if path.is_symlink():
            raise ContractError(f"{path}: symlinks are not allowed")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ContractError(f"{path}: asset must be a regular file")
        entries.append((relative, path.read_bytes()))
    if not entries:
        raise ContractError(f"{root}: asset tree must not be empty")
    hasher = hashlib.sha256()
    hasher.update(b"evaluation-tree-v1\x00")
    hasher.update(len(entries).to_bytes(8, "big"))
    for relative, content in entries:
        path_bytes = relative.encode("utf-8")
        hasher.update(len(path_bytes).to_bytes(8, "big"))
        hasher.update(path_bytes)
        hasher.update(len(content).to_bytes(8, "big"))
        hasher.update(content)
    return "sha256:" + hasher.hexdigest()


def public_fixture_digest(
    fixture_id: str,
    fixture_version: int,
    public_payload_tree_sha256: str,
    seed_repository_tree_sha256: str,
) -> str:
    return document_digest(
        {
            "fixture_id": fixture_id,
            "fixture_version": fixture_version,
            "public_payload_tree_sha256": public_payload_tree_sha256,
            "seed_repository_tree_sha256": seed_repository_tree_sha256,
        }
    )


def validate_suite_shape(value: dict[str, Any]) -> list[dict[str, Any]]:
    _exact_keys(
        value,
        "suite",
        {"schema_version", "suite_id", "suite_version", "fixtures"},
    )
    _version_one(value["schema_version"], "suite.schema_version")
    _identifier(value["suite_id"], "suite.suite_id")
    _version_one(value["suite_version"], "suite.suite_version")
    fixtures = value["fixtures"]
    if not isinstance(fixtures, list) or not fixtures:
        raise ContractError("suite.fixtures: must be a non-empty array")
    seen: set[str] = set()
    for index, entry in enumerate(fixtures):
        label = f"suite.fixtures[{index}]"
        _exact_keys(
            entry,
            label,
            {
                "fixture_id",
                "fixture_version",
                "manifest",
                "public_manifest_sha256",
                "public_payload_tree_sha256",
                "seed_repository_tree_sha256",
                "grader_contract_sha256",
            },
        )
        fixture_id = _identifier(entry["fixture_id"], f"{label}.fixture_id")
        if fixture_id in seen:
            raise ContractError(f"{label}: duplicate fixture ID: {fixture_id}")
        seen.add(fixture_id)
        _version_one(entry["fixture_version"], f"{label}.fixture_version")
        _safe_relative_path(entry["manifest"], f"{label}.manifest")
        for field in (
            "public_manifest_sha256",
            "public_payload_tree_sha256",
            "seed_repository_tree_sha256",
            "grader_contract_sha256",
        ):
            _digest(entry[field], f"{label}.{field}")
    expected = {"low-risk-existing-tests-v1", "seeded-review-defects-v1"}
    if seen != expected:
        raise ContractError(
            "suite.fixtures: version 1 must contain exactly "
            + ", ".join(sorted(expected))
        )
    return fixtures


def validate_manifest_shape(value: dict[str, Any], label: str) -> None:
    _exact_keys(
        value,
        label,
        {
            "schema_version",
            "fixture_id",
            "fixture_version",
            "scenario_kind",
            "prompt",
            "acceptance_criteria",
            "seed_repository",
            "initial_repository",
            "grader",
        },
    )
    _version_one(value["schema_version"], f"{label}.schema_version")
    _identifier(value["fixture_id"], f"{label}.fixture_id")
    _version_one(value["fixture_version"], f"{label}.fixture_version")
    if value["scenario_kind"] not in {
        "low_risk_existing_tests",
        "seeded_review_defects",
    }:
        raise ContractError(f"{label}.scenario_kind: unsupported scenario kind")
    for field in ("prompt", "acceptance_criteria", "seed_repository"):
        _safe_relative_path(value[field], f"{label}.{field}")
    initial = _exact_keys(
        value["initial_repository"],
        f"{label}.initial_repository",
        {"default_branch", "initial_ref", "clean_worktree"},
    )
    _nonblank(initial["default_branch"], f"{label}.initial_repository.default_branch")
    _nonblank(initial["initial_ref"], f"{label}.initial_repository.initial_ref")
    if initial["clean_worktree"] is not True:
        raise ContractError(f"{label}.initial_repository.clean_worktree: must be true")
    grader = _exact_keys(
        value["grader"], f"{label}.grader", {"id", "version", "digest"}
    )
    _identifier(grader["id"], f"{label}.grader.id")
    _version_one(grader["version"], f"{label}.grader.version")
    _digest(grader["digest"], f"{label}.grader.digest")


def validate_grader_shape(value: dict[str, Any], label: str) -> None:
    _exact_keys(
        value,
        label,
        {
            "schema_version",
            "grader_id",
            "grader_version",
            "fixture_id",
            "fixture_version",
            "public_fixture_sha256",
            "checks",
        },
    )
    _version_one(value["schema_version"], f"{label}.schema_version")
    _identifier(value["grader_id"], f"{label}.grader_id")
    _version_one(value["grader_version"], f"{label}.grader_version")
    _identifier(value["fixture_id"], f"{label}.fixture_id")
    _version_one(value["fixture_version"], f"{label}.fixture_version")
    _digest(value["public_fixture_sha256"], f"{label}.public_fixture_sha256")
    checks = value["checks"]
    if not isinstance(checks, list) or not checks:
        raise ContractError(f"{label}.checks: must be a non-empty array")
    seen: set[str] = set()
    categories: set[str] = set()
    for index, check in enumerate(checks):
        check_label = f"{label}.checks[{index}]"
        _exact_keys(
            check,
            check_label,
            {"id", "category", "required", "execution", "expected"},
        )
        check_id = _identifier(check["id"], f"{check_label}.id")
        if check_id in seen:
            raise ContractError(f"{check_label}: duplicate check ID: {check_id}")
        seen.add(check_id)
        if check["category"] not in {
            "functional_outcome",
            "contract_fidelity",
            "seeded_defect_detection",
        }:
            raise ContractError(f"{check_label}.category: unsupported category")
        categories.add(check["category"])
        if not isinstance(check["required"], bool):
            raise ContractError(f"{check_label}.required: must be boolean")
        execution = _exact_keys(
            check["execution"],
            f"{check_label}.execution",
            {"kind", "argv", "timeout_seconds"},
        )
        if execution["kind"] != "process":
            raise ContractError(f"{check_label}.execution.kind: unsupported kind")
        argv = execution["argv"]
        if not isinstance(argv, list) or not argv:
            raise ContractError(f"{check_label}.execution.argv: non-empty array required")
        for arg_index, arg in enumerate(argv):
            _nonblank(arg, f"{check_label}.execution.argv[{arg_index}]")
        _integer(
            execution["timeout_seconds"],
            f"{check_label}.execution.timeout_seconds",
            minimum=1,
        )
        expected = _exact_keys(
            check["expected"],
            f"{check_label}.expected",
            {"observation", "value"},
        )
        _nonblank(expected["observation"], f"{check_label}.expected.observation")
    required_categories = {"contract_fidelity"}
    if value["fixture_id"] == "low-risk-existing-tests-v1":
        required_categories.add("functional_outcome")
    if value["fixture_id"] == "seeded-review-defects-v1":
        required_categories.add("seeded_defect_detection")
    if not required_categories <= categories:
        raise ContractError(f"{label}.checks: missing required grading categories")


def _assert_public_boundary(public_root: Path) -> None:
    for path in public_root.rglob("*"):
        relative = path.relative_to(public_root).as_posix().lower()
        if "grader" in PurePosixPath(relative).parts:
            raise ContractError(f"{path}: held-out grader path leaked into public tree")
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8").lower()
            except UnicodeDecodeError:
                continue
            for forbidden in FORBIDDEN_PUBLIC_TEXT:
                if forbidden in text:
                    raise ContractError(
                        f"{path}: held-out grader detail leaked into public content"
                    )


def _validate_schema_documents(root: Path) -> None:
    schema_root = root / "schemas"
    expected = {
        "suite-v1.schema.json",
        "fixture-v1.schema.json",
        "grader-v1.schema.json",
    }
    actual = {
        path.name for path in schema_root.iterdir() if path.is_file()
    } if schema_root.is_dir() else set()
    if actual != expected:
        raise ContractError("schemas: missing or extra version 1 schema documents")
    for name in sorted(expected):
        schema = load_json(schema_root / name)
        _nonblank(schema.get("$schema"), f"{name}.$schema")
        _nonblank(schema.get("$id"), f"{name}.$id")
        _nonblank(schema.get("title"), f"{name}.title")
        if schema.get("type") != "object":
            raise ContractError(f"{name}: root schema type must be object")
        if schema.get("additionalProperties") is not False:
            raise ContractError(f"{name}: root schema must reject unknown fields")
        required = schema.get("required")
        properties = schema.get("properties")
        if not isinstance(required, list) or not required:
            raise ContractError(f"{name}: required fields must be declared")
        if not isinstance(properties, dict) or set(required) - properties.keys():
            raise ContractError(f"{name}: required fields must have schemas")


def validate(root: Path = ROOT) -> dict[str, str]:
    _validate_schema_documents(root)
    suite_path = root / "suite-v1.json"
    suite = load_json(suite_path)
    entries = validate_suite_shape(suite)
    fixture_root = root / "fixtures"
    indexed_ids = {entry["fixture_id"] for entry in entries}
    actual_ids = {
        path.name for path in fixture_root.iterdir() if path.is_dir()
    } if fixture_root.is_dir() else set()
    if actual_ids != indexed_ids:
        raise ContractError("fixtures: missing, extra, or dangling fixture directories")

    for entry in entries:
        fixture_id = entry["fixture_id"]
        fixture_dir = fixture_root / fixture_id
        if set(path.name for path in fixture_dir.iterdir()) != {"public", "grader"}:
            raise ContractError(f"{fixture_id}: missing or extra fixture assets")
        expected_manifest = (
            Path("fixtures") / fixture_id / "public" / "manifest-v1.json"
        ).as_posix()
        if entry["manifest"] != expected_manifest:
            raise ContractError(f"{fixture_id}: dangling manifest reference")
        manifest_path = root / entry["manifest"]
        public_root = fixture_dir / "public"
        payload_root = public_root / "payload"
        seed_root = payload_root / "seed"
        grader_dir = fixture_dir / "grader"
        grader_path = grader_dir / "grader-v1.json"
        if set(path.name for path in public_root.iterdir()) != {
            "manifest-v1.json",
            "payload",
        }:
            raise ContractError(f"{fixture_id}: missing or extra public assets")
        if set(path.name for path in grader_dir.iterdir()) != {"grader-v1.json"}:
            raise ContractError(f"{fixture_id}: missing or extra grader assets")

        manifest = load_json(manifest_path)
        grader = load_json(grader_path)
        validate_manifest_shape(manifest, f"{fixture_id}.manifest")
        validate_grader_shape(grader, f"{fixture_id}.grader")
        if manifest["fixture_id"] != fixture_id or grader["fixture_id"] != fixture_id:
            raise ContractError(f"{fixture_id}: fixture identity binding drift")
        if manifest["fixture_version"] != entry["fixture_version"]:
            raise ContractError(f"{fixture_id}: fixture version binding drift")
        if grader["fixture_version"] != entry["fixture_version"]:
            raise ContractError(f"{fixture_id}: grader fixture version binding drift")
        if (
            manifest["grader"]["id"] != grader["grader_id"]
            or manifest["grader"]["version"] != grader["grader_version"]
        ):
            raise ContractError(f"{fixture_id}: dangling grader reference")

        for field in ("prompt", "acceptance_criteria"):
            asset = public_root / manifest[field]
            if not asset.is_file():
                raise ContractError(f"{fixture_id}: missing {field} asset")
            _nonblank(asset.read_text(encoding="utf-8"), f"{fixture_id}.{field}")
        if public_root / manifest["seed_repository"] != seed_root:
            raise ContractError(f"{fixture_id}: seed repository reference drift")
        _assert_public_boundary(public_root)

        manifest_digest = document_digest(manifest)
        payload_digest = tree_digest(payload_root)
        seed_digest = tree_digest(seed_root)
        fixture_digest = public_fixture_digest(
            fixture_id, entry["fixture_version"], payload_digest, seed_digest
        )
        grader_digest = document_digest(grader)
        expected_digests = {
            "public_manifest_sha256": manifest_digest,
            "public_payload_tree_sha256": payload_digest,
            "seed_repository_tree_sha256": seed_digest,
            "grader_contract_sha256": grader_digest,
        }
        for field, observed in expected_digests.items():
            if entry[field] != observed:
                raise ContractError(f"{fixture_id}: {field} tampering or binding drift")
        if grader["public_fixture_sha256"] != fixture_digest:
            raise ContractError(f"{fixture_id}: public/grader binding drift")
        if manifest["grader"]["digest"] != grader_digest:
            raise ContractError(f"{fixture_id}: grader digest binding drift")

    return {
        "suite": document_digest(suite),
        "fixtures": str(len(entries)),
    }


def main() -> int:
    try:
        result = validate()
    except ContractError as error:
        print(f"evaluation validation failed: {error}", file=sys.stderr)
        return 1
    print(
        f"evaluation validation passed: {result['fixtures']} fixtures, "
        f"suite {result['suite']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
