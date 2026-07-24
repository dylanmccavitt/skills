"""Small durable state kernel for voice-directed repository work."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable


class StateError(ValueError):
    pass


class PersistedStateError(StateError):
    """A failed transition whose fail-closed state must still be persisted."""


class HeadDriftError(PersistedStateError):
    pass


FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
FULL_DIGEST = re.compile(r"^[0-9a-f]{64}$")
DELIVERY_EFFECTS = {"close", "deploy", "merge", "publish"}
DELIVERY_PATTERNS = (
    (
        re.compile(
            r"(?:^|[\s;&|])(?:\S*/)?gh(?:\s+(?:--repo|-R)\s+\S+)?"
            r"\s+pr\s+merge(?:\s|$)"
        ),
        "merge",
    ),
    (
        re.compile(
            r"(?:^|[\s;&|])(?:\S*/)?gh(?:\s+(?:--repo|-R)\s+\S+)?"
            r"\s+issue\s+close(?:\s|$)"
        ),
        "close",
    ),
    (re.compile(r"(?:^|[\s;&|])(?:\S*/)?npm\s+publish(?:\s|$)"), "publish"),
    (
        re.compile(
            r"(?:^|[\s;&|])(?:\S*/)?vercel\s+deploy\b.*"
            r"(?:--prod|--production)(?:\s|$)"
        ),
        "deploy",
    ),
)


def observe_pr_head(repo: str, pr: str) -> str:
    """Refresh the current PR head from GitHub at the delivery boundary."""
    result = subprocess.run(
        ["gh", "pr", "view", pr, "--repo", repo, "--json", "headRefOid", "--jq", ".headRefOid"],
        check=True,
        capture_output=True,
        text=True,
    )
    head = result.stdout.strip()
    if not FULL_SHA.fullmatch(head):
        raise StateError("GitHub returned an invalid PR head")
    return head


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def work_class(
    needs_branch: bool,
    concurrent: bool,
    external_effect: bool,
    high_risk: bool,
) -> str:
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
        seen.add(lane_id)
        domains.add(domain)


def _validated_actors(contract: dict) -> dict[str, list[str]]:
    actors = contract.get("actors")
    if not isinstance(actors, dict):
        raise StateError("contract actors must be an object")
    required_roles = {"implement", "review_gate"}
    missing_roles = required_roles - set(actors)
    if missing_roles:
        raise StateError(f"contract actors missing: {', '.join(sorted(missing_roles))}")
    if not any(role in actors for role in ("coordinator", "user")):
        raise StateError("contract requires a coordinator or user decision actor")

    normalized = {}
    for role, members in actors.items():
        if (
            not isinstance(role, str)
            or not role
            or not isinstance(members, list)
            or not members
            or any(not isinstance(member, str) or not member for member in members)
            or len(set(members)) != len(members)
        ):
            raise StateError(f"contract actor role is invalid: {role}")
        normalized[role] = list(members)

    implementers = set(normalized["implement"])
    reviewers = set(normalized["review_gate"])
    decision_actors = set(normalized.get("coordinator", [])) | set(
        normalized.get("user", [])
    )
    if implementers & reviewers:
        raise StateError("implement and review_gate actors must be independent")
    if implementers & decision_actors:
        raise StateError("implement actors cannot hold delivery authority")
    if contract["owner"] not in decision_actors:
        raise StateError("task owner must be a registered coordinator or user actor")
    return normalized


def new_task(task_id: str, contract: dict) -> dict:
    required = {
        "acceptance",
        "actors",
        "branch",
        "intent",
        "non_scope",
        "owner",
        "repo",
        "scope",
    }
    missing = required - set(contract)
    if missing:
        raise StateError(f"contract missing: {', '.join(sorted(missing))}")
    if not isinstance(task_id, str) or not task_id:
        raise StateError("task id is required")
    if not isinstance(contract["branch"], str) or not contract["branch"]:
        raise StateError("contract branch is required")
    if not isinstance(contract["repo"], str) or not contract["repo"]:
        raise StateError("contract repository is required")
    _validated_actors(contract)
    return {
        "schema": 2,
        "id": task_id,
        "revision": 1,
        "state": "approved",
        "contract": copy.deepcopy(contract),
        "contract_digest": digest(contract),
        "writer": None,
        "head": None,
        "review": None,
        "authority": None,
        "authority_history": [],
        "receipts": [],
        "next_action": "claim writer",
    }


def _receipt(task: dict, kind: str, text: str, actor: str) -> None:
    task["receipts"].append(
        {
            "kind": kind,
            "text": text,
            "actor": actor,
            "revision": task["revision"],
        }
    )


def _transition(task: dict, expected_revision: int, change: Callable[[dict], None]) -> None:
    observed = int(task.get("revision", 0))
    if isinstance(expected_revision, bool) or observed != expected_revision:
        raise StateError(
            f"revision conflict: expected {expected_revision}, observed {observed}"
        )
    candidate = copy.deepcopy(task)
    candidate["revision"] = observed + 1
    try:
        change(candidate)
    except PersistedStateError:
        task.clear()
        task.update(candidate)
        raise
    task.clear()
    task.update(candidate)


def _actor_has_role(task: dict, actor: str, role: str) -> bool:
    return actor in task["contract"]["actors"].get(role, [])


def _archive_authority(task: dict, status: str) -> None:
    authority = task.get("authority")
    if not authority:
        return
    archived = copy.deepcopy(authority)
    if archived.get("status") == "active":
        archived["status"] = status
    task["authority_history"].append(archived)
    task["authority"] = None


def _claim(
    task: dict,
    expected_revision: int,
    actor: str,
    branch: str,
    worktree: str,
) -> None:
    def change(candidate: dict) -> None:
        if not _actor_has_role(candidate, actor, "implement"):
            raise StateError("writer is not registered for the implement role")
        if candidate["writer"]:
            raise StateError("task already has a writer")
        if branch != candidate["contract"]["branch"]:
            raise StateError("writer branch does not match the approved contract")
        if not worktree:
            raise StateError("writer worktree is required")
        candidate["writer"] = {
            "actor": actor,
            "branch": branch,
            "worktree": worktree,
        }
        candidate["state"] = "implementing"
        candidate["next_action"] = "implement approved scope"

    _transition(task, expected_revision, change)


def _set_implemented(
    task: dict,
    expected_revision: int,
    actor: str,
    head: str,
    checks: list[str],
) -> None:
    def change(candidate: dict) -> None:
        if not candidate["writer"] or candidate["writer"]["actor"] != actor:
            raise StateError("only the claimed writer may implement")
        if not FULL_SHA.fullmatch(head):
            raise StateError("implementation head must be a full commit SHA")
        if not isinstance(checks, list) or any(
            not isinstance(check, str) or not check for check in checks
        ):
            raise StateError("implementation checks must be a list of non-empty strings")
        _archive_authority(candidate, "invalidated")
        candidate.update(
            {
                "state": "implemented",
                "head": head,
                "review": None,
                "next_action": "independent review",
            }
        )
        _receipt(
            candidate,
            "implemented",
            f"Implemented {head[:12]}; {len(checks)} checks passed.",
            actor,
        )

    _transition(task, expected_revision, change)


def _review(
    task: dict,
    expected_revision: int,
    actor: str,
    head: str,
    passed: bool,
    finding_count: int = 0,
) -> None:
    def change(candidate: dict) -> None:
        if not _actor_has_role(candidate, actor, "review_gate"):
            raise StateError("reviewer is not registered for the review_gate role")
        if not candidate["writer"] or candidate["writer"]["actor"] == actor:
            raise StateError("reviewer must be independent from writer")
        if candidate["state"] != "implemented" or candidate["head"] != head:
            raise StateError("review must bind the current implemented head")
        if not isinstance(passed, bool):
            raise StateError("review result must be explicit")
        if (
            isinstance(finding_count, bool)
            or not isinstance(finding_count, int)
            or finding_count < 0
            or (passed and finding_count != 0)
        ):
            raise StateError("review finding count does not match the result")
        candidate["review"] = {
            "actor": actor,
            "head": head,
            "passed": passed,
            "findings": finding_count,
        }
        _archive_authority(candidate, "invalidated")
        candidate["state"] = "review_passed" if passed else "changes_requested"
        candidate["next_action"] = (
            "await delivery authority" if passed else "writer addresses findings"
        )
        _receipt(
            candidate,
            "review",
            "Review passed."
            if passed
            else f"Review requested {finding_count} changes.",
            actor,
        )

    _transition(task, expected_revision, change)


def _grant_delivery(
    task: dict,
    expected_revision: int,
    actor: str,
    origin: str,
    effect: str,
    repo: str,
    task_id: str,
    pr: str,
    head: str,
    request_digest: str,
) -> None:
    def change(candidate: dict) -> None:
        if _actor_has_role(candidate, actor, "implement"):
            raise StateError("implement actors cannot grant delivery authority")
        if origin not in {"coordinator", "user"} or not _actor_has_role(
            candidate, actor, origin
        ):
            raise StateError("delivery authority actor is not registered for this task")
        if task_id != candidate["id"] or repo != candidate["contract"]["repo"]:
            raise StateError("delivery authority scope does not match the task")
        if effect not in DELIVERY_EFFECTS or not pr:
            raise StateError("delivery authority requires an explicit supported effect and PR")
        if not FULL_SHA.fullmatch(head) or not FULL_DIGEST.fullmatch(request_digest):
            raise StateError("delivery authority requires exact head and action digests")
        if (
            candidate["state"] != "review_passed"
            or not candidate["review"]
            or not candidate["review"]["passed"]
            or candidate["head"] != head
            or candidate["review"]["head"] != head
        ):
            raise StateError("delivery requires a passing review of the current head")

        _archive_authority(candidate, "superseded")
        grant_id = digest(
            {
                "actor": actor,
                "effect": effect,
                "head": head,
                "pr": pr,
                "repo": repo,
                "request_digest": request_digest,
                "revision": candidate["revision"],
                "task": task_id,
            }
        )
        candidate["authority"] = {
            "id": grant_id,
            "actor": actor,
            "origin": origin,
            "effect": effect,
            "repo": repo,
            "task": task_id,
            "pr": pr,
            "head": head,
            "request_digest": request_digest,
            "status": "active",
        }
        candidate["next_action"] = "delivery gate refreshes live PR head"
        _receipt(
            candidate,
            "authority",
            "One exact-head delivery attempt authorized.",
            actor,
        )

    _transition(task, expected_revision, change)


def _revoke_delivery(
    task: dict,
    expected_revision: int,
    actor: str,
    grant_id: str,
) -> None:
    def change(candidate: dict) -> None:
        authority = candidate.get("authority")
        if (
            not authority
            or authority.get("id") != grant_id
            or authority.get("status") != "active"
            or authority.get("actor") != actor
        ):
            raise StateError("only the active grant actor may revoke delivery authority")
        authority["status"] = "revoked"
        authority["revoked_at_revision"] = candidate["revision"]
        candidate["next_action"] = "await fresh delivery authority"
        _receipt(candidate, "authority-revoked", "Delivery authority revoked.", actor)

    _transition(task, expected_revision, change)


def _invalidate(task: dict, reason: str) -> None:
    task["review"] = None
    _archive_authority(task, "invalidated")
    task["state"] = "implementing"
    task["next_action"] = "refresh proof and independent review"
    _receipt(task, "invalidated", f"Proof invalidated: {reason}.", "kernel")


def _consume_delivery(
    task: dict,
    expected_revision: int,
    grant_id: str,
    effect: str,
    request_digest: str,
    observe_head: Callable[[str, str], str] = observe_pr_head,
) -> None:
    def change(candidate: dict) -> None:
        authority = candidate.get("authority")
        if (
            not authority
            or authority.get("id") != grant_id
            or authority.get("status") != "active"
            or authority.get("effect") != effect
            or authority.get("request_digest") != request_digest
        ):
            raise StateError("no active one-shot delivery authority for this exact action")
        live_head = observe_head(authority["repo"], authority["pr"])
        if live_head != authority["head"] or live_head != candidate["head"]:
            _invalidate(candidate, f"live PR head changed to {live_head}")
            raise HeadDriftError(
                "live PR head does not match the authorized reviewed head"
            )
        if (
            not candidate["review"]
            or not candidate["review"]["passed"]
            or candidate["review"]["head"] != live_head
        ):
            raise StateError("current independent review required")
        authority["status"] = "consumed"
        authority["consumed_at_revision"] = candidate["revision"]
        candidate["state"] = "delivery_started"
        candidate["next_action"] = "run the single authorized external action"
        _receipt(
            candidate,
            "delivery-attempt",
            "One-shot authority consumed after live-head refresh.",
            "delivery-gate",
        )

    _transition(task, expected_revision, change)


def _checkpoint(
    task: dict,
    expected_revision: int,
    actor: str,
    successor: str,
    next_action: str,
) -> None:
    def change(candidate: dict) -> None:
        writer = candidate.get("writer")
        if not writer or writer["actor"] != actor:
            raise StateError("only the claimed writer may checkpoint")
        if (
            not _actor_has_role(candidate, successor, "implement")
            or successor == actor
        ):
            raise StateError(
                "checkpoint requires one distinct registered implement successor"
            )
        if not next_action:
            raise StateError("checkpoint requires an exact next action")
        candidate["checkpoint"] = {
            "status": "pending",
            "successor": successor,
            "next_action": next_action,
            "previous_writer": copy.deepcopy(writer),
            "head": candidate.get("head"),
            "review": copy.deepcopy(candidate.get("review")),
            "authority_limits": copy.deepcopy(candidate.get("authority")),
        }
        candidate["review"] = None
        _archive_authority(candidate, "invalidated")
        candidate["state"] = "checkpointed"
        candidate["next_action"] = next_action
        _receipt(
            candidate,
            "checkpoint",
            "Checkpoint recorded with one confirmed successor.",
            actor,
        )

    _transition(task, expected_revision, change)


def _resume_checkpoint(
    task: dict,
    expected_revision: int,
    actor: str,
) -> None:
    def change(candidate: dict) -> None:
        saved = candidate.get("checkpoint")
        if (
            not saved
            or saved.get("status") != "pending"
            or saved.get("successor") != actor
            or not _actor_has_role(candidate, actor, "implement")
        ):
            raise StateError("only the pending checkpoint successor may resume")
        previous_writer = saved["previous_writer"]
        candidate["writer"] = {
            "actor": actor,
            "branch": previous_writer["branch"],
            "worktree": previous_writer["worktree"],
        }
        saved["status"] = "resumed"
        saved["resumed_at_revision"] = candidate["revision"]
        candidate["state"] = "implementing"
        candidate["next_action"] = saved["next_action"]
        _receipt(
            candidate,
            "checkpoint-resumed",
            "Checkpoint recovered and writer ownership transferred.",
            actor,
        )

    _transition(task, expected_revision, change)


def _read_store(path: Path) -> dict:
    if not path.exists():
        return {"schema": 2, "tasks": {}}
    try:
        store = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"unable to read orchestration state: {path}") from error
    if (
        not isinstance(store, dict)
        or store.get("schema") != 2
        or not isinstance(store.get("tasks"), dict)
    ):
        raise StateError(f"invalid orchestration state: {path}")
    return store


def _replace_store(path: Path, store: dict) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=".state-",
        text=True,
    )
    try:
        with os.fdopen(file_descriptor, "w") as temporary:
            json.dump(store, temporary, sort_keys=True, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


@contextmanager
def locked_store(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    with lock.open("a+") as handle:
        os.chmod(lock, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            current = _read_store(path)
            yield current
            _replace_store(path, current)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def create_task(path: Path, task_id: str, contract: dict) -> dict:
    with locked_store(path) as store:
        if task_id in store["tasks"]:
            raise StateError(f"task already exists: {task_id}")
        store["tasks"][task_id] = new_task(task_id, contract)
        result = copy.deepcopy(store["tasks"][task_id])
    return result


def load_task(path: Path, task_id: str) -> dict:
    lock = path.with_suffix(path.suffix + ".lock")
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock.open("a+") as handle:
        os.chmod(lock, 0o600)
        fcntl.flock(handle, fcntl.LOCK_SH)
        try:
            store = _read_store(path)
            return copy.deepcopy(store["tasks"][task_id])
        except KeyError as error:
            raise StateError(f"unknown task: {task_id}") from error
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def mutate_task(
    path: Path,
    record_id: str,
    expected_revision: int,
    operation: str,
    **arguments,
) -> dict:
    persisted_error = None
    with locked_store(path) as store:
        try:
            task = store["tasks"][record_id]
        except KeyError as error:
            raise StateError(f"unknown task: {record_id}") from error
        try:
            if operation == "claim":
                _claim(task, expected_revision, **arguments)
            elif operation == "implemented":
                _set_implemented(task, expected_revision, **arguments)
            elif operation == "review":
                _review(task, expected_revision, **arguments)
            elif operation == "grant-delivery":
                _grant_delivery(task, expected_revision, **arguments)
            elif operation == "revoke-delivery":
                _revoke_delivery(task, expected_revision, **arguments)
            elif operation == "consume-delivery":
                _consume_delivery(task, expected_revision, **arguments)
            elif operation == "checkpoint":
                _checkpoint(task, expected_revision, **arguments)
            elif operation == "resume-checkpoint":
                _resume_checkpoint(task, expected_revision, **arguments)
            else:
                raise StateError(f"unknown operation: {operation}")
        except PersistedStateError as error:
            persisted_error = error
        result = copy.deepcopy(task)
    if persisted_error:
        raise persisted_error
    return result


def requested_delivery_effect(payload: dict) -> str | None:
    if payload.get("hook_event_name") != "PreToolUse":
        return None
    if payload.get("tool_name") == "mcp__github__merge_pull_request":
        return "merge"
    tool_input = payload.get("tool_input")
    if payload.get("tool_name") != "Bash" or not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command", tool_input.get("cmd", ""))
    if not isinstance(command, str):
        return None
    for pattern, effect in DELIVERY_PATTERNS:
        if pattern.search(command):
            return effect
    return None


def delivery_request(payload: dict) -> dict | None:
    """Return the exact external action digest used by both grant and hook."""
    effect = requested_delivery_effect(payload)
    if effect is None:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        raise StateError("delivery tool input must be an object")
    return {
        "effect": effect,
        "digest": digest({"tool": payload.get("tool_name"), "input": tool_input}),
    }


def handle_hook(
    payload: dict,
    environment: dict[str, str],
    observe_head: Callable[[str, str], str] = observe_pr_head,
) -> None:
    request = delivery_request(payload)
    if request is None:
        return
    names = {
        "state": "CODEX_ORCHESTRATION_STATE",
        "task": "CODEX_ORCHESTRATION_TASK",
        "revision": "CODEX_ORCHESTRATION_REVISION",
        "grant": "CODEX_ORCHESTRATION_GRANT",
    }
    values = {key: environment.get(name, "") for key, name in names.items()}
    missing = [names[key] for key, value in values.items() if not value]
    if missing:
        raise StateError(f"missing delivery gate context: {', '.join(missing)}")
    try:
        revision = int(values["revision"])
    except ValueError as error:
        raise StateError("delivery gate revision must be an integer") from error
    mutate_task(
        Path(values["state"]),
        values["task"],
        revision,
        "consume-delivery",
        grant_id=values["grant"],
        effect=request["effect"],
        request_digest=request["digest"],
        observe_head=observe_head,
    )


def main(arguments: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if arguments == ["hook"]:
        try:
            payload = json.load(sys.stdin)
            if not isinstance(payload, dict):
                raise StateError("hook payload must be a JSON object")
            handle_hook(payload, dict(os.environ))
            return 0
        except Exception as error:
            print(f"voice-state-hook: {error}", file=sys.stderr)
            return 2
    print("usage: voice_state.py hook", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
