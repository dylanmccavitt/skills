#!/usr/bin/env python3
"""Classify registered orchestration sessions by liveness; report, never act."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from orchestration_graph import DEFAULT_WORKFLOW, load_workflow
from orchestration_state import load_state, state_root


DEFAULT_SUPERVISION = {
    "heartbeat_ttl_seconds": {"gepetto": 1800, "research": 900, "implementation": 2700, "review": 2700, "jiminy": 900},
    "max_lane_restarts": 2,
    "recycle_after_events": 400,
}


def supervision_policies(workflow_path: Path) -> dict[str, Any]:
    workflow = load_workflow(workflow_path)
    supervision = workflow.get("policies", {}).get("supervision")
    if not isinstance(supervision, dict):
        return DEFAULT_SUPERVISION
    return {key: supervision.get(key, default) for key, default in DEFAULT_SUPERVISION.items()}


def classify(state: dict[str, Any], policies: dict[str, Any], now: int) -> dict[str, Any]:
    role = state.get("role", "-")
    restarts = int(state.get("restarts", 0))
    events = int(state.get("events", 0))
    last_heartbeat = state.get("last_heartbeat")
    age: int | str = "unknown" if last_heartbeat is None else now - int(last_heartbeat)
    ttl = policies["heartbeat_ttl_seconds"].get(role, DEFAULT_SUPERVISION["heartbeat_ttl_seconds"].get(role, 1800))
    if restarts > policies["max_lane_restarts"]:
        status, advice = "over-budget", "RESTART_BUDGET_EXCEEDED"
    elif age == "unknown" or age > ttl:
        status, advice = "stale", "LANE_UNRESPONSIVE"
    elif events >= policies["recycle_after_events"]:
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
    }


def check(workflow_path: Path, now: int) -> tuple[list[dict[str, Any]], int]:
    policies = supervision_policies(workflow_path)
    reports = []
    sessions = state_root() / "sessions"
    for path in sorted(sessions.glob("*.json")) if sessions.is_dir() else []:
        state = load_state(path.stem)
        if state is None or not state.get("active"):
            continue
        reports.append(classify(state, policies, now))
    exit_code = 1 if any(report["status"] in {"stale", "over-budget"} for report in reports) else 0
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
