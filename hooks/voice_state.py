"""Small durable state kernel for voice-directed repository work."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


class StateError(ValueError):
    pass


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def work_class(needs_branch: bool, concurrent: bool, external_effect: bool, high_risk: bool) -> str:
    """Ordinary work stays outside the registry; complex work needs approved lanes."""
    if concurrent:
        return "complex"
    if needs_branch or external_effect or high_risk:
        return "durable"
    return "ordinary"


def validate_lanes(lanes: list[dict], approved: bool) -> None:
    if not approved:
        raise StateError("complex lane map requires user approval")
    seen = set()
    domains = set()
    for lane in lanes:
        lane_id, domain = lane.get("id"), lane.get("domain")
        if not lane_id or not domain or lane_id in seen or domain in domains:
            raise StateError("lanes require unique ids and ownership domains")
        seen.add(lane_id); domains.add(domain)


def new_task(task_id: str, contract: dict) -> dict:
    required = {"intent", "scope", "non_scope", "repo", "owner", "branch", "acceptance"}
    missing = required - set(contract)
    if missing:
        raise StateError(f"contract missing: {', '.join(sorted(missing))}")
    return {"schema": 2, "id": task_id, "revision": 1, "state": "approved", "contract": copy.deepcopy(contract),
            "contract_digest": digest(contract), "writer": None, "head": None, "review": None,
            "authority": None, "receipts": [], "next_action": "claim writer"}


def claim(task: dict, actor: str, branch: str, worktree: str) -> None:
    if task["writer"] and task["writer"]["actor"] != actor:
        raise StateError("task already has a writer")
    task["writer"] = {"actor": actor, "branch": branch, "worktree": worktree}
    task["state"], task["next_action"] = "implementing", "implement approved scope"


def set_implemented(task: dict, actor: str, head: str, checks: list[str]) -> None:
    if not task["writer"] or task["writer"]["actor"] != actor:
        raise StateError("only the claimed writer may implement")
    task.update({"state": "implemented", "head": head, "review": None,
                 "next_action": "independent review"})
    receipt(task, "implemented", f"Implemented {head[:12]}; {len(checks)} checks passed.", actor)


def review(task: dict, actor: str, head: str, passed: bool, finding_count: int = 0) -> None:
    if not task["writer"] or task["writer"]["actor"] == actor:
        raise StateError("reviewer must be independent from writer")
    if task["head"] != head:
        raise StateError("review must bind the current head")
    task["review"] = {"actor": actor, "head": head, "passed": passed, "findings": finding_count}
    task["state"] = "review_passed" if passed else "changes_requested"
    task["next_action"] = "await delivery authority" if passed else "writer addresses findings"
    receipt(task, "review", "Review passed." if passed else f"Review requested {finding_count} changes.", actor)


def grant_delivery(task: dict, actor: str, repo: str, pr: str, head: str) -> None:
    if task["state"] != "review_passed" or not task["review"] or task["head"] != head:
        raise StateError("delivery requires a passing review of the current head")
    task["authority"] = {"actor": actor, "repo": repo, "pr": pr, "head": head, "revoked": False}
    task["next_action"] = "delivery gate refreshes live state"
    receipt(task, "authority", "Delivery authority recorded for the reviewed head.", actor)


def invalidate(task: dict, reason: str) -> None:
    task["review"] = None
    task["authority"] = None
    task["state"] = "implementing"
    task["next_action"] = "refresh proof and independent review"
    receipt(task, "invalidated", f"Proof invalidated: {reason}.", "kernel")


def deliver(task: dict, repo: str, pr: str, head: str) -> None:
    authority = task["authority"]
    if not authority or authority["revoked"] or (authority["repo"], authority["pr"], authority["head"]) != (repo, pr, head):
        raise StateError("no current exact-head delivery authority")
    if not task["review"] or not task["review"]["passed"] or task["review"]["head"] != head:
        raise StateError("current independent review required")
    task["state"], task["next_action"] = "complete", "none"
    receipt(task, "delivered", "Delivered and verified.", "delivery-gate")


def checkpoint(task: dict, successor: str, next_action: str) -> None:
    if not task["writer"]:
        raise StateError("checkpoint requires a claimed writer")
    task["checkpoint"] = {"successor": successor, "next_action": next_action, "writer": copy.deepcopy(task["writer"])}
    task["next_action"] = next_action
    receipt(task, "checkpoint", "Checkpoint recorded with one successor.", "checkpoint")


def receipt(task: dict, kind: str, text: str, actor: str) -> None:
    task["receipts"].append({"kind": kind, "text": text, "actor": actor, "revision": task["revision"]})


@contextmanager
def locked_store(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    with lock.open("a+") as handle:
        os.chmod(lock, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        current = json.loads(path.read_text()) if path.exists() else {"schema": 2, "tasks": {}}
        yield current
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=".state-", text=True)
        with os.fdopen(fd, "w") as temp:
            json.dump(current, temp, sort_keys=True, indent=2)
            temp.write("\n")
        os.chmod(name, 0o600)
        os.replace(name, path)
        fcntl.flock(handle, fcntl.LOCK_UN)
