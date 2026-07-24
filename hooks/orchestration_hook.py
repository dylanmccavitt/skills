#!/usr/bin/env python3
"""Read one Codex hook event, dispatch it, and emit its JSON result."""

from __future__ import annotations

import json
import sys
import time

from orchestration_events import HANDLERS, HookContext
from orchestration_state import (
    HEARTBEAT_COLLECTOR,
    OBSERVATION_VERSION,
    load_state,
    recover_transactions,
    registry_lock,
)


BLOCKING_EVENTS = {"PreToolUse", "Stop", "SubagentStop"}


def heartbeat_source(payload: dict[str, object], event: str) -> str:
    if event == "SessionStart":
        return str(payload.get("source") or "unknown")
    if event == "PreToolUse":
        return str(payload.get("tool_name") or "unknown")
    if event in {"SubagentStart", "SubagentStop"}:
        return str(payload.get("agent_type") or "unknown")
    return event or "unknown"


def main() -> int:
    payload: object = None
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be a JSON object")
        with registry_lock():
            recover_transactions()
            session_id = str(payload.get("session_id", ""))
            context = HookContext(payload, load_state(session_id) if session_id else None)
            if context.active and context.event in HANDLERS:
                observed_at = int(time.time())
                context.state["last_heartbeat"] = observed_at
                context.state["events"] = context.state.get("events", 0) + 1
                context.state["heartbeat"] = {
                    "version": OBSERVATION_VERSION,
                    "status": "observed",
                    "observed_at": observed_at,
                    "event": context.event,
                    "source": heartbeat_source(payload, context.event),
                    "collector": HEARTBEAT_COLLECTOR,
                }
                context.save()
            handler = HANDLERS.get(context.event)
            result = handler(context) if handler else None
        if result is not None:
            print(json.dumps(result, separators=(",", ":")))
        return 0
    except Exception as error:
        print(f"codex-orchestration-hook: {error}", file=sys.stderr)
        event = str(payload.get("hook_event_name", "")) if isinstance(payload, dict) else ""
        return 2 if not isinstance(payload, dict) or event in BLOCKING_EVENTS else 1


if __name__ == "__main__":
    raise SystemExit(main())
