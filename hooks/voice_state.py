"""Small durable state kernel for voice-directed repository work."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import hmac
import json
import os
import posixpath
import re
import secrets
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
MERGE_METHODS = {"merge", "rebase", "squash"}


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
    domains = []
    for lane in lanes:
        lane_id, domain = lane.get("id"), lane.get("domain")
        normalized = (
            posixpath.normpath(domain.replace("\\", "/")).strip("/").casefold()
            if isinstance(domain, str)
            else ""
        )
        if normalized in {"", "."}:
            normalized = "."
        overlaps = any(
            normalized == "."
            or existing == "."
            or normalized == existing
            or normalized.startswith(f"{existing}/")
            or existing.startswith(f"{normalized}/")
            for existing in domains
        )
        if (
            not lane_id
            or normalized == ".."
            or normalized.startswith("../")
            or lane_id in seen
            or overlaps
        ):
            raise StateError("lanes require unique ids and ownership domains")
        seen.add(lane_id)
        domains.append(normalized)


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
    if reviewers & decision_actors:
        raise StateError("review_gate actors must remain read-only")
    if contract["owner"] not in decision_actors:
        raise StateError("task owner must be a registered coordinator or user actor")
    return normalized


def new_task(
    task_id: str,
    contract: dict,
    credential_hashes: dict[str, str],
) -> dict:
    required = {
        "acceptance",
        "actors",
        "branch",
        "intent",
        "non_scope",
        "owner",
        "pr",
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
    if not str(contract["pr"]).isdigit():
        raise StateError("contract PR must be an explicit number")
    actors = _validated_actors(contract)
    actor_ids = {actor for members in actors.values() for actor in members}
    if set(credential_hashes) != actor_ids or any(
        not FULL_DIGEST.fullmatch(value) for value in credential_hashes.values()
    ):
        raise StateError("every registered actor requires one credential hash")
    return {
        "schema": 2,
        "id": task_id,
        "revision": 1,
        "state": "approved",
        "contract": copy.deepcopy(contract),
        "contract_digest": digest(contract),
        "credential_hashes": copy.deepcopy(credential_hashes),
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


def _authenticate(task: dict, actor: str, credential: str, role: str) -> None:
    if not _actor_has_role(task, actor, role):
        raise StateError(f"actor is not registered for the {role} role")
    expected = task["credential_hashes"].get(actor, "")
    observed = digest({"task": task["id"], "actor": actor, "credential": credential})
    if not credential or not hmac.compare_digest(expected, observed):
        raise StateError("actor credential is invalid")


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
        if candidate["state"] != "approved" or candidate["writer"]:
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
        if (
            candidate["state"] not in {"implementing", "changes_requested"}
            or not candidate["writer"]
            or candidate["writer"]["actor"] != actor
        ):
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


def validate_delivery_action(
    action: dict,
    repo: str | None = None,
    pr: str | None = None,
) -> dict:
    if not isinstance(action, dict) or set(action) != {"kind", "repo", "pr", "method"}:
        raise StateError("delivery action must be one typed GitHub merge")
    normalized = {
        "kind": action.get("kind"),
        "repo": action.get("repo"),
        "pr": str(action.get("pr", "")),
        "method": action.get("method"),
    }
    if (
        normalized["kind"] != "github_merge"
        or not isinstance(normalized["repo"], str)
        or not normalized["repo"]
        or not normalized["pr"].isdigit()
        or normalized["method"] not in MERGE_METHODS
        or (repo is not None and normalized["repo"] != repo)
        or (pr is not None and normalized["pr"] != str(pr))
    ):
        raise StateError("delivery action is outside the approved task scope")
    return normalized


def _grant_delivery(
    task: dict,
    expected_revision: int,
    actor: str,
    origin: str,
    head: str,
    action: dict,
) -> None:
    def change(candidate: dict) -> None:
        normalized_action = validate_delivery_action(
            action,
            candidate["contract"]["repo"],
            str(candidate["contract"]["pr"]),
        )
        if _actor_has_role(candidate, actor, "implement"):
            raise StateError("implement actors cannot grant delivery authority")
        if origin not in {"coordinator", "user"} or not _actor_has_role(
            candidate, actor, origin
        ):
            raise StateError("delivery authority actor is not registered for this task")
        if not FULL_SHA.fullmatch(head):
            raise StateError("delivery authority requires an exact head")
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
                "action": normalized_action,
                "head": head,
                "revision": candidate["revision"],
                "task": candidate["id"],
            }
        )
        candidate["authority"] = {
            "id": grant_id,
            "actor": actor,
            "origin": origin,
            "action": normalized_action,
            "action_digest": digest(normalized_action),
            "task": candidate["id"],
            "head": head,
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
    action: dict,
    observe_head: Callable[[str, str], str] = observe_pr_head,
) -> None:
    def change(candidate: dict) -> None:
        authority = candidate.get("authority")
        normalized_action = validate_delivery_action(
            action,
            candidate["contract"]["repo"],
            str(candidate["contract"]["pr"]),
        )
        if (
            not authority
            or authority.get("id") != grant_id
            or authority.get("status") != "active"
            or authority.get("action_digest") != digest(normalized_action)
        ):
            raise StateError("no active one-shot delivery authority for this exact action")
        live_head = observe_head(normalized_action["repo"], normalized_action["pr"])
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


def _finish_delivery(
    task: dict,
    expected_revision: int,
    grant_id: str,
    succeeded: bool,
    exit_code: int,
) -> None:
    def change(candidate: dict) -> None:
        authority = candidate.get("authority")
        if (
            candidate["state"] != "delivery_started"
            or not authority
            or authority.get("id") != grant_id
            or authority.get("status") != "consumed"
        ):
            raise StateError("delivery result does not match a consumed authority")
        candidate["state"] = "complete" if succeeded else "delivery_failed"
        candidate["next_action"] = "none" if succeeded else "user decides whether to retry"
        _receipt(
            candidate,
            "delivered" if succeeded else "delivery-failed",
            f"Typed delivery action exited {exit_code}.",
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
        if (
            candidate["state"]
            not in {"implementing", "implemented", "changes_requested", "review_passed"}
            or not writer
            or writer["actor"] != actor
        ):
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
        candidate["writer"] = None
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
            candidate["state"] != "checkpointed"
            or candidate.get("writer") is not None
            or not saved
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


def create_task(
    path: Path,
    task_id: str,
    contract: dict,
    credentials: dict[str, str],
) -> dict:
    actors = _validated_actors(contract)
    actor_ids = {actor for members in actors.values() for actor in members}
    if set(credentials) != actor_ids or any(
        not isinstance(value, str) or not value for value in credentials.values()
    ):
        raise StateError("every registered actor requires one credential")
    credential_hashes = {
        actor: digest(
            {"task": task_id, "actor": actor, "credential": credential}
        )
        for actor, credential in credentials.items()
    }
    with locked_store(path) as store:
        if task_id in store["tasks"]:
            raise StateError(f"task already exists: {task_id}")
        store["tasks"][task_id] = new_task(task_id, contract, credential_hashes)
        result = copy.deepcopy(store["tasks"][task_id])
    return result


def provision_task(path: Path, task_id: str, contract: dict) -> dict:
    actors = _validated_actors(contract)
    actor_ids = {actor for members in actors.values() for actor in members}
    credentials = {actor: secrets.token_urlsafe(32) for actor in actor_ids}
    task = create_task(path, task_id, contract, credentials)
    return {"task": task, "credentials": credentials}


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
    credential: str = "",
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
                _authenticate(task, arguments.get("actor", ""), credential, "implement")
                _claim(task, expected_revision, **arguments)
            elif operation == "implemented":
                _authenticate(task, arguments.get("actor", ""), credential, "implement")
                _set_implemented(task, expected_revision, **arguments)
            elif operation == "review":
                _authenticate(
                    task,
                    arguments.get("actor", ""),
                    credential,
                    "review_gate",
                )
                _review(task, expected_revision, **arguments)
            elif operation == "grant-delivery":
                origin = arguments.get("origin", "")
                if origin not in {"coordinator", "user"}:
                    raise StateError("delivery authority origin is invalid")
                _authenticate(task, arguments.get("actor", ""), credential, origin)
                _grant_delivery(task, expected_revision, **arguments)
            elif operation == "revoke-delivery":
                authority = task.get("authority") or {}
                _authenticate(
                    task,
                    arguments.get("actor", ""),
                    credential,
                    authority.get("origin", ""),
                )
                _revoke_delivery(task, expected_revision, **arguments)
            elif operation == "consume-delivery":
                _consume_delivery(task, expected_revision, **arguments)
            elif operation == "finish-delivery":
                _finish_delivery(task, expected_revision, **arguments)
            elif operation == "checkpoint":
                _authenticate(task, arguments.get("actor", ""), credential, "implement")
                _checkpoint(task, expected_revision, **arguments)
            elif operation == "resume-checkpoint":
                _authenticate(task, arguments.get("actor", ""), credential, "implement")
                _resume_checkpoint(task, expected_revision, **arguments)
            else:
                raise StateError(f"unknown operation: {operation}")
        except PersistedStateError as error:
            persisted_error = error
        result = copy.deepcopy(task)
    if persisted_error:
        raise persisted_error
    return result


def direct_delivery_reason(payload: dict) -> str | None:
    if payload.get("hook_event_name") != "PreToolUse":
        return None
    tool_name = payload.get("tool_name", "")
    if isinstance(tool_name, str) and "github" in tool_name and "merge_pull_request" in tool_name:
        return "direct GitHub merge"
    if isinstance(tool_name, str) and "github" in tool_name and "close_issue" in tool_name:
        return "direct issue closure"
    tool_input = payload.get("tool_input")
    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command", tool_input.get("cmd", ""))
    if not isinstance(command, str):
        return None
    normalized = re.sub(r"[^a-z0-9_+./:$=-]+", " ", command.lower())
    checks = (
        (r"\bgh\b.*\bpr\b.*\bmerge\b", "direct GitHub merge"),
        (
            r"\bgh\b.*\bapi\b.*\bpulls?/\S+/merge\b",
            "direct GitHub merge API",
        ),
        (
            r"\bapi\.github\.com/repos/\S+/pulls?/\S+/merge\b",
            "direct GitHub merge API",
        ),
        (r"\bmergepullrequest\b", "direct GitHub GraphQL merge"),
        (r"\bgh\b.*\bissue\b.*\bclose\b", "direct issue closure"),
        (r"\bnpm\b.*\bpublish\b", "direct package publish"),
        (
            r"\bvercel\b.*\bdeploy\b.*(?:--prod|--production)\b",
            "direct production deploy",
        ),
        (
            r"\bgit\b.*\bpush\b.*(?:\+|:|/|\s)(?:main|master)\b",
            "direct protected-branch push",
        ),
    )
    for pattern, reason in checks:
        if re.search(pattern, normalized):
            return reason
    return None


def _write_capable_tool(payload: dict) -> bool:
    tool_name = payload.get("tool_name", "")
    if tool_name in {"Bash", "Edit", "Write", "apply_patch"}:
        return True
    return isinstance(tool_name, str) and (
        tool_name.endswith("__edit")
        or tool_name.endswith("__write")
        or tool_name.endswith("__apply_patch")
    )


def _review_command_is_read_only(command: str) -> bool:
    if re.search(r"[<>]|\||--output(?:=|\s)|\s-(?:i|delete|exec)\b", command):
        return False
    allowed = (
        r"(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
        r"(?:git\s+(?:diff|status|show|log|rev-parse)\b"
        r"|rg\b|ls\b|head\b|tail\b"
        r"|npm\s+test\b|node\s+--test\b"
        r"|python3?\s+-m\s+(?:unittest|pytest)\b|pytest\b"
        r"|gh\s+pr\s+(?:view|diff|checks)\b)"
    )
    parts = [part.strip() for part in re.split(r"&&|\|\||[;\n]", command)]
    return bool(parts) and all(re.fullmatch(allowed + r".*", part) for part in parts)


def _control_command(command: str) -> bool:
    return bool(
        re.fullmatch(
            r"\s*(?:/usr/bin/env\s+)?python3?\s+\S*voice_state\.py\s+"
            r"(?:create|transition|deliver)\s*(?:<\s*[^\s;&|]+)?\s*",
            command,
        )
    )


def _validate_tool_lease(payload: dict, environment: dict[str, str]) -> None:
    names = {
        "state": "CODEX_ORCHESTRATION_STATE",
        "task": "CODEX_ORCHESTRATION_TASK",
        "actor": "CODEX_ORCHESTRATION_ACTOR",
        "credential": "CODEX_ORCHESTRATION_CREDENTIAL",
        "worktree": "CODEX_ORCHESTRATION_WORKTREE",
    }
    present = {key: environment.get(name, "") for key, name in names.items()}
    if not any(present.values()):
        return
    missing = [names[key] for key, value in present.items() if not value]
    if missing:
        raise StateError(f"incomplete orchestration writer context: {', '.join(missing)}")
    task = load_task(Path(present["state"]), present["task"])
    actor = present["actor"]
    credential = present["credential"]
    actual_worktree = str(Path(environment.get("PWD", os.getcwd())).resolve())
    context_worktree = str(Path(present["worktree"]).resolve())
    if actual_worktree != context_worktree:
        raise StateError("tool worktree does not match orchestration context")
    command = ""
    if payload.get("tool_name") == "Bash":
        tool_input = payload.get("tool_input")
        if isinstance(tool_input, dict):
            command = tool_input.get("command", tool_input.get("cmd", ""))
    if isinstance(command, str) and _control_command(command):
        roles = [
            role
            for role in ("coordinator", "user", "implement", "review_gate")
            if _actor_has_role(task, actor, role)
        ]
        if not roles:
            raise StateError("control command actor is not registered")
        _authenticate(task, actor, credential, roles[0])
        return
    if _actor_has_role(task, actor, "review_gate"):
        _authenticate(task, actor, credential, "review_gate")
        if payload.get("tool_name") == "Bash" and _review_command_is_read_only(command):
            return
        raise StateError("Review Gate tool use is read-only")
    _authenticate(task, actor, credential, "implement")
    writer = task.get("writer")
    recorded_worktree = (
        str(Path(writer.get("worktree", "")).resolve()) if writer else ""
    )
    if (
        task["state"] not in {"implementing", "changes_requested"}
        or not writer
        or writer.get("actor") != present["actor"]
        or recorded_worktree != context_worktree
    ):
        raise StateError("write tool requires the active credentialed writer lease")


def handle_hook(payload: dict, environment: dict[str, str] | None = None) -> None:
    reason = direct_delivery_reason(payload)
    if reason:
        raise StateError(
            f"{reason} is blocked; use the typed voice_state.py deliver command"
        )
    if _write_capable_tool(payload):
        _validate_tool_lease(
            payload,
            dict(os.environ) if environment is None else environment,
        )


def run_delivery(
    path: Path,
    task_id: str,
    expected_revision: int,
    grant_id: str,
    action: dict,
    observe_head: Callable[[str, str], str] = observe_pr_head,
    run_command=subprocess.run,
) -> dict:
    normalized_action = validate_delivery_action(action)
    started = mutate_task(
        path,
        task_id,
        expected_revision,
        "consume-delivery",
        grant_id=grant_id,
        action=normalized_action,
        observe_head=observe_head,
    )
    head = started["head"]
    command = [
        "gh",
        "pr",
        "merge",
        normalized_action["pr"],
        "--repo",
        normalized_action["repo"],
        f"--{normalized_action['method']}",
        "--match-head-commit",
        head,
    ]
    result = run_command(command, check=False)
    exit_code = int(result.returncode)
    finished = mutate_task(
        path,
        task_id,
        started["revision"],
        "finish-delivery",
        grant_id=grant_id,
        succeeded=exit_code == 0,
        exit_code=exit_code,
    )
    if exit_code != 0:
        raise StateError(f"typed delivery action failed with exit {exit_code}")
    return finished


def _read_cli_payload() -> dict:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise StateError("command input must be a JSON object")
    return payload


def main(arguments: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    try:
        if arguments == ["hook"]:
            payload = json.load(sys.stdin)
            if not isinstance(payload, dict):
                raise StateError("hook payload must be a JSON object")
            handle_hook(payload, dict(os.environ))
            return 0
        if arguments == ["create"]:
            payload = _read_cli_payload()
            result = provision_task(
                Path(payload["state"]),
                payload["task"],
                payload["contract"],
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if arguments == ["transition"]:
            payload = _read_cli_payload()
            operation = payload["operation"]
            if operation not in {
                "claim",
                "implemented",
                "review",
                "grant-delivery",
                "revoke-delivery",
                "checkpoint",
                "resume-checkpoint",
            }:
                raise StateError("operation is not available through the control CLI")
            result = mutate_task(
                Path(payload["state"]),
                payload["task"],
                int(payload["revision"]),
                operation,
                credential=payload["credential"],
                **payload.get("arguments", {}),
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if arguments == ["deliver"]:
            payload = _read_cli_payload()
            result = run_delivery(
                Path(payload["state"]),
                payload["task"],
                int(payload["revision"]),
                payload["grant"],
                payload["action"],
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        print(
            "usage: voice_state.py create|transition|deliver|hook",
            file=sys.stderr,
        )
        return 1
    except Exception as error:
        print(f"voice-state: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
