#!/usr/bin/env python3
"""Inject the minimal checkpoint handoff instruction after compaction."""

from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if payload.get("hook_event_name") != "SessionStart" or payload.get("source") != "compact":
            return 0
        session_id = str(payload.get("session_id", ""))
        if not session_id:
            raise ValueError("missing session id")
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": (
                    "Context was compacted. Use $checkpoint-handoff for source task "
                    f"{session_id}. Keep the capsule short, preserve role-aware Gepetto/Jiminy "
                    "naming, and do not archive either the source or successor task."
                ),
            }
        }, separators=(",", ":")))
        return 0
    except (json.JSONDecodeError, ValueError) as error:
        print(f"checkpoint-handoff: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
