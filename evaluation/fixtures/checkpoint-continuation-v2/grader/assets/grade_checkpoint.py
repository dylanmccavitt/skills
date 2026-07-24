#!/usr/bin/env python3
"""Held-out checks for the checkpoint-continuation fixture."""

from __future__ import annotations

import argparse
import json
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
    return subprocess.run(
        [sys.executable, "-m", "release_builder.cli", *arguments],
        cwd=workspace,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def write_input(path: Path, records: list[dict[str, str]]) -> None:
    path.write_text(json.dumps(records), encoding="utf-8")


def exercise(workspace: Path, records: list[dict[str, str]]) -> tuple[str, Path]:
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
        return report.read_text(encoding="utf-8"), checkpoint


def check_outcomes(workspace: Path) -> None:
    first = [
        {"id": "REL-9", "component": "worker", "status": "blocked", "notes": "hold"},
        {"id": "REL-2", "component": "api", "status": "ready", "notes": "green"},
    ]
    second = [
        {"id": "REL-7", "component": "web", "status": "ready", "notes": "shipped"},
    ]
    first_report, _ = exercise(workspace, first)
    second_report, _ = exercise(workspace, second)
    required_first = ("Ready: 1", "Blocked: 1", "REL-2", "REL-9", "worker")
    if not all(value in first_report for value in required_first):
        raise AssertionError("first report omitted required data")
    if not all(value in second_report for value in ("Ready: 1", "Blocked: 0", "REL-7")):
        raise AssertionError("second report omitted required data")
    if first_report == second_report:
        raise AssertionError("reports appear hard-coded")


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


def check_scope(workspace: Path) -> None:
    actual = {
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    unexpected = actual - ALLOWED_PATHS
    if unexpected:
        raise AssertionError(f"unexpected paths: {sorted(unexpected)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--check", required=True, choices=("outcomes", "preservation", "scope")
    )
    arguments = parser.parse_args()
    checks = {
        "outcomes": check_outcomes,
        "preservation": check_preservation,
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
