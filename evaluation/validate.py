#!/usr/bin/env python3
"""Validate immutable evaluation fixtures with Python's standard library."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import replay
import compare


ROOT = Path(__file__).resolve().parent
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FORBIDDEN_PUBLIC_TEXT = (
    "grader_tools/",
    "grader_tests.",
    "private rubric",
    "rubric:",
    "defect inventory",
    "seeded defect inventory",
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


def _reject_non_json_constant(value: str) -> None:
    raise ContractError(f"non-JSON numeric constant: {value}")


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_non_json_constant,
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


def _version(value: Any, label: str, *, supported: set[int]) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value not in supported
    ):
        raise ContractError(f"{label}: unsupported version: {value!r}")
    return value


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


def validate_suite_shape(
    value: dict[str, Any], *, expected_version: int
) -> list[dict[str, Any]]:
    _exact_keys(
        value,
        "suite",
        {"schema_version", "suite_id", "suite_version", "fixtures"},
    )
    _version(
        value["schema_version"],
        "suite.schema_version",
        supported={expected_version},
    )
    _identifier(value["suite_id"], "suite.suite_id")
    if value["suite_id"] != "orchestration-baselines":
        raise ContractError("suite.suite_id: unsupported suite identity")
    if value["suite_version"] != expected_version:
        raise ContractError(
            "suite.suite_version: must match the schema and index version"
        )
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
        fixture_version = _version(
            entry["fixture_version"],
            f"{label}.fixture_version",
            supported={1, 2},
        )
        manifest = _safe_relative_path(entry["manifest"], f"{label}.manifest")
        expected_manifest = (
            PurePosixPath("fixtures")
            / fixture_id
            / "public"
            / f"manifest-v{fixture_version}.json"
        )
        if manifest != expected_manifest:
            raise ContractError(f"{label}.manifest: identity/version mismatch")
        for field in (
            "public_manifest_sha256",
            "public_payload_tree_sha256",
            "seed_repository_tree_sha256",
            "grader_contract_sha256",
        ):
            _digest(entry[field], f"{label}.{field}")
    expected = {
        "low-risk-existing-tests-v1": 1,
        "seeded-review-defects-v1": 1,
    }
    if expected_version == 2:
        expected["checkpoint-continuation-v2"] = 2
    if seen != set(expected):
        raise ContractError(
            f"suite.fixtures: version {expected_version} must contain exactly "
            + ", ".join(sorted(expected))
        )
    observed_versions = {
        entry["fixture_id"]: entry["fixture_version"] for entry in fixtures
    }
    if observed_versions != expected:
        raise ContractError(
            f"suite.fixtures: invalid version {expected_version} fixture identities"
        )
    if [entry["fixture_id"] for entry in fixtures] != list(expected):
        raise ContractError(
            f"suite.fixtures: invalid version {expected_version} fixture order"
        )
    return fixtures


def validate_manifest_shape(
    value: dict[str, Any], label: str, *, expected_version: int
) -> None:
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
    _version(
        value["schema_version"],
        f"{label}.schema_version",
        supported={expected_version},
    )
    _identifier(value["fixture_id"], f"{label}.fixture_id")
    if value["fixture_version"] != expected_version:
        raise ContractError(f"{label}.fixture_version: identity/version mismatch")
    scenario_by_version = {
        1: {"low_risk_existing_tests", "seeded_review_defects"},
        2: {"checkpoint_continuation"},
    }
    if value["scenario_kind"] not in scenario_by_version[expected_version]:
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
    if grader["version"] != expected_version:
        raise ContractError(f"{label}.grader.version: identity/version mismatch")
    _digest(grader["digest"], f"{label}.grader.digest")


def validate_grader_shape(
    value: dict[str, Any], label: str, *, expected_version: int
) -> None:
    required = {
        "schema_version",
        "grader_id",
        "grader_version",
        "fixture_id",
        "fixture_version",
        "public_fixture_sha256",
        "checks",
    }
    if expected_version == 2:
        required.update({"private_assets_tree_sha256", "private_expectations"})
    _exact_keys(
        value,
        label,
        required,
    )
    _version(
        value["schema_version"],
        f"{label}.schema_version",
        supported={expected_version},
    )
    _identifier(value["grader_id"], f"{label}.grader_id")
    if value["grader_version"] != expected_version:
        raise ContractError(f"{label}.grader_version: identity/version mismatch")
    _identifier(value["fixture_id"], f"{label}.fixture_id")
    if value["fixture_version"] != expected_version:
        raise ContractError(f"{label}.fixture_version: identity/version mismatch")
    _digest(value["public_fixture_sha256"], f"{label}.public_fixture_sha256")
    if expected_version == 2:
        _digest(
            value["private_assets_tree_sha256"],
            f"{label}.private_assets_tree_sha256",
        )
        expectations = _exact_keys(
            value["private_expectations"],
            f"{label}.private_expectations",
            {"sensitive_strings"},
        )
        sensitive_strings = expectations["sensitive_strings"]
        if not isinstance(sensitive_strings, list) or not sensitive_strings:
            raise ContractError(
                f"{label}.private_expectations.sensitive_strings: "
                "non-empty array required"
            )
        normalized_sensitive = [
            _nonblank(
                item,
                f"{label}.private_expectations.sensitive_strings[{index}]",
            ).lower()
            for index, item in enumerate(sensitive_strings)
        ]
        if len(set(normalized_sensitive)) != len(normalized_sensitive):
            raise ContractError(
                f"{label}.private_expectations.sensitive_strings: "
                "duplicate value"
            )
    checks = value["checks"]
    if not isinstance(checks, list) or not checks:
        raise ContractError(f"{label}.checks: must be a non-empty array")
    seen: set[str] = set()
    required_categories: set[str] = set()
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
        if not isinstance(check["required"], bool):
            raise ContractError(f"{check_label}.required: must be boolean")
        if check["required"]:
            required_categories.add(check["category"])
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
    expected_required_categories = {"contract_fidelity"}
    if value["fixture_id"] == "low-risk-existing-tests-v1":
        expected_required_categories.add("functional_outcome")
    if value["fixture_id"] == "seeded-review-defects-v1":
        expected_required_categories.add("seeded_defect_detection")
    if value["fixture_id"] == "checkpoint-continuation-v2":
        expected_required_categories.add("functional_outcome")
    if not expected_required_categories <= required_categories:
        raise ContractError(f"{label}.checks: missing required grading categories")


def _grader_sensitive_text(grader: dict[str, Any]) -> set[str]:
    sensitive = set(FORBIDDEN_PUBLIC_TEXT)
    private_expectations = grader.get("private_expectations")
    if private_expectations:
        sensitive.update(
            text.strip().lower()
            for text in private_expectations["sensitive_strings"]
        )
    for check in grader["checks"]:
        sensitive.add(check["id"].lower())
        sensitive.add(check["expected"]["observation"].lower())
        sensitive.update(_nested_sensitive_text(check["expected"]["value"]))
        argv = check["execution"]["argv"]
        sensitive.add(" ".join(argv).lower())
        sensitive.update(
            arg.lower()
            for arg in argv
            if "/" in arg or "\\" in arg or "grader" in arg.lower()
        )
    return sensitive


def _nested_sensitive_text(value: Any) -> set[str]:
    sensitive: set[str] = set()
    if isinstance(value, str):
        text = value.strip().lower()
        if len(text) >= 8:
            sensitive.add(text)
    elif isinstance(value, list):
        for item in value:
            sensitive.update(_nested_sensitive_text(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            sensitive.update(_nested_sensitive_text(key))
            sensitive.update(_nested_sensitive_text(item))
    return sensitive


def _leakage_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[^\W_]+", value.lower()))


def _contains_sensitive_text(value: str, sensitive: set[str]) -> bool:
    value_tokens = _leakage_tokens(value)
    for forbidden in sensitive:
        forbidden_tokens = _leakage_tokens(forbidden)
        if not forbidden_tokens or len(forbidden_tokens) > len(value_tokens):
            continue
        width = len(forbidden_tokens)
        for index in range(len(value_tokens) - width + 1):
            if value_tokens[index : index + width] == forbidden_tokens:
                return True
    return False


def _assert_public_boundary(public_root: Path, grader: dict[str, Any]) -> None:
    forbidden_text = _grader_sensitive_text(grader)
    for path in public_root.rglob("*"):
        relative = path.relative_to(public_root).as_posix().lower()
        if (
            "grader" in PurePosixPath(relative).parts
            or _contains_sensitive_text(relative, forbidden_text)
        ):
            raise ContractError(f"{path}: held-out grader path leaked into public tree")
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8").lower()
            except UnicodeDecodeError as error:
                raise ContractError(
                    f"{path}: public assets must be UTF-8 text"
                ) from error
            if _contains_sensitive_text(text, forbidden_text):
                raise ContractError(
                    f"{path}: held-out grader detail leaked into public content"
                )


def _validate_schema_documents(root: Path) -> None:
    schema_root = root / "schemas"
    expected = {
        "suite-v1.schema.json",
        "fixture-v1.schema.json",
        "grader-v1.schema.json",
        "replay-trace-v1.schema.json",
        "run-manifest-v1.schema.json",
        "event-record-v1.schema.json",
        "run-result-v1.schema.json",
        "comparison-model-v1.schema.json",
        "suite-v2.schema.json",
        "fixture-v2.schema.json",
        "grader-v2.schema.json",
    }
    actual = {
        path.name for path in schema_root.iterdir() if path.is_file()
    } if schema_root.is_dir() else set()
    if actual != expected:
        raise ContractError("schemas: missing or extra versioned schema documents")
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
    trace_path = root / "replay-trace-v1.json"
    trace = load_json(trace_path)
    try:
        replay.validate_trace(trace)
    except replay.ReplayError as error:
        raise ContractError(f"replay trace: {error}") from error
    trace_bytes = replay.canonical_bytes(trace)
    if trace_path.read_bytes() != trace_bytes:
        raise ContractError("replay trace: must use canonical JSON bytes")
    suite_path = root / "suite-v1.json"
    suite = load_json(suite_path)
    version_one_entries = validate_suite_shape(suite, expected_version=1)
    suite_v2_path = root / "suite-v2.json"
    suite_v2 = load_json(suite_v2_path)
    entries = validate_suite_shape(suite_v2, expected_version=2)
    version_one_bindings = {
        entry["fixture_id"]: entry for entry in version_one_entries
    }
    for entry in entries:
        if entry["fixture_version"] == 1 and entry != version_one_bindings.get(
            entry["fixture_id"]
        ):
            raise ContractError(
                f"{entry['fixture_id']}: version 1 suite binding drift"
            )
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
        fixture_version = entry["fixture_version"]
        expected_manifest = (
            Path("fixtures")
            / fixture_id
            / "public"
            / f"manifest-v{fixture_version}.json"
        ).as_posix()
        if entry["manifest"] != expected_manifest:
            raise ContractError(f"{fixture_id}: dangling manifest reference")
        manifest_path = root / entry["manifest"]
        public_root = fixture_dir / "public"
        payload_root = public_root / "payload"
        seed_root = payload_root / "seed"
        grader_dir = fixture_dir / "grader"
        grader_path = grader_dir / f"grader-v{fixture_version}.json"
        if set(path.name for path in public_root.iterdir()) != {
            f"manifest-v{fixture_version}.json",
            "payload",
        }:
            raise ContractError(f"{fixture_id}: missing or extra public assets")
        expected_grader_assets = {f"grader-v{fixture_version}.json"}
        if fixture_version == 2:
            expected_grader_assets.add("assets")
        if set(path.name for path in grader_dir.iterdir()) != expected_grader_assets:
            raise ContractError(f"{fixture_id}: missing or extra grader assets")

        manifest = load_json(manifest_path)
        grader = load_json(grader_path)
        validate_manifest_shape(
            manifest,
            f"{fixture_id}.manifest",
            expected_version=fixture_version,
        )
        validate_grader_shape(
            grader,
            f"{fixture_id}.grader",
            expected_version=fixture_version,
        )
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
        _assert_public_boundary(public_root, grader)

        manifest_digest = document_digest(manifest)
        payload_digest = tree_digest(payload_root)
        seed_digest = tree_digest(seed_root)
        fixture_digest = public_fixture_digest(
            fixture_id, entry["fixture_version"], payload_digest, seed_digest
        )
        grader_digest = document_digest(grader)
        if fixture_version == 2:
            private_assets_digest = tree_digest(grader_dir / "assets")
            if grader["private_assets_tree_sha256"] != private_assets_digest:
                raise ContractError(
                    f"{fixture_id}: private grader asset binding drift"
                )
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

    baseline_root = root / "baseline-v1"
    comparison_path = baseline_root / "comparison-v1.json"
    if not comparison_path.is_file():
        raise ContractError("baseline-v1: missing comparison model")
    try:
        comparison = replay.load_json(comparison_path)
        if replay.canonical_bytes(comparison) != comparison_path.read_bytes():
            raise ContractError("baseline-v1: comparison model must be canonical")
        compare.validate_model(comparison, baseline_root)
        run_dirs = [
            (baseline_root / source["evidence"]["manifest"]).parent
            for source in comparison["sources"]
        ]
        compare.compare_runs(
            run_dirs,
            baseline_root,
            comparison["comparison_reference_run_id"],
            check=True,
        )
    except (compare.ComparisonError, replay.ReplayError) as error:
        raise ContractError(f"baseline-v1: {error}") from error

    return {
        "suite": document_digest(suite_v2),
        "suite_v1": document_digest(suite),
        "fixtures": str(len(entries)),
        "trace": replay.digest_bytes(trace_bytes),
        "comparison": replay.digest_bytes(comparison_path.read_bytes()),
    }


def main() -> int:
    try:
        result = validate()
    except ContractError as error:
        print(f"evaluation validation failed: {error}", file=sys.stderr)
        return 1
    print(
        f"evaluation validation passed: {result['fixtures']} fixtures, "
        f"suite {result['suite']}, trace {result['trace']}, "
        f"comparison {result['comparison']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
