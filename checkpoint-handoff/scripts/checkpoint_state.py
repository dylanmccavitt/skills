#!/usr/bin/env python3
"""Compatibility shim for tasks that cached the retired checkpoint hooks."""

from __future__ import annotations

import sys

from checkpoint_hook import main as checkpoint_hook


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "hook-session-start":
        raise SystemExit(checkpoint_hook())
    if command in {"hook-precompact", "hook-stop"}:
        raise SystemExit(0)
    print("checkpoint-handoff: retired command", file=sys.stderr)
    raise SystemExit(1)
