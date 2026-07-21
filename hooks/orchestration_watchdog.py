#!/usr/bin/env python3
"""Classify registered orchestration sessions by liveness; report, never act."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from orchestration_graph import DEFAULT_WORKFLOW, load_workflow
from orchestration_state import load_state, recover_transactions, registry_lock, state_root


DEFAULT_SUPERVISION = {
    "heartbeat_ttl_seconds": {"gepetto": 1800, "research": 900, "implementation": 2700, "review": 2700, "jiminy": 900},
    "max_lane_restarts": 2,
    "recycle_after_events": 400,
    "recycle_context_ratio": 0.8,
    "recycle_state_bytes": 131072,
    "pressure_ttl_seconds": 300,
}


def supervision_policies(workflow_path: Path) -> dict[str, Any]:
    workflow = load_workflow(workflow_path)
    supervision = workflow.get("policies", {}).get("supervision")
    if not isinstance(supervision, dict):
        return DEFAULT_SUPERVISION
    return {key: supervision.get(key, default) for key, default in DEFAULT_SUPERVISION.items()}


def classify(state: dict[str, Any], policies: dict[str, Any], now: int) -> dict[str, Any]:
    role = state.get("role", "-")
    restarts_value = state.get("restarts", 0)
    events_value = state.get("events", 0)
    last_heartbeat = state.get("last_heartbeat")
    invalid_fields = []
    if not isinstance(role, str) or role not in DEFAULT_SUPERVISION["heartbeat_ttl_seconds"]:
        invalid_fields.append("role")
    if type(state.get("active")) is not bool:
        invalid_fields.append("active")
    if type(restarts_value) is not int or restarts_value < 0:
        invalid_fields.append("restarts")
    if type(events_value) is not int or events_value < 0:
        invalid_fields.append("events")
    if last_heartbeat is not None and type(last_heartbeat) is not int:
        invalid_fields.append("last_heartbeat")
    if invalid_fields:
        return {
            "session_id": state.get("session_id", "-"), "role": role,
            "status": "invalid", "age": "unknown", "restarts": 0, "events": 0,
            "advice": f"INVALID_STATE_FIELDS: {','.join(invalid_fields)}",
            "pressure_source": "legacy-events",
        }
    restarts = restarts_value
    events = events_value
    age: int | str = "unknown" if last_heartbeat is None else now - last_heartbeat
    ttl = policies["heartbeat_ttl_seconds"].get(role, DEFAULT_SUPERVISION["heartbeat_ttl_seconds"].get(role, 1800))
    pressure = state.get("pressure")
    pressure_available = False
    if isinstance(pressure, dict):
        try:
            used = pressure["context_used_tokens"]
            limit = pressure["context_limit_tokens"]
            ratio = pressure["context_ratio"]
            state_bytes = pressure["state_bytes"]
            observed_at = pressure["observed_at"]
            pressure_available = (
                type(used) is int and type(limit) is int and type(observed_at) is int
                and type(state_bytes) is int and isinstance(ratio, (int, float))
                and 0 <= used <= limit and limit > 0 and state_bytes >= 0
                and abs(float(ratio) - used / limit) < 1e-9
                and 0 <= now - observed_at <= policies["pressure_ttl_seconds"]
            )
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            pressure_available = False
    if restarts > policies["max_lane_restarts"]:
        status, advice = "over-budget", "RESTART_BUDGET_EXCEEDED"
    elif age == "unknown" or age > ttl:
        status, advice = "stale", "LANE_UNRESPONSIVE"
    elif pressure_available and (
        float(pressure["context_ratio"]) >= policies["recycle_context_ratio"]
        or int(pressure["state_bytes"]) >= policies["recycle_state_bytes"]
    ):
        status, advice = "recycle", "proactive checkpoint"
    elif not pressure_available and events >= policies["recycle_after_events"]:
        status, advice = "recycle", "proactive checkpoint"
    else:
        status, advice = "healthy", "none"
    return {
        "session_id": state["session_id"],
        "role": role,
        "status": status,
        "age": age,
        "restarts": restarts,
        "events": events,
        "advice": advice,
        "pressure_source": "measured" if pressure_available else "legacy-events",
    }


def check(workflow_path: Path, now: int) -> tuple[list[dict[str, Any]], int]:
    policies = supervision_policies(workflow_path)
    reports = []
    with registry_lock():
        recover_transactions()
        sessions = state_root() / "sessions"
        for path in sorted(sessions.glob("*.json")) if sessions.is_dir() else []:
            try:
                state = load_state(path.stem)
                if state is None or not state.get("active"):
                    continue
                reports.append(classify(state, policies, now))
            except (OSError, TypeError, ValueError) as error:
                reports.append({
                    "session_id": path.stem,
                    "role": "-",
                    "status": "invalid",
                    "age": "unknown",
                    "restarts": 0,
                    "events": 0,
                    "advice": f"INVALID_STATE: {error}",
                    "pressure_source": "legacy-events",
                })
    exit_code = 1 if any(report["status"] in {"stale", "over-budget", "invalid"} for report in reports) else 0
    return reports, exit_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--workflow", type=Path, default=DEFAULT_WORKFLOW)
    check_parser.add_argument("--json", action="store_true")
    check_parser.add_argument("--now", type=int, default=None)
    return parser


def main() -> int:
    args = _parser().parse_args()
    now = args.now if args.now is not None else int(time.time())
    reports, exit_code = check(args.workflow, now)
    if args.json:
        print(json.dumps({"checked_at": now, "sessions": reports}, sort_keys=True))
    else:
        for report in reports:
            print(
                f"{report['session_id']} role={report['role']} status={report['status']} "
                f"age={report['age']} restarts={report['restarts']} events={report['events']} "
                f"advice={report['advice']}"
            )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
