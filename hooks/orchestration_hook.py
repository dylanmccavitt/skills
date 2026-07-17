#!/usr/bin/env python3
"""Read one Codex hook event, dispatch it, and emit its JSON result."""

from __future__ import annotations

import json
import sys

from orchestration_events import HANDLERS, HookContext
from orchestration_state import load_state


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        session_id = str(payload.get("session_id", ""))
        context = HookContext(payload, load_state(session_id) if session_id else None)
        handler = HANDLERS.get(context.event)
        result = handler(context) if handler else None
        if result is not None:
            print(json.dumps(result, separators=(",", ":")))
        return 0
    except (json.JSONDecodeError, OSError, ValueError) as error:
        print(f"codex-orchestration-hook: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
