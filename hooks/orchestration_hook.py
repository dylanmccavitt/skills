#!/usr/bin/env python3
"""Read one Codex hook event, dispatch it, and emit its JSON result."""

from __future__ import annotations

import json
import sys
import time

from orchestration_events import HANDLERS, HookContext
from orchestration_state import load_state, recover_transactions, registry_lock


BLOCKING_EVENTS = {"PreToolUse", "Stop", "SubagentStop"}


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
            if context.active:
                context.state["last_heartbeat"] = int(time.time())
                context.state["events"] = context.state.get("events", 0) + 1
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
