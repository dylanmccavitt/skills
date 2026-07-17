#!/usr/bin/env python3
"""Small, private state registry for Codex orchestration hooks."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from pathlib import Path
from typing import Any


SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
ROLES = {"gepetto", "jiminy", "research", "implementation", "review"}


def state_root() -> Path:
    configured = os.environ.get("CODEX_ORCHESTRATION_STATE_DIR", "").strip()
    if configured:
        root = Path(configured).expanduser()
    else:
        xdg = os.environ.get("XDG_STATE_HOME", "").strip()
        root = Path(xdg).expanduser() / "codex" / "orchestration" if xdg else Path.home() / ".local/state/codex/orchestration"
    return root.resolve()


def _safe_id(value: str) -> str:
    if not value or not SAFE_ID.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"unsafe session id: {value!r}")
    return value


def state_path(session_id: str) -> Path:
    return state_root() / "sessions" / f"{_safe_id(session_id)}.json"


def load_state(session_id: str) -> dict[str, Any] | None:
    path = state_path(session_id)
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return None
    if not isinstance(value, dict) or value.get("session_id") != session_id:
        raise ValueError(f"invalid orchestration state: {path}")
    return value


def write_state(session_id: str, value: dict[str, Any]) -> Path:
    path = state_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    value = dict(value)
    value["session_id"] = session_id
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def register(
    session_id: str,
    role: str,
    *,
    checkpoint_on_compact: bool = True,
    merge_authorized: bool = False,
    coordinator_thread_id: str | None = None,
) -> Path:
    if role not in ROLES:
        raise ValueError(f"unsupported orchestration role: {role}")
    existing = load_state(session_id) or {}
    existing.update({
        "role": role,
        "active": True,
        "checkpoint_on_compact": checkpoint_on_compact,
        "merge_authorized": merge_authorized,
        "agents": existing.get("agents", {}),
    })
    if coordinator_thread_id:
        existing["coordinator_thread_id"] = _safe_id(coordinator_thread_id)
    return write_state(session_id, existing)


def continue_session(source_id: str, successor_id: str) -> Path | None:
    source = load_state(source_id)
    if source is None:
        return None
    source["successor_id"] = _safe_id(successor_id)
    source["active"] = False
    source["checkpoint_on_compact"] = False
    write_state(source_id, source)
    successor = dict(source)
    successor.pop("successor_id", None)
    successor["source_id"] = source_id
    successor["agents"] = {}
    successor["active"] = True
    successor["checkpoint_on_compact"] = True
    return write_state(successor_id, successor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    register_parser = subparsers.add_parser("register")
    register_parser.add_argument("--session-id", required=True)
    register_parser.add_argument("--role", choices=sorted(ROLES), required=True)
    register_parser.add_argument("--coordinator-thread-id")
    register_parser.add_argument("--merge-authorized", action="store_true")
    register_parser.add_argument("--no-checkpoint", action="store_true")

    continue_parser = subparsers.add_parser("continue")
    continue_parser.add_argument("--source-id", required=True)
    continue_parser.add_argument("--successor-id", required=True)

    complete_parser = subparsers.add_parser("complete")
    complete_parser.add_argument("--session-id", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "register":
        path = register(
            args.session_id,
            args.role,
            checkpoint_on_compact=not args.no_checkpoint,
            merge_authorized=args.merge_authorized,
            coordinator_thread_id=args.coordinator_thread_id,
        )
    elif args.command == "continue":
        path = continue_session(args.source_id, args.successor_id)
    else:
        state = load_state(args.session_id)
        if state is None:
            path = None
        else:
            state["active"] = False
            state["checkpoint_on_compact"] = False
            path = write_state(args.session_id, state)
    if path:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
