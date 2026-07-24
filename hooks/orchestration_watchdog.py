#!/usr/bin/env python3
"""Audit and classify orchestration state without mutating the registry."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from orchestration_graph import DEFAULT_WORKFLOW, load_workflow
from orchestration_state import (
    HEARTBEAT_COLLECTOR,
    LIFECYCLE_VERSION,
    OBSERVATION_VERSION,
    REGISTRY_SCHEMA_VERSION,
    continuation_journal_path,
    load_state,
    recover_transactions,
    registry_lock,
    registry_read_lock,
    state_root,
)


DEFAULT_SUPERVISION = {
    "heartbeat_ttl_seconds": {
        "gepetto": 1800,
        "research": 900,
        "implementation": 2700,
        "review": 2700,
        "jiminy": 900,
    },
    "max_lane_restarts": 2,
    "recycle_after_events": 400,
    "recycle_context_ratio": 0.8,
    "recycle_state_bytes": 131072,
    "pressure_ttl_seconds": 300,
}
RUNTIME_FILES = (
    "orchestration_events.py",
    "orchestration_hook.py",
    "orchestration_state.py",
    "orchestration_watchdog.py",
)
SUPPORTED_HEARTBEAT_EVENTS = {
    "PreToolUse",
    "SessionStart",
    "Stop",
    "SubagentStart",
    "SubagentStop",
}
HEARTBEAT_CAPABILITIES = {"supported-hook-v1", "unsupported"}
PRESSURE_CAPABILITIES = {"manual-context-v1", "unsupported"}


def supervision_policies(workflow_path: Path) -> dict[str, Any]:
    workflow = load_workflow(workflow_path)
    supervision = workflow.get("policies", {}).get("supervision")
    if not isinstance(supervision, dict):
        return DEFAULT_SUPERVISION
    return {
        key: supervision.get(key, default)
        for key, default in DEFAULT_SUPERVISION.items()
    }


def _invalid_report(state: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "session_id": state.get("session_id", "-"),
        "role": state.get("role", "-"),
        "status": "invalid",
        "age": "unknown",
        "restarts": 0,
        "events": 0,
        "advice": reason,
        "heartbeat_status": "invalid",
        "heartbeat_event": None,
        "heartbeat_source": None,
        "heartbeat_observed_at": None,
        "pressure_status": "invalid",
        "pressure_source": None,
        "pressure_collector": None,
    }


def _current_lifecycle(state: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    schema_version = state.get("record_schema_version")
    lifecycle = state.get("lifecycle")
    if schema_version is None and lifecycle is None:
        return None, None
    if schema_version != REGISTRY_SCHEMA_VERSION or not isinstance(lifecycle, dict):
        return None, "record_schema_version,lifecycle"
    required = {
        "version": lifecycle.get("version"),
        "origin": lifecycle.get("origin"),
        "created_at": lifecycle.get("created_at"),
        "observation_capabilities": lifecycle.get("observation_capabilities"),
    }
    if (
        required["version"] != LIFECYCLE_VERSION
        or required["origin"] not in {"registration", "continuation"}
        or type(required["created_at"]) is not int
        or required["created_at"] < 0
        or not isinstance(required["observation_capabilities"], dict)
    ):
        return None, "lifecycle"
    capabilities = required["observation_capabilities"]
    if (
        capabilities.get("heartbeat") not in HEARTBEAT_CAPABILITIES
        or capabilities.get("pressure") not in PRESSURE_CAPABILITIES
    ):
        return None, "lifecycle.observation_capabilities"
    if required["origin"] == "continuation":
        if not isinstance(lifecycle.get("continued_from"), str) or not lifecycle.get(
            "continued_from"
        ):
            return None, "lifecycle.continued_from"
        if (
            type(lifecycle.get("continued_from_revision")) is not int
            or lifecycle["continued_from_revision"] < 1
        ):
            return None, "lifecycle.continued_from_revision"
    return lifecycle, None


def _heartbeat_state(
    state: dict[str, Any], lifecycle: dict[str, Any], now: int
) -> tuple[str, int | str, str | None]:
    heartbeat = state.get("heartbeat")
    if not isinstance(heartbeat, dict) or heartbeat.get("version") != OBSERVATION_VERSION:
        return "invalid", "unknown", "heartbeat"
    status = heartbeat.get("status")
    if status == "pending":
        pending_since = heartbeat.get("pending_since")
        if (
            lifecycle["observation_capabilities"].get("heartbeat")
            != "supported-hook-v1"
            or type(pending_since) is not int
            or heartbeat.get("collector") != HEARTBEAT_COLLECTOR
            or heartbeat.get("observed_at") is not None
            or heartbeat.get("event") is not None
            or heartbeat.get("source") is not None
            or state.get("last_heartbeat") is not None
        ):
            return "invalid", "unknown", "heartbeat.pending"
        return "pending", now - pending_since, None
    if status == "observed":
        observed_at = heartbeat.get("observed_at")
        if (
            lifecycle["observation_capabilities"].get("heartbeat")
            != "supported-hook-v1"
            or type(observed_at) is not int
            or not isinstance(heartbeat.get("event"), str)
            or heartbeat.get("event") not in SUPPORTED_HEARTBEAT_EVENTS
            or not isinstance(heartbeat.get("source"), str)
            or not heartbeat.get("source")
            or heartbeat.get("collector") != HEARTBEAT_COLLECTOR
            or state.get("last_heartbeat") != observed_at
        ):
            return "invalid", "unknown", "heartbeat.observed"
        return "observed", now - observed_at, None
    capability = lifecycle["observation_capabilities"].get("heartbeat")
    if (
        status == "unsupported"
        and capability == "unsupported"
        and heartbeat.get("observed_at") is None
        and heartbeat.get("event") is None
        and heartbeat.get("source") is None
        and state.get("last_heartbeat") is None
    ):
        return "unsupported", "unknown", None
    return "invalid", "unknown", "heartbeat.status"


def _pressure_state(
    state: dict[str, Any], lifecycle: dict[str, Any], policies: dict[str, Any], now: int
) -> tuple[str, dict[str, Any] | None]:
    capability = lifecycle["observation_capabilities"].get("pressure")
    pressure = state.get("pressure")
    if capability == "unsupported":
        return ("unsupported", None) if pressure is None else ("invalid", None)
    if pressure is None:
        return "unavailable", None
    if not isinstance(pressure, dict):
        return "invalid", None
    try:
        used = pressure["context_used_tokens"]
        limit = pressure["context_limit_tokens"]
        ratio = pressure["context_ratio"]
        state_bytes = pressure["state_bytes"]
        observed_at = pressure["observed_at"]
        validation = pressure["validation"]
        context_window = pressure["context_window"]
        context_window_valid = (
            isinstance(context_window, dict)
            and (
                (
                    context_window.get("status") == "identified"
                    and isinstance(context_window.get("id"), str)
                    and bool(context_window.get("id"))
                    and context_window.get("unavailable_reason") is None
                )
                or (
                    context_window.get("status") == "unavailable"
                    and context_window.get("id") is None
                    and isinstance(context_window.get("unavailable_reason"), str)
                    and bool(context_window.get("unavailable_reason"))
                )
            )
        )
        valid = (
            pressure.get("version") == OBSERVATION_VERSION
            and pressure.get("status") == "measured"
            and isinstance(pressure.get("source"), str)
            and bool(pressure.get("source"))
            and isinstance(pressure.get("collector"), str)
            and bool(pressure.get("collector"))
            and context_window_valid
            and type(used) is int
            and type(limit) is int
            and type(observed_at) is int
            and type(state_bytes) is int
            and type(ratio) in (int, float)
            and 0 <= used <= limit
            and limit > 0
            and state_bytes >= 0
            and abs(float(ratio) - used / limit) < 1e-9
            and isinstance(validation, dict)
            and validation.get("status") == "valid"
            and type(validation.get("validated_at")) is int
            and validation.get("validated_at") == observed_at
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        valid = False
    if not valid:
        return "invalid", None
    age = now - int(pressure["observed_at"])
    if age < 0:
        return "invalid", None
    if age > policies["pressure_ttl_seconds"]:
        return "measured-expired", pressure
    return "measured-current", pressure


def classify(state: dict[str, Any], policies: dict[str, Any], now: int) -> dict[str, Any]:
    role = state.get("role", "-")
    if not isinstance(role, str) or role not in DEFAULT_SUPERVISION["heartbeat_ttl_seconds"]:
        return _invalid_report(state, "INVALID_STATE_FIELDS: role")
    if type(state.get("active")) is not bool:
        return _invalid_report(state, "INVALID_STATE_FIELDS: active")

    lifecycle, lifecycle_error = _current_lifecycle(state)
    if lifecycle_error:
        return _invalid_report(state, f"INVALID_STATE_FIELDS: {lifecycle_error}")
    if lifecycle is None:
        return {
            "session_id": state.get("session_id", "-"),
            "role": role,
            "status": "legacy-unknown",
            "age": "unknown",
            "restarts": state.get("restarts", 0)
            if type(state.get("restarts", 0)) is int
            else 0,
            "events": state.get("events", 0)
            if type(state.get("events", 0)) is int
            else 0,
            "advice": "none",
            "heartbeat_status": "legacy-unknown",
            "heartbeat_event": None,
            "heartbeat_source": None,
            "heartbeat_observed_at": None,
            "pressure_status": "legacy-unknown",
            "pressure_source": None,
            "pressure_collector": None,
        }

    restarts = state.get("restarts", 0)
    events = state.get("events", 0)
    if type(restarts) is not int or restarts < 0 or type(events) is not int or events < 0:
        return _invalid_report(state, "INVALID_STATE_FIELDS: restarts,events")
    heartbeat_status, age, heartbeat_error = _heartbeat_state(state, lifecycle, now)
    if heartbeat_error or (isinstance(age, int) and age < 0):
        return _invalid_report(
            state, f"INVALID_STATE_FIELDS: {heartbeat_error or 'heartbeat.timestamp'}"
        )
    pressure_status, pressure = _pressure_state(state, lifecycle, policies, now)
    if pressure_status == "invalid":
        return _invalid_report(state, "INVALID_STATE_FIELDS: pressure")

    if not state["active"]:
        ended_at = lifecycle.get("ended_at")
        end_reason = lifecycle.get("end_reason")
        if (
            type(ended_at) is not int
            or ended_at < lifecycle["created_at"]
            or end_reason not in {"completed", "continued", "terminal-receipt"}
        ):
            return _invalid_report(state, "INVALID_STATE_FIELDS: lifecycle.terminal")
        heartbeat = state["heartbeat"]
        return {
            "session_id": state["session_id"],
            "role": role,
            "status": "completed-ignored",
            "age": "unknown",
            "restarts": restarts,
            "events": events,
            "advice": "none",
            "heartbeat_status": heartbeat_status,
            "heartbeat_event": heartbeat.get("event"),
            "heartbeat_source": heartbeat.get("source"),
            "heartbeat_observed_at": heartbeat.get("observed_at"),
            "pressure_status": "ignored",
            "pressure_source": None,
            "pressure_collector": None,
        }

    ttl = policies["heartbeat_ttl_seconds"].get(
        role, DEFAULT_SUPERVISION["heartbeat_ttl_seconds"][role]
    )
    if restarts > policies["max_lane_restarts"]:
        status, advice = "over-budget", "RESTART_BUDGET_EXCEEDED"
    elif heartbeat_status == "unsupported":
        status, advice = "legacy-unknown", "none"
    elif age == "unknown" or age > ttl:
        status, advice = "stale-current", "LANE_UNRESPONSIVE"
    elif pressure_status == "measured-current" and pressure is not None and (
        float(pressure["context_ratio"]) >= policies["recycle_context_ratio"]
        or int(pressure["state_bytes"]) >= policies["recycle_state_bytes"]
    ):
        status, advice = "recycle-current", "proactive checkpoint"
    elif pressure_status != "measured-current" and events >= policies["recycle_after_events"]:
        status, advice = "recycle-current", "proactive checkpoint (event heuristic)"
    else:
        status, advice = "healthy-current", "none"

    heartbeat = state["heartbeat"]
    return {
        "session_id": state["session_id"],
        "role": role,
        "status": status,
        "age": age,
        "restarts": restarts,
        "events": events,
        "advice": advice,
        "heartbeat_status": heartbeat_status,
        "heartbeat_event": heartbeat.get("event"),
        "heartbeat_source": heartbeat.get("source"),
        "heartbeat_observed_at": heartbeat.get("observed_at"),
        "pressure_status": pressure_status,
        "pressure_source": pressure.get("source") if pressure else None,
        "pressure_collector": pressure.get("collector") if pressure else None,
    }


def check(
    workflow_path: Path,
    now: int,
    *,
    include_completed: bool = False,
    recover: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    policies = supervision_policies(workflow_path)
    reports = []
    with registry_lock():
        if recover:
            recover_transactions()
        sessions = state_root() / "sessions"
        for path in sorted(sessions.glob("*.json")) if sessions.is_dir() else []:
            try:
                state = load_state(path.stem)
                if state is None:
                    continue
                report = classify(state, policies, now)
                if report["status"] == "completed-ignored" and not include_completed:
                    continue
                reports.append(report)
            except (OSError, TypeError, ValueError) as error:
                reports.append(
                    _invalid_report(
                        {"session_id": path.stem, "role": "-"},
                        f"INVALID_STATE: {error}",
                    )
                )
    failing = {"stale-current", "over-budget", "invalid"}
    return reports, 1 if any(report["status"] in failing for report in reports) else 0


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_compatibility(runtime_hooks_dir: Path) -> dict[str, Any]:
    repository_hooks_dir = Path(__file__).resolve().parent
    files = []
    overall = "compatible"
    for name in RUNTIME_FILES:
        expected = repository_hooks_dir / name
        observed = runtime_hooks_dir.expanduser().resolve() / name
        entry: dict[str, Any] = {
            "file": name,
            "repository_sha256": _sha256(expected),
            "runtime_sha256": None,
            "status": "missing",
        }
        if observed.is_file():
            entry["runtime_sha256"] = _sha256(observed)
            entry["status"] = (
                "match"
                if entry["runtime_sha256"] == entry["repository_sha256"]
                else "mismatch"
            )
        if entry["status"] != "match":
            overall = "incompatible"
        files.append(entry)
    return {
        "status": overall,
        "repository_hooks_dir": str(repository_hooks_dir),
        "runtime_hooks_dir": str(runtime_hooks_dir.expanduser().resolve()),
        "record_schema_version": REGISTRY_SCHEMA_VERSION,
        "lifecycle_version": LIFECYCLE_VERSION,
        "files": files,
    }


def audit(
    workflow_path: Path, runtime_hooks_dir: Path, now: int
) -> dict[str, Any]:
    policies = supervision_policies(workflow_path)
    reports: list[dict[str, Any]] = []
    states: dict[str, dict[str, Any]] = {}
    with registry_read_lock():
        recovery_pending = continuation_journal_path().is_file()
        sessions = state_root() / "sessions"
        for path in sorted(sessions.glob("*.json")) if sessions.is_dir() else []:
            try:
                state = load_state(path.stem)
                if state is not None:
                    states[path.stem] = state
                    reports.append(classify(state, policies, now))
            except (OSError, TypeError, ValueError) as error:
                state = None
                reports.append(
                    _invalid_report(
                        {"session_id": path.stem, "role": "-"},
                        f"INVALID_STATE: {error}",
                    )
                )
    records = []
    for report in reports:
        state = states.get(report["session_id"], {})
        lifecycle = state.get("lifecycle")
        lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
        records.append(
            {
                "session_id": report["session_id"],
                "role": report["role"],
                "record_schema_version": state.get("record_schema_version"),
                "lifecycle_version": lifecycle.get("version"),
                "lifecycle_status": report["status"],
                "heartbeat_status": report["heartbeat_status"],
                "pressure_status": report["pressure_status"],
                "active": state.get("active")
                if type(state.get("active")) is bool
                else None,
                "state_revision": state.get("state_revision")
                if type(state.get("state_revision")) is int
                else None,
                "continuation": {
                    "source_id": state.get("source_id"),
                    "successor_id": state.get("successor_id"),
                    "continued_from": lifecycle.get("continued_from"),
                    "continued_from_revision": lifecycle.get(
                        "continued_from_revision"
                    ),
                },
                "terminal": {
                    "ended_at": lifecycle.get("ended_at"),
                    "end_reason": lifecycle.get("end_reason"),
                    "terminal_packet_type": state.get("terminal_packet_type"),
                },
            }
        )
    return {
        "audit_version": 1,
        "checked_at": now,
        "registry_root": str(state_root()),
        "continuation_recovery_pending": recovery_pending,
        "runtime": runtime_compatibility(runtime_hooks_dir),
        "records": records,
    }


def reconcile_plan(
    workflow_path: Path, runtime_hooks_dir: Path, now: int
) -> dict[str, Any]:
    report = audit(workflow_path, runtime_hooks_dir, now)
    actions = []
    for record in report["records"]:
        lifecycle_status = record["lifecycle_status"]
        if lifecycle_status == "legacy-unknown":
            action, reason = (
                "preserve-legacy",
                "no mechanically provable lifecycle observation",
            )
        elif lifecycle_status == "invalid":
            action, reason = "inspect-manually", "structurally invalid state"
        else:
            action, reason = "no-op", "record already has explicit lifecycle semantics"
        actions.append(
            {
                "session_id": record["session_id"],
                "action": action,
                "reason": reason,
                "writes": False,
            }
        )
    return {
        "reconciliation_version": 1,
        "mode": "dry-run",
        "checked_at": now,
        "registry_root": report["registry_root"],
        "runtime": report["runtime"],
        "actions": actions,
        "writes_performed": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "audit", "reconcile"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--workflow", type=Path, default=DEFAULT_WORKFLOW)
        command_parser.add_argument("--json", action="store_true")
        command_parser.add_argument("--now", type=int, default=None)
        if command in {"audit", "reconcile"}:
            command_parser.add_argument(
                "--runtime-hooks-dir", type=Path, default=Path(__file__).parent
            )
        if command == "check":
            command_parser.add_argument("--include-completed", action="store_true")
        if command == "reconcile":
            command_parser.add_argument(
                "--dry-run",
                action="store_true",
                help="required safety acknowledgement; reconciliation never writes",
            )
    return parser


def main() -> int:
    args = _parser().parse_args()
    now = args.now if args.now is not None else int(time.time())
    if args.command == "check":
        reports, exit_code = check(
            args.workflow, now, include_completed=args.include_completed
        )
        if args.json:
            print(json.dumps({"checked_at": now, "sessions": reports}, sort_keys=True))
        else:
            for report in reports:
                print(
                    f"{report['session_id']} role={report['role']} "
                    f"status={report['status']} age={report['age']} "
                    f"restarts={report['restarts']} events={report['events']} "
                    f"heartbeat={report['heartbeat_status']} "
                    f"pressure={report['pressure_status']} advice={report['advice']}"
                )
        return exit_code

    if args.command == "audit":
        result = audit(args.workflow, args.runtime_hooks_dir, now)
    else:
        if not args.dry_run:
            print(
                "orchestration_watchdog: reconcile requires --dry-run; apply is unsupported",
                flush=True,
            )
            return 2
        result = reconcile_plan(args.workflow, args.runtime_hooks_dir, now)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    elif args.command == "audit":
        print(
            f"registry={result['registry_root']} records={len(result['records'])} "
            f"runtime={result['runtime']['status']}"
        )
        for record in result["records"]:
            print(
                f"{record['session_id']} role={record['role']} "
                f"lifecycle={record['lifecycle_status']} "
                f"heartbeat={record['heartbeat_status']} "
                f"pressure={record['pressure_status']}"
            )
    else:
        print(
            f"mode=dry-run registry={result['registry_root']} "
            f"actions={len(result['actions'])} writes=false"
        )
        for action in result["actions"]:
            print(
                f"{action['session_id']} action={action['action']} "
                f"reason={action['reason']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
