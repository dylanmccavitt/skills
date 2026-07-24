#!/usr/bin/env python3
"""Held-out checks for the checkpoint-continuation fixture."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ALLOWED_PATHS = {
    "README.md",
    "release_builder/__init__.py",
    "release_builder/checkpoint.py",
    "release_builder/cli.py",
    "release_builder/model.py",
    "release_builder/render.py",
    "tests/test_visible.py",
}


def run(workspace: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "release_builder.cli", *arguments],
        cwd=workspace,
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def write_input(path: Path, records: list[dict[str, str]]) -> None:
    path.write_text(json.dumps(records), encoding="utf-8")


def canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def expected_report(records: list[dict[str, str]]) -> str:
    normalized = sorted(
        (
            {
                key: value.strip()
                for key, value in record.items()
            }
            for record in records
        ),
        key=lambda record: record["id"],
    )
    ready = sum(record["status"] == "ready" for record in normalized)
    blocked = sum(record["status"] == "blocked" for record in normalized)
    lines = [
        "# Release readiness",
        "",
        f"Ready: {ready}",
        f"Blocked: {blocked}",
        "",
        "## Items",
        "",
    ]
    for record in normalized:
        lines.append(
            f"- {record['id']} [{record['status']}] "
            f"{record['component']} — {record['notes']}"
        )
    return "\n".join(lines) + "\n"


def exercise(workspace: Path, records: list[dict[str, str]]) -> str:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "input.json"
        checkpoint = root / "checkpoint.json"
        report = root / "report.md"
        write_input(source, records)
        prepared = run(
            workspace,
            "prepare",
            "--input",
            str(source),
            "--checkpoint",
            str(checkpoint),
        )
        if prepared.returncode != 0:
            raise AssertionError(prepared.stderr or prepared.stdout)
        source.unlink()
        completed = run(
            workspace,
            "complete",
            "--checkpoint",
            str(checkpoint),
            "--output",
            str(report),
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr or completed.stdout)
        return report.read_text(encoding="utf-8")


def check_outcomes(workspace: Path) -> None:
    first = [
        {"id": "REL-9", "component": "worker", "status": "blocked", "notes": "hold"},
        {"id": "REL-2", "component": "api", "status": "ready", "notes": "green"},
    ]
    second = [
        {"id": "REL-7", "component": "web", "status": "ready", "notes": "shipped"},
    ]
    for records in (first, second):
        if exercise(workspace, records) != expected_report(records):
            raise AssertionError("report did not exactly match its input")


def check_preservation(workspace: Path) -> None:
    records = [
        {"id": "REL-4", "component": "db", "status": "blocked", "notes": "migration"}
    ]
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "input.json"
        checkpoint = root / "checkpoint.json"
        output = root / "report.md"
        write_input(source, records)
        result = run(
            workspace,
            "prepare",
            "--input",
            str(source),
            "--checkpoint",
            str(checkpoint),
        )
        if result.returncode != 0:
            raise AssertionError("prepare failed")
        normalized = sorted(records, key=lambda record: record["id"])
        expected_state = {
            "records": normalized,
            "records_sha256": (
                "sha256:" + hashlib.sha256(canonical_bytes(normalized)).hexdigest()
            ),
            "schema_version": 1,
            "summary": {"blocked": 1, "ready": 0},
        }
        if checkpoint.read_bytes() != canonical_bytes(expected_state):
            raise AssertionError("checkpoint is not the required canonical state")
        source.unlink()
        result = run(
            workspace,
            "complete",
            "--checkpoint",
            str(checkpoint),
            "--output",
            str(output),
        )
        if result.returncode != 0 or "REL-4" not in output.read_text(encoding="utf-8"):
            raise AssertionError("saved state was not reusable in a fresh process")
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        state["records"][0]["status"] = "ready"
        checkpoint.write_text(json.dumps(state), encoding="utf-8")
        result = run(
            workspace,
            "complete",
            "--checkpoint",
            str(checkpoint),
            "--output",
            str(output),
        )
        if result.returncode == 0:
            raise AssertionError("tampered saved state was accepted")


def _assert_atomic_checkpoint_source(workspace: Path) -> None:
    source = workspace / "release_builder/checkpoint.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    atomic_calls = {
        "os.replace",
        "os.rename",
        "Path.replace",
        "Path.rename",
    }
    observed = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if isinstance(owner, ast.Name):
            observed.add(f"{owner.id}.{node.func.attr}")
    if not observed & atomic_calls:
        raise AssertionError("checkpoint writer does not use atomic replacement")


def _assert_standard_library_only(workspace: Path) -> None:
    local_modules = {"release_builder"}
    for path in workspace.rglob("*.py"):
        if path.is_symlink():
            raise AssertionError("Python source must not be a symlink")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module] if node.level == 0 else []
            else:
                continue
            for module in modules:
                if module is None:
                    continue
                root = module.split(".", 1)[0]
                if root not in sys.stdlib_module_names | local_modules:
                    raise AssertionError(f"non-standard-library import: {module}")


def check_contract(workspace: Path) -> None:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    visible = subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_*.py",
        ],
        cwd=workspace,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if visible.returncode != 0:
        raise AssertionError(visible.stderr or visible.stdout)
    _assert_atomic_checkpoint_source(workspace)
    _assert_standard_library_only(workspace)

    invalid_inputs = (
        [],
        [{"id": "REL-1", "component": "api", "status": "unknown", "notes": "x"}],
        [
            {"id": "REL-1", "component": "api", "status": "ready", "notes": "x"},
            {"id": "REL-1", "component": "web", "status": "blocked", "notes": "y"},
        ],
        [
            {
                "id": "REL-1",
                "component": "api",
                "status": "ready",
                "notes": "x",
                "extra": "forbidden",
            }
        ],
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for index, records in enumerate(invalid_inputs):
            source = root / f"invalid-{index}.json"
            checkpoint = root / f"invalid-{index}.checkpoint"
            write_input(source, records)
            result = run(
                workspace,
                "prepare",
                "--input",
                str(source),
                "--checkpoint",
                str(checkpoint),
            )
            if result.returncode == 0:
                raise AssertionError("invalid input was accepted")

        output = root / "report.md"
        for index, content in enumerate((b"", b"not-json\n", b"{}\n")):
            checkpoint = root / f"malformed-{index}.json"
            checkpoint.write_bytes(content)
            result = run(
                workspace,
                "complete",
                "--checkpoint",
                str(checkpoint),
                "--output",
                str(output),
            )
            if result.returncode == 0:
                raise AssertionError("missing or malformed checkpoint was accepted")


def check_scope(workspace: Path) -> None:
    actual = set()
    for path in workspace.rglob("*"):
        relative_path = path.relative_to(workspace)
        if "__pycache__" in relative_path.parts:
            continue
        if path.is_symlink():
            raise AssertionError(f"symlink is not allowed: {relative_path.as_posix()}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise AssertionError(
                f"non-regular path is not allowed: {relative_path.as_posix()}"
            )
        actual.add(relative_path.as_posix())
    unexpected = actual - ALLOWED_PATHS
    missing = ALLOWED_PATHS - actual
    if unexpected or missing:
        raise AssertionError(
            f"path set mismatch: unexpected={sorted(unexpected)}, missing={sorted(missing)}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--check",
        required=True,
        choices=("outcomes", "preservation", "contract", "scope"),
    )
    arguments = parser.parse_args()
    checks = {
        "outcomes": check_outcomes,
        "preservation": check_preservation,
        "contract": check_contract,
        "scope": check_scope,
    }
    try:
        checks[arguments.check](arguments.workspace.resolve())
    except (AssertionError, OSError, subprocess.SubprocessError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
