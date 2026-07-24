"""Small durable state kernel for voice-directed repository work."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import hmac
import json
import os
import platform
import posixpath
import re
import secrets
import shlex
import shutil
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
STATE_SCHEMA = 3
TRUSTED_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PROTECTED_ENVIRONMENT = {
    "CODEX_ORCHESTRATION_STATE",
    "CODEX_ORCHESTRATION_TASK",
    "CODEX_ORCHESTRATION_ACTOR",
    "CODEX_ORCHESTRATION_CREDENTIAL",
    "CODEX_ORCHESTRATION_WORKTREE",
}
VIGIL_READ_ONLY_TOOLS = {
    "Glob",
    "Grep",
    "Read",
    "WebFetch",
    "WebSearch",
    "functions__list_mcp_resource_templates",
    "functions__list_mcp_resources",
    "functions__read_mcp_resource",
    "mcp__browser__screenshot",
    "mcp__filesystem__list_directory",
    "mcp__filesystem__read_file",
    "mcp__filesystem__read_multiple_files",
    "mcp__filesystem__read_text_file",
    "mcp__github__get_issue",
    "mcp__github__get_issue_comments",
    "mcp__github__get_me",
    "mcp__github__get_pull_request",
    "mcp__github__get_pull_request_files",
    "mcp__github__list_issues",
    "mcp__github__list_pull_requests",
    "mcp__github__search_code",
    "mcp__github__search_issues",
    "web__run",
}


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


def observe_pr_status(repo: str, pr: str) -> dict:
    """Observe whether the exact PR is open, merged, or closed for recovery."""
    result = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            pr,
            "--repo",
            repo,
            "--json",
            "headRefOid,state,mergedAt",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    observed = json.loads(result.stdout)
    head = observed.get("headRefOid", "")
    state = observed.get("state", "")
    if not FULL_SHA.fullmatch(head) or state not in {"OPEN", "CLOSED", "MERGED"}:
        raise StateError("GitHub returned an invalid PR recovery status")
    return {
        "head": head,
        "state": state,
        "merged": state == "MERGED" or bool(observed.get("mergedAt")),
    }


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


def validate_lanes(lanes: list[dict], approved: bool) -> list[dict]:
    if not approved:
        raise StateError("complex lane map requires user approval")
    if not isinstance(lanes, list) or not lanes:
        raise StateError("complex lane map requires at least one lane")
    seen = set()
    domains = []
    normalized_lanes = []
    for lane in lanes:
        if not isinstance(lane, dict):
            raise StateError("lanes require unique ids and ownership domains")
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
            not isinstance(lane_id, str)
            or not lane_id
            or normalized == ".."
            or normalized.startswith("../")
            or lane_id in seen
            or overlaps
        ):
            raise StateError("lanes require unique ids and ownership domains")
        dependencies = lane.get("depends_on", [])
        if (
            not isinstance(dependencies, list)
            or any(not isinstance(item, str) or not item for item in dependencies)
            or len(set(dependencies)) != len(dependencies)
            or lane_id in dependencies
        ):
            raise StateError("lanes require valid dependency ids")
        seen.add(lane_id)
        domains.append(normalized)
        normalized_lanes.append(
            {
                "id": lane_id,
                "domain": normalized,
                "depends_on": list(dependencies),
            }
        )

    unknown = {
        dependency
        for lane in normalized_lanes
        for dependency in lane["depends_on"]
        if dependency not in seen
    }
    if unknown:
        raise StateError("lane dependencies must reference approved lanes")

    visiting = set()
    visited = set()
    dependencies_by_lane = {
        lane["id"]: lane["depends_on"] for lane in normalized_lanes
    }

    def visit(lane_id: str) -> None:
        if lane_id in visiting:
            raise StateError("lane dependencies must be acyclic")
        if lane_id in visited:
            return
        visiting.add(lane_id)
        for dependency in dependencies_by_lane[lane_id]:
            visit(dependency)
        visiting.remove(lane_id)
        visited.add(lane_id)

    for lane_id in dependencies_by_lane:
        visit(lane_id)
    return normalized_lanes


def _canonical_path(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise StateError("path is required")
    return str(Path(value).expanduser().resolve())


def _validated_write_roots(worktree: str, roots: object) -> list[str]:
    if (
        not isinstance(roots, list)
        or not roots
        or any(not isinstance(root, str) or not root for root in roots)
    ):
        raise StateError("contract write_roots must be non-empty relative paths")
    canonical_worktree = Path(worktree)
    normalized = []
    for root in roots:
        portable = posixpath.normpath(root.replace("\\", "/")).strip("/")
        portable = "." if portable in {"", "."} else portable
        if (
            Path(root).is_absolute()
            or portable == ".."
            or portable.startswith("../")
        ):
            raise StateError("contract write_roots must stay inside the worktree")
        resolved = (canonical_worktree / portable).resolve()
        if not resolved.is_relative_to(canonical_worktree):
            raise StateError("contract write_roots must stay inside the worktree")
        normalized.append(portable)
    if len(set(normalized)) != len(normalized):
        raise StateError("contract write_roots must be unique")
    return normalized


def _normalized_contract(contract: dict) -> dict:
    if not isinstance(contract, dict):
        raise StateError("contract must be an object")
    required = {
        "acceptance",
        "actors",
        "branch",
        "commands",
        "intent",
        "non_scope",
        "owner",
        "pr",
        "repo",
        "scope",
        "worktree",
        "write_roots",
    }
    missing = required - set(contract)
    if missing:
        raise StateError(f"contract missing: {', '.join(sorted(missing))}")
    normalized = copy.deepcopy(contract)
    normalized["worktree"] = _canonical_path(contract.get("worktree", ""))
    normalized["write_roots"] = _validated_write_roots(
        normalized["worktree"],
        contract.get("write_roots"),
    )
    normalized["actors"] = _validated_actors(normalized)
    normalized["commands"] = _validated_commands(normalized)
    return normalized


def _actor_roles(contract: dict, actor: str) -> list[str]:
    return sorted(
        role for role, members in contract["actors"].items() if actor in members
    )


def _credential_digest(
    task_id: str,
    contract_digest: str,
    contract: dict,
    actor: str,
    credential: str,
) -> str:
    return digest(
        {
            "task": task_id,
            "contract_digest": contract_digest,
            "actor": actor,
            "roles": _actor_roles(contract, actor),
            "credential": credential,
        }
    )


def _validated_commands(contract: dict) -> dict[str, list[str]]:
    commands = contract.get("commands")
    if not isinstance(commands, dict) or set(commands) != {"painter"}:
        raise StateError("contract commands must define only Painter")
    approved = commands["painter"]
    if (
        not isinstance(approved, list)
        or any(
            not isinstance(command, str)
            or not command
            or command != command.strip()
            or "\n" in command
            or "\r" in command
            for command in approved
        )
        or len(set(approved)) != len(approved)
    ):
        raise StateError("Painter commands must be unique exact command strings")
    for command in approved:
        if re.search(r"""[\\'"`$;&|<>\n\r()]""", command):
            raise StateError(
                "Painter commands cannot use shell composition or indirection"
            )
        words = shlex.split(command)
        if (
            not words
            or words[0] in {"bash", "sh", "zsh", "fish"}
            or (words[0] in {"python", "python3", "node"} and any(
                flag in words[1:] for flag in {"-c", "-e", "--eval"}
            ))
            or (
                words[0] == "gh"
                and not (
                    len(words) >= 3
                    and words[1] == "pr"
                    and words[2] in {"view", "diff", "checks"}
                )
            )
            or (
                words[:2] == ["git", "push"]
                and (
                    len(words) != 4
                    or words[2] != "origin"
                    or words[3] != contract["branch"]
                    or words[3].casefold() in {"main", "master"}
                )
            )
            or words[:2] == ["npm", "publish"]
            or words[0] in {"curl", "wget"}
        ):
            raise StateError(
                "Painter command is outside the scoped command policy"
            )
    return {"painter": list(approved)}


def _validated_actors(contract: dict) -> dict[str, list[str]]:
    actors = contract.get("actors")
    if not isinstance(actors, dict):
        raise StateError("contract actors must be an object")
    required_roles = {"painter", "vigil"}
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

    painters = set(normalized["painter"])
    reviewers = set(normalized["vigil"])
    decision_actors = set(normalized.get("coordinator", [])) | set(
        normalized.get("user", [])
    )
    if painters & reviewers:
        raise StateError("Painter and Vigil actors must be independent")
    if painters & decision_actors:
        raise StateError("Painter actors cannot hold delivery authority")
    if reviewers & decision_actors:
        raise StateError("Vigil actors must remain read-only")
    if contract["owner"] not in decision_actors:
        raise StateError("task owner must be a registered coordinator or user actor")
    return normalized


def new_task(
    task_id: str,
    contract: dict,
    credential_hashes: dict[str, str],
) -> dict:
    if not isinstance(task_id, str) or not task_id:
        raise StateError("task id is required")
    normalized_contract = _normalized_contract(contract)
    if (
        not isinstance(normalized_contract["branch"], str)
        or not normalized_contract["branch"]
    ):
        raise StateError("contract branch is required")
    if (
        not isinstance(normalized_contract["repo"], str)
        or not normalized_contract["repo"]
    ):
        raise StateError("contract repository is required")
    if not str(normalized_contract["pr"]).isdigit():
        raise StateError("contract PR must be an explicit number")
    actors = normalized_contract["actors"]
    actor_ids = {actor for members in actors.values() for actor in members}
    if set(credential_hashes) != actor_ids or any(
        not FULL_DIGEST.fullmatch(value) for value in credential_hashes.values()
    ):
        raise StateError("every registered actor requires one credential hash")
    contract_digest = digest(normalized_contract)
    return {
        "schema": STATE_SCHEMA,
        "id": task_id,
        "revision": 1,
        "state": "approved",
        "contract": normalized_contract,
        "contract_digest": contract_digest,
        "credential_hashes": copy.deepcopy(credential_hashes),
        "writer": None,
        "head": None,
        "review": None,
        "authority": None,
        "authority_history": [],
        "delivery_attempt": None,
        "lane_map": None,
        "receipts": [],
        "next_action": "claim writer",
    }


def _validate_task_integrity(task_id: str, task: object) -> None:
    if (
        not isinstance(task, dict)
        or task.get("schema") != STATE_SCHEMA
        or task.get("id") != task_id
        or isinstance(task.get("revision"), bool)
        or not isinstance(task.get("revision"), int)
        or task["revision"] < 1
        or not isinstance(task.get("contract"), dict)
        or not isinstance(task.get("credential_hashes"), dict)
    ):
        raise StateError(f"invalid orchestration task: {task_id}")
    normalized_contract = _normalized_contract(task["contract"])
    if normalized_contract != task["contract"]:
        raise StateError(f"non-canonical orchestration contract: {task_id}")
    if task.get("contract_digest") != digest(normalized_contract):
        raise StateError(f"orchestration contract integrity failure: {task_id}")
    actor_ids = {
        actor
        for members in normalized_contract["actors"].values()
        for actor in members
    }
    if set(task["credential_hashes"]) != actor_ids or any(
        not isinstance(value, str) or not FULL_DIGEST.fullmatch(value)
        for value in task["credential_hashes"].values()
    ):
        raise StateError(f"invalid orchestration credentials: {task_id}")
    writer = task.get("writer")
    if writer is not None and (
        not isinstance(writer, dict)
        or writer.get("branch") != normalized_contract["branch"]
        or writer.get("worktree") != normalized_contract["worktree"]
        or not _actor_has_role(task, writer.get("actor", ""), "painter")
    ):
        raise StateError(f"invalid orchestration writer lease: {task_id}")
    checkpoint = task.get("checkpoint")
    if isinstance(checkpoint, dict) and checkpoint.get("status") == "pending":
        previous = checkpoint.get("previous_writer")
        if (
            not isinstance(previous, dict)
            or previous.get("branch") != normalized_contract["branch"]
            or previous.get("worktree") != normalized_contract["worktree"]
        ):
            raise StateError(f"invalid checkpoint reservation: {task_id}")


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
    observed = _credential_digest(
        task["id"],
        task["contract_digest"],
        task["contract"],
        actor,
        credential,
    )
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
        canonical_worktree = _canonical_path(worktree)
        if not _actor_has_role(candidate, actor, "painter"):
            raise StateError("writer is not registered for the Painter role")
        if candidate["state"] != "approved" or candidate["writer"]:
            raise StateError("task already has a writer")
        if branch != candidate["contract"]["branch"]:
            raise StateError("writer branch does not match the approved contract")
        if canonical_worktree != candidate["contract"]["worktree"]:
            raise StateError("writer worktree does not match the approved contract")
        candidate["writer"] = {
            "actor": actor,
            "branch": branch,
            "worktree": canonical_worktree,
        }
        candidate["state"] = "painting"
        candidate["next_action"] = "paint approved scope"

    _transition(task, expected_revision, change)


def _set_painted(
    task: dict,
    expected_revision: int,
    actor: str,
    head: str,
    checks: list[str],
) -> None:
    def change(candidate: dict) -> None:
        if (
            candidate["state"] not in {"painting", "changes_requested"}
            or not candidate["writer"]
            or candidate["writer"]["actor"] != actor
        ):
            raise StateError("only the claimed writer may paint")
        if not FULL_SHA.fullmatch(head):
            raise StateError("painted head must be a full commit SHA")
        if not isinstance(checks, list) or any(
            not isinstance(check, str) or not check for check in checks
        ):
            raise StateError("Painter checks must be a list of non-empty strings")
        _archive_authority(candidate, "invalidated")
        candidate.update(
            {
                "state": "painted",
                "head": head,
                "review": None,
                "next_action": "independent review",
            }
        )
        _receipt(
            candidate,
            "painted",
            f"Painted {head[:12]}; {len(checks)} checks passed.",
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
        if not _actor_has_role(candidate, actor, "vigil"):
            raise StateError("reviewer is not registered for the Vigil role")
        if not candidate["writer"] or candidate["writer"]["actor"] == actor:
            raise StateError("reviewer must be independent from writer")
        if candidate["state"] != "painted" or candidate["head"] != head:
            raise StateError("review must bind the current painted head")
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


def _approve_lanes(
    task: dict,
    expected_revision: int,
    actor: str,
    origin: str,
    lanes: list[dict],
) -> None:
    def change(candidate: dict) -> None:
        if origin not in {"coordinator", "user"} or not _actor_has_role(
            candidate, actor, origin
        ):
            raise StateError("lane approval requires a registered decision actor")
        if candidate["state"] != "approved" or candidate.get("writer"):
            raise StateError("lane map must be approved before writer claim")
        normalized = validate_lanes(lanes, True)
        candidate["lane_map"] = {
            "actor": actor,
            "origin": origin,
            "lanes": normalized,
            "digest": digest(normalized),
            "approved_at_revision": candidate["revision"],
        }
        candidate["next_action"] = "coordinate approved complex lanes"
        _receipt(
            candidate,
            "lane-map-approved",
            f"Approved {len(normalized)} complex lanes.",
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
        if _actor_has_role(candidate, actor, "painter"):
            raise StateError("Painter actors cannot grant delivery authority")
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
        candidate["delivery_attempt"] = None
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
    attempt = task.get("delivery_attempt")
    if attempt and attempt.get("status") == "prepared":
        attempt["status"] = "invalidated"
        attempt["invalidated_reason"] = reason
    task["review"] = None
    _archive_authority(task, "invalidated")
    task["state"] = "painting"
    task["next_action"] = "refresh proof and independent review"
    _receipt(task, "invalidated", f"Proof invalidated: {reason}.", "kernel")


def _consume_delivery(
    task: dict,
    expected_revision: int,
    actor: str,
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
            or authority.get("actor") != actor
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
        candidate["delivery_attempt"] = {
            "id": grant_id,
            "actor": actor,
            "action": normalized_action,
            "action_digest": digest(normalized_action),
            "head": live_head,
            "status": "prepared",
            "prepared_at_revision": candidate["revision"],
        }
        candidate["state"] = "delivery_started"
        candidate["next_action"] = "run the single authorized external action"
        _receipt(
            candidate,
            "delivery-attempt",
            "One-shot authority consumed after live-head refresh.",
            "delivery-gate",
        )

    _transition(task, expected_revision, change)


def _recover_delivery(
    task: dict,
    expected_revision: int,
    actor: str,
    grant_id: str,
    observe_status: Callable[[str, str], dict] = observe_pr_status,
) -> None:
    def change(candidate: dict) -> None:
        authority = candidate.get("authority") or {}
        attempt = candidate.get("delivery_attempt") or {}
        if (
            candidate.get("state") != "delivery_started"
            or authority.get("id") != grant_id
            or authority.get("status") != "consumed"
            or authority.get("actor") != actor
            or attempt.get("id") != grant_id
            or attempt.get("status") != "prepared"
        ):
            raise StateError("no recoverable delivery attempt for this actor")
        action = validate_delivery_action(
            attempt.get("action"),
            candidate["contract"]["repo"],
            str(candidate["contract"]["pr"]),
        )
        observed = observe_status(action["repo"], action["pr"])
        if (
            not isinstance(observed, dict)
            or observed.get("head") != attempt.get("head")
        ):
            _invalidate(candidate, "delivery recovery observed head drift")
            raise HeadDriftError(
                "delivery recovery head does not match the prepared attempt"
            )
        if observed.get("merged"):
            attempt["status"] = "completed"
            attempt["recovered"] = True
            attempt["finished_at_revision"] = candidate["revision"]
            authority["recovered_at_revision"] = candidate["revision"]
            candidate["state"] = "complete"
            candidate["next_action"] = "none"
            _receipt(
                candidate,
                "delivery-recovered",
                "Observed the prepared exact-head PR already merged.",
                actor,
            )
            return
        if observed.get("state") != "OPEN":
            attempt["status"] = "failed"
            candidate["state"] = "delivery_failed"
            candidate["next_action"] = "user decides whether to retry"
            _receipt(
                candidate,
                "delivery-recovery-failed",
                "Prepared PR is closed without a merge.",
                actor,
            )
            return
        attempt["recovery_checked_at_revision"] = candidate["revision"]
        _receipt(
            candidate,
            "delivery-retry",
            "Prepared exact-head PR remains open; retry the same action.",
            actor,
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
            not in {"painting", "painted", "changes_requested", "review_passed"}
            or not writer
            or writer["actor"] != actor
        ):
            raise StateError("only the claimed writer may checkpoint")
        if (
            not _actor_has_role(candidate, successor, "painter")
            or successor == actor
        ):
            raise StateError(
                "checkpoint requires one distinct registered Painter successor"
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
            or not _actor_has_role(candidate, actor, "painter")
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
        candidate["state"] = "painting"
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
        return {"schema": STATE_SCHEMA, "tasks": {}}
    try:
        store = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"unable to read orchestration state: {path}") from error
    if (
        not isinstance(store, dict)
        or store.get("schema") != STATE_SCHEMA
        or not isinstance(store.get("tasks"), dict)
    ):
        raise StateError(f"invalid orchestration state: {path}")
    for task_id, task in store["tasks"].items():
        _validate_task_integrity(task_id, task)
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
    normalized_contract = _normalized_contract(contract)
    actors = normalized_contract["actors"]
    actor_ids = {actor for members in actors.values() for actor in members}
    if set(credentials) != actor_ids or any(
        not isinstance(value, str) or not value for value in credentials.values()
    ):
        raise StateError("every registered actor requires one credential")
    contract_digest = digest(normalized_contract)
    credential_hashes = {
        actor: _credential_digest(
            task_id,
            contract_digest,
            normalized_contract,
            actor,
            credential,
        )
        for actor, credential in credentials.items()
    }
    with locked_store(path) as store:
        if task_id in store["tasks"]:
            raise StateError(f"task already exists: {task_id}")
        store["tasks"][task_id] = new_task(
            task_id,
            normalized_contract,
            credential_hashes,
        )
        result = copy.deepcopy(store["tasks"][task_id])
    return result


def provision_task(path: Path, task_id: str, contract: dict) -> dict:
    actors = _normalized_contract(contract)["actors"]
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


def orchestrate_lanes(
    path: Path,
    task_id: str,
    actor: str,
    credential: str,
    lanes: list[dict],
) -> dict:
    task = load_task(path, task_id)
    approval = task.get("lane_map") or {}
    if actor != approval.get("actor"):
        raise StateError("complex lanes require the approving decision actor")
    _authenticate(task, actor, credential, approval.get("origin", ""))
    normalized = validate_lanes(lanes, True)
    if not approval or approval.get("digest") != digest(normalized):
        raise StateError("lane map does not match immutable decision approval")
    return {
        "approved": True,
        "actor": actor,
        "digest": approval["digest"],
        "lanes": normalized,
        "status": "validated",
        "task": task_id,
    }


def _reserved_writer(task: dict) -> dict | None:
    if task.get("state") == "complete":
        return None
    writer = task.get("writer")
    if isinstance(writer, dict):
        return writer
    checkpoint = task.get("checkpoint")
    if (
        isinstance(checkpoint, dict)
        and checkpoint.get("status") == "pending"
        and isinstance(checkpoint.get("previous_writer"), dict)
    ):
        return checkpoint["previous_writer"]
    return None


def _assert_writer_reservation_available(
    store: dict,
    record_id: str,
    repo: str,
    branch: str,
    worktree: str,
) -> None:
    canonical_worktree = _canonical_path(worktree)
    for other_id, other_task in store["tasks"].items():
        if other_id == record_id:
            continue
        reservation = _reserved_writer(other_task)
        if not reservation:
            continue
        same_branch = (
            other_task["contract"]["repo"] == repo
            and reservation.get("branch") == branch
        )
        same_worktree = (
            _canonical_path(reservation.get("worktree", "")) == canonical_worktree
        )
        if same_branch or same_worktree:
            raise StateError(
                "writer reservation conflicts with another task branch or worktree"
            )


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
                _assert_writer_reservation_available(
                    store,
                    record_id,
                    task["contract"]["repo"],
                    arguments.get("branch", ""),
                    arguments.get("worktree", ""),
                )
                _authenticate(task, arguments.get("actor", ""), credential, "painter")
                _claim(task, expected_revision, **arguments)
            elif operation == "painted":
                _authenticate(task, arguments.get("actor", ""), credential, "painter")
                _set_painted(task, expected_revision, **arguments)
            elif operation == "review":
                _authenticate(
                    task,
                    arguments.get("actor", ""),
                    credential,
                    "vigil",
                )
                _review(task, expected_revision, **arguments)
            elif operation == "approve-lanes":
                origin = arguments.get("origin", "")
                if origin not in {"coordinator", "user"}:
                    raise StateError("lane approval origin is invalid")
                _authenticate(task, arguments.get("actor", ""), credential, origin)
                _approve_lanes(task, expected_revision, **arguments)
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
                authority = task.get("authority") or {}
                actor = arguments.get("actor", "")
                if actor != authority.get("actor"):
                    raise StateError(
                        "only the active decision actor may consume delivery authority"
                    )
                _authenticate(
                    task,
                    actor,
                    credential,
                    authority.get("origin", ""),
                )
                _consume_delivery(task, expected_revision, **arguments)
            elif operation == "recover-delivery":
                authority = task.get("authority") or {}
                actor = arguments.get("actor", "")
                if actor != authority.get("actor"):
                    raise StateError(
                        "only the active decision actor may recover delivery"
                    )
                _authenticate(
                    task,
                    actor,
                    credential,
                    authority.get("origin", ""),
                )
                _recover_delivery(task, expected_revision, **arguments)
            elif operation == "checkpoint":
                _authenticate(task, arguments.get("actor", ""), credential, "painter")
                _checkpoint(task, expected_revision, **arguments)
            elif operation == "resume-checkpoint":
                _assert_writer_reservation_available(
                    store,
                    record_id,
                    task["contract"]["repo"],
                    task["contract"]["branch"],
                    task["contract"]["worktree"],
                )
                _authenticate(task, arguments.get("actor", ""), credential, "painter")
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
    normalized_tool = tool_name.lower() if isinstance(tool_name, str) else ""
    tool_checks = (
        ("merge_pull_request", "direct GitHub merge"),
        ("close_issue", "direct issue closure"),
        ("publish", "direct package publish"),
        ("production_deploy", "direct production deploy"),
        ("deploy_production", "direct production deploy"),
        ("release", "direct release"),
    )
    for marker, reason in tool_checks:
        if marker in normalized_tool:
            return reason
    tool_input = payload.get("tool_input")
    if not _shell_capable_tool(payload) or not isinstance(tool_input, dict):
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


def _shell_capable_tool(payload: dict) -> bool:
    tool_name = payload.get("tool_name", "")
    return tool_name == "Bash" or (
        isinstance(tool_name, str)
        and re.search(
            r"__(?:exec|exec_command|shell|execute|run|terminal)(?:_.*)?$",
            tool_name,
        )
        is not None
    )


def _vigil_read_only_tool(payload: dict) -> bool:
    tool_name = payload.get("tool_name", "")
    return isinstance(tool_name, str) and tool_name in VIGIL_READ_ONLY_TOOLS


def _ordinary_command_is_read_only(command: str) -> bool:
    if (
        not isinstance(command, str)
        or not command
        or re.search(r"""[\\'"`$;&|<>\n\r()]""", command)
    ):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words:
        return False
    if words[0] == "git" and len(words) >= 2:
        if words[1] not in {"diff", "status", "show", "log", "rev-parse"}:
            return False
        forbidden = {
            "--ext-diff",
            "--textconv",
            "--output",
            "--paginate",
            "-p",
        }
        return not any(
            word in forbidden or word.startswith("--output=") for word in words[2:]
        )
    if words[0] == "gh" and len(words) >= 3:
        return words[1] == "pr" and words[2] in {"view", "diff", "checks"}
    if words[0] == "rg":
        return not any(
            word == "--pre" or word.startswith("--pre=") for word in words[1:]
        )
    return words[0] in {"ls", "head", "tail", "pwd"} and words[0] == command.split()[0]


def _control_operation(command: str) -> str | None:
    match = re.fullmatch(
        r"\s*(?:/usr/bin/env\s+)?python3?\s+"
        r"(?P<script>[A-Za-z0-9_./:-]*voice_state\.py)\s+"
        r"(create|transition|run|deliver|recover-delivery|classify|orchestrate)"
        r"\s*(?:<\s*[A-Za-z0-9_./:-]+)?\s*",
        command,
    )
    if not match:
        return None
    script = Path(match.group("script"))
    if not script.is_absolute() or script.resolve() != Path(__file__).resolve():
        return None
    return match.group(2)


def _command_from_payload(payload: dict) -> str:
    if not _shell_capable_tool(payload):
        return ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    command = tool_input.get("command", tool_input.get("cmd", ""))
    return command if isinstance(command, str) else ""


def _requested_workdir(payload: dict, fallback: str) -> str:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return _canonical_path(fallback)
    requested = {
        key: tool_input[key]
        for key in ("workdir", "cwd")
        if key in tool_input
        and tool_input[key] is not None
        and tool_input[key] != ""
    }
    if any(not isinstance(value, str) for value in requested.values()):
        raise StateError("tool workdir must be a path string")
    canonical = {_canonical_path(value) for value in requested.values()}
    if len(canonical) > 1:
        raise StateError("tool workdir fields disagree")
    return next(iter(canonical), _canonical_path(fallback))


def _reject_environment_override(payload: dict) -> None:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    for key in ("env", "environment"):
        if (
            key not in tool_input
            or tool_input[key] is None
            or tool_input[key] == {}
        ):
            continue
        value = tool_input[key]
        if not isinstance(value, dict):
            raise StateError("tool environment override must be an object")
        if PROTECTED_ENVIRONMENT & set(value):
            raise StateError("tool cannot override orchestration identity")
        raise StateError("control commands cannot override their environment")


def _apply_patch_targets(payload: dict) -> list[str]:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        raise StateError("apply_patch requires an inspectable patch payload")
    patch = next(
        (
            value
            for key in ("patch", "input")
            if isinstance((value := tool_input.get(key)), str)
        ),
        "",
    )
    targets = re.findall(
        r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$",
        patch,
        flags=re.MULTILINE,
    )
    flattened = [left or right for left, right in targets]
    if not flattened:
        raise StateError("apply_patch must declare every target path")
    return flattened


def _structured_write_targets(payload: dict) -> list[str]:
    tool_name = payload.get("tool_name", "")
    if tool_name in {"apply_patch", "functions__apply_patch"}:
        return _apply_patch_targets(payload)
    supported = {
        "Edit",
        "Write",
        "mcp__filesystem__edit",
        "mcp__filesystem__write",
        "mcp__filesystem__write_file",
    }
    if tool_name not in supported:
        raise StateError("write-capable tool has no approved target adapter")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        raise StateError("write tool requires an inspectable target")
    targets = [
        tool_input[key]
        for key in ("file_path", "path", "file")
        if key in tool_input
    ]
    if len(targets) != 1 or not isinstance(targets[0], str) or not targets[0]:
        raise StateError("write tool requires exactly one target path")
    return targets


def _path_is_within(path: Path, roots: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _validate_painter_targets(
    task: dict,
    state_path: Path,
    payload: dict,
    workdir: str,
) -> None:
    worktree = Path(task["contract"]["worktree"])
    roots = [
        (worktree / root).resolve()
        for root in task["contract"]["write_roots"]
    ]
    protected = {
        state_path.resolve(),
        state_path.with_suffix(state_path.suffix + ".lock").resolve(),
        Path(__file__).resolve(),
    }
    for raw_target in _structured_write_targets(payload):
        candidate = Path(raw_target)
        target = (
            candidate.resolve()
            if candidate.is_absolute()
            else (Path(workdir) / candidate).resolve()
        )
        if (
            not target.is_relative_to(worktree)
            or not _path_is_within(target, roots)
            or target in protected
            or target == worktree / ".git"
            or target.is_relative_to(worktree / ".git")
        ):
            raise StateError("write target is outside the approved Painter scope")


def _authenticate_registered_actor(
    task: dict,
    actor: str,
    credential: str,
) -> str:
    roles = _actor_roles(task["contract"], actor)
    if not roles:
        raise StateError("control command actor is not registered")
    _authenticate(task, actor, credential, roles[0])
    return roles[0]


def _validate_control_actor(
    task: dict,
    operation: str,
    actor: str,
    credential: str,
) -> None:
    if operation == "run":
        _authenticate(task, actor, credential, "painter")
        writer = task.get("writer")
        if (
            task["state"] not in {"painting", "changes_requested"}
            or not writer
            or writer.get("actor") != actor
        ):
            raise StateError("command runner requires the active Painter lease")
        return
    if operation in {"deliver", "recover-delivery"}:
        authority = task.get("authority") or {}
        if actor != authority.get("actor"):
            raise StateError(
                "typed delivery requires the active decision actor context"
            )
        _authenticate(task, actor, credential, authority.get("origin", ""))
        return
    if operation == "orchestrate":
        approval = task.get("lane_map") or {}
        if actor != approval.get("actor"):
            raise StateError(
                "complex lanes require the approving decision actor context"
            )
        _authenticate(task, actor, credential, approval.get("origin", ""))
        return
    _authenticate_registered_actor(task, actor, credential)


def _validate_tool_lease(payload: dict, environment: dict[str, str]) -> None:
    names = {
        "state": "CODEX_ORCHESTRATION_STATE",
        "task": "CODEX_ORCHESTRATION_TASK",
        "actor": "CODEX_ORCHESTRATION_ACTOR",
        "credential": "CODEX_ORCHESTRATION_CREDENTIAL",
        "worktree": "CODEX_ORCHESTRATION_WORKTREE",
    }
    present = {key: environment.get(name, "") for key, name in names.items()}
    command = _command_from_payload(payload)
    operation = _control_operation(command)
    if not any(present.values()):
        if not _shell_capable_tool(payload):
            return
        if operation in {"create", "classify"}:
            return
        if _ordinary_command_is_read_only(command):
            return
        raise StateError(
            "non-read-only Bash requires a credentialed durable task context"
        )
    missing = [names[key] for key, value in present.items() if not value]
    if missing:
        raise StateError(f"incomplete orchestration writer context: {', '.join(missing)}")
    task = load_task(Path(present["state"]), present["task"])
    actor = present["actor"]
    credential = present["credential"]
    actual_worktree = _canonical_path(environment.get("PWD", os.getcwd()))
    context_worktree = _canonical_path(present["worktree"])
    if actual_worktree != context_worktree:
        raise StateError("tool worktree does not match orchestration context")
    requested_worktree = _requested_workdir(payload, actual_worktree)
    if requested_worktree != context_worktree:
        raise StateError("requested tool workdir does not match orchestration context")
    if operation:
        _reject_environment_override(payload)
        if operation == "create":
            raise StateError("task creation cannot run inside another task context")
        if _actor_has_role(task, actor, "vigil") and operation != "transition":
            _authenticate(task, actor, credential, "vigil")
            raise StateError(
                "Vigil can execute only the canonical locked transition command"
            )
        if operation == "classify":
            raise StateError("classification runs only outside durable task context")
        _validate_control_actor(task, operation, actor, credential)
        if operation == "run":
            writer = task.get("writer") or {}
            if writer.get("worktree") != context_worktree:
                raise StateError(
                    "command runner worktree does not match the active Painter lease"
                )
        return
    if _actor_has_role(task, actor, "vigil"):
        _authenticate(task, actor, credential, "vigil")
        if _vigil_read_only_tool(payload):
            return
        raise StateError("Vigil can use only explicit read-only inspection tools")
    if _vigil_read_only_tool(payload):
        _authenticate_registered_actor(task, actor, credential)
        return
    _authenticate(task, actor, credential, "painter")
    writer = task.get("writer")
    recorded_worktree = (
        str(Path(writer.get("worktree", "")).resolve()) if writer else ""
    )
    if (
        task["state"] not in {"painting", "changes_requested"}
        or not writer
        or writer.get("actor") != present["actor"]
        or recorded_worktree != context_worktree
    ):
        raise StateError("write tool requires the active credentialed writer lease")
    if _shell_capable_tool(payload):
        raise StateError(
            "Painter commands must use the canonical voice_state.py run gateway"
        )
    _validate_painter_targets(
        task,
        Path(present["state"]),
        payload,
        requested_worktree,
    )


def handle_hook(payload: dict, environment: dict[str, str] | None = None) -> None:
    reason = direct_delivery_reason(payload)
    if reason:
        raise StateError(
            f"{reason} is blocked; use the typed voice_state.py deliver command"
        )
    _validate_tool_lease(
        payload,
        dict(os.environ) if environment is None else environment,
    )


@contextmanager
def locked_task_snapshot(path: Path, task_id: str):
    lock = path.with_suffix(path.suffix + ".lock")
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock.open("a+") as handle:
        os.chmod(lock, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            store = _read_store(path)
            try:
                task = copy.deepcopy(store["tasks"][task_id])
            except KeyError as error:
                raise StateError(f"unknown task: {task_id}") from error
            yield task
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _credential_paths() -> list[Path]:
    user_home = Path.home().resolve()
    return [
        user_home / ".ssh",
        user_home / ".git-credentials",
        user_home / ".config" / "gh",
        user_home / ".config" / "hub",
        user_home / ".npmrc",
        user_home / ".vercel",
        user_home / ".aws",
        user_home / ".azure",
        user_home / ".docker",
        user_home / ".kube",
        user_home / ".codex" / "auth.json",
    ]


def _sandbox_profile(
    state_path: Path,
    worktree: Path,
    write_roots: list[Path],
    sandbox_home: Path,
) -> str:
    allowed_writes = [*write_roots, sandbox_home]
    protected_writes = [
        state_path.resolve(),
        state_path.with_suffix(state_path.suffix + ".lock").resolve(),
        (worktree / ".git").resolve(),
        Path(__file__).resolve(),
    ]
    protected_reads = [
        state_path.resolve(),
        state_path.with_suffix(state_path.suffix + ".lock").resolve(),
        *_credential_paths(),
    ]

    def filters(paths: list[Path]) -> str:
        return " ".join(
            f'(subpath {json.dumps(str(path.resolve()))})' for path in paths
        )

    return " ".join(
        [
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow file-read*)",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow ipc-posix*)",
            f"(allow file-write* {filters(allowed_writes)})",
            f"(deny file-write* {filters(protected_writes)})",
            f"(deny file-read* {filters(protected_reads)})",
            '(deny process-exec (literal "/usr/bin/security"))',
        ]
    )


def _sandboxed_command(
    state_path: Path,
    worktree: Path,
    write_roots: list[Path],
    words: list[str],
    run_command=subprocess.run,
):
    executable = shutil.which(words[0], path=TRUSTED_PATH)
    if not executable:
        raise StateError(f"approved command executable is not trusted: {words[0]}")
    executable_path = Path(executable).resolve()
    if executable_path == worktree or executable_path.is_relative_to(worktree):
        raise StateError("approved command executable cannot come from the worktree")
    with tempfile.TemporaryDirectory(prefix="voice-command-") as temporary:
        sandbox_home = Path(temporary).resolve()
        (sandbox_home / "tmp").mkdir(mode=0o700)
        environment = {
            "HOME": str(sandbox_home),
            "PATH": TRUSTED_PATH,
            "TMPDIR": str(sandbox_home / "tmp"),
            "XDG_CONFIG_HOME": str(sandbox_home / "config"),
            "GH_CONFIG_DIR": str(sandbox_home / "gh"),
            "NPM_CONFIG_USERCONFIG": str(sandbox_home / "npmrc"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        }
        system = platform.system()
        if system == "Darwin":
            sandbox = Path("/usr/bin/sandbox-exec")
            if not sandbox.exists():
                raise StateError("secure Painter command sandbox is unavailable")
            command = [
                str(sandbox),
                "-p",
                _sandbox_profile(
                    state_path,
                    worktree,
                    write_roots,
                    sandbox_home,
                ),
                str(executable_path),
                *words[1:],
            ]
        elif system == "Linux":
            bubblewrap = shutil.which("bwrap", path=TRUSTED_PATH)
            if not bubblewrap:
                raise StateError(
                    "secure Painter command sandbox requires bubblewrap on Linux"
                )
            command = [
                bubblewrap,
                "--die-with-parent",
                "--unshare-net",
                "--ro-bind",
                "/",
                "/",
                "--dev-bind",
                "/dev",
                "/dev",
                "--proc",
                "/proc",
            ]
            for root in [*write_roots, sandbox_home]:
                command.extend(["--bind", str(root), str(root)])
            for protected in [
                (worktree / ".git").resolve(),
            ]:
                if protected.exists():
                    command.extend(["--ro-bind", str(protected), str(protected)])
            for protected in [
                state_path.resolve(),
                state_path.with_suffix(state_path.suffix + ".lock").resolve(),
                *_credential_paths(),
            ]:
                if protected.is_dir():
                    command.extend(["--tmpfs", str(protected)])
                elif protected.exists():
                    command.extend(["--ro-bind", "/dev/null", str(protected)])
            command.extend(["--", str(executable_path), *words[1:]])
        else:
            raise StateError("secure Painter command sandbox is unsupported")
        return run_command(
            command,
            cwd=str(worktree),
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )


def _github_remote_matches(repo: str, remote: str) -> bool:
    normalized = remote.strip().removesuffix(".git")
    candidates = {
        f"https://github.com/{repo}",
        f"http://github.com/{repo}",
        f"git@github.com:{repo}",
        f"ssh://git@github.com/{repo}",
    }
    return normalized in candidates


def _run_typed_branch_push(
    task: dict,
    words: list[str],
    run_command=subprocess.run,
):
    worktree = task["contract"]["worktree"]
    branch = task["contract"]["branch"]
    git = shutil.which("git", path=TRUSTED_PATH)
    if not git:
        raise StateError("trusted git executable is unavailable")
    environment = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PATH": TRUSTED_PATH,
    }
    if os.environ.get("SSH_AUTH_SOCK"):
        environment["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
    if words != ["git", "push", "origin", branch]:
        raise StateError("branch push must exactly match the approved branch")
    remote_result = run_command(
        [git, "-C", worktree, "remote", "get-url", "--push", "--all", "origin"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    remotes = [
        line.strip()
        for line in remote_result.stdout.splitlines()
        if line.strip()
    ]
    if (
        len(remotes) != 1
        or not _github_remote_matches(task["contract"]["repo"], remotes[0])
    ):
        raise StateError("origin remote does not match the approved repository")
    remote = remotes[0]
    head = run_command(
        [git, "-C", worktree, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()
    if not FULL_SHA.fullmatch(head):
        raise StateError("worktree returned an invalid head")
    return run_command(
        [
            git,
            "-C",
            worktree,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.helper=",
            "-c",
            (
                "core.sshCommand=/usr/bin/ssh -F /dev/null "
                "-o ClearAllForwardings=yes -o PermitLocalCommand=no"
            ),
            "-c",
            "protocol.ext.allow=never",
            "push",
            "--porcelain",
            remote,
            f"{head}:refs/heads/{branch}",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def run_painter_command(
    path: Path,
    task_id: str,
    actor: str,
    credential: str,
    command: str,
    workdir: str,
    run_command=subprocess.run,
) -> dict:
    with locked_task_snapshot(path, task_id) as task:
        _authenticate(task, actor, credential, "painter")
        writer = task.get("writer")
        canonical_workdir = _canonical_path(workdir)
        if (
            task["state"] not in {"painting", "changes_requested"}
            or not writer
            or writer.get("actor") != actor
            or writer.get("worktree") != canonical_workdir
            or task["contract"]["worktree"] != canonical_workdir
        ):
            raise StateError("command runner requires the active Painter worktree lease")
        if command not in task["contract"]["commands"]["painter"]:
            raise StateError("command is not an exact contract-approved Painter command")
        words = shlex.split(command)
        if words[:2] == ["git", "push"]:
            result = _run_typed_branch_push(task, words, run_command)
        else:
            roots = [
                (Path(canonical_workdir) / root).resolve()
                for root in task["contract"]["write_roots"]
            ]
            result = _sandboxed_command(
                path,
                Path(canonical_workdir),
                roots,
                words,
                run_command,
            )
        return {
            "command": command,
            "exit_code": int(result.returncode),
            "revision": task["revision"],
            "stderr": getattr(result, "stderr", "") or "",
            "stdout": getattr(result, "stdout", "") or "",
            "task": task_id,
        }


def run_delivery(
    path: Path,
    task_id: str,
    expected_revision: int,
    actor: str,
    credential: str,
    grant_id: str,
    action: dict,
    observe_head: Callable[[str, str], str] = observe_pr_head,
    observe_status: Callable[[str, str], dict] = observe_pr_status,
    run_command=subprocess.run,
    after_command: Callable[[], None] | None = None,
) -> dict:
    normalized_action = validate_delivery_action(action)
    started = mutate_task(
        path,
        task_id,
        expected_revision,
        "consume-delivery",
        credential=credential,
        actor=actor,
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
    if after_command is not None:
        after_command()
    if exit_code != 0:
        raise StateError(
            f"typed delivery action returned exit {exit_code}; "
            "recover-delivery is required"
        )
    observed = mutate_task(
        path,
        task_id,
        started["revision"],
        "recover-delivery",
        credential=credential,
        actor=actor,
        grant_id=grant_id,
        observe_status=observe_status,
    )
    if observed["state"] != "complete":
        raise StateError(
            "typed delivery action is not yet observably merged; "
            "recover-delivery is required"
        )
    return observed


def recover_delivery(
    path: Path,
    task_id: str,
    expected_revision: int,
    actor: str,
    credential: str,
    grant_id: str,
    observe_status: Callable[[str, str], dict] = observe_pr_status,
    run_command=subprocess.run,
    after_command: Callable[[], None] | None = None,
) -> dict:
    recovered = mutate_task(
        path,
        task_id,
        expected_revision,
        "recover-delivery",
        credential=credential,
        actor=actor,
        grant_id=grant_id,
        observe_status=observe_status,
    )
    if recovered["state"] != "delivery_started":
        return recovered
    attempt = recovered["delivery_attempt"]
    action = validate_delivery_action(attempt["action"])
    command = [
        "gh",
        "pr",
        "merge",
        action["pr"],
        "--repo",
        action["repo"],
        f"--{action['method']}",
        "--match-head-commit",
        attempt["head"],
    ]
    result = run_command(command, check=False)
    exit_code = int(result.returncode)
    if after_command is not None:
        after_command()
    if exit_code != 0:
        raise StateError(
            f"typed delivery recovery returned exit {exit_code}; "
            "recover-delivery remains required"
        )
    observed = mutate_task(
        path,
        task_id,
        recovered["revision"],
        "recover-delivery",
        credential=credential,
        actor=actor,
        grant_id=grant_id,
        observe_status=observe_status,
    )
    if observed["state"] != "complete":
        raise StateError(
            "typed delivery retry is not yet observably merged; "
            "recover-delivery remains required"
        )
    return observed


def _read_cli_payload() -> dict:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise StateError("command input must be a JSON object")
    return payload


def _require_cli_identity(payload: dict, actor: str, credential: str) -> None:
    required = {
        name: os.environ.get(name, "")
        for name in PROTECTED_ENVIRONMENT
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise StateError(
            f"durable control command missing context: {', '.join(sorted(missing))}"
        )
    if (
        _canonical_path(payload.get("state", ""))
        != _canonical_path(required["CODEX_ORCHESTRATION_STATE"])
        or payload.get("task") != required["CODEX_ORCHESTRATION_TASK"]
        or actor != required["CODEX_ORCHESTRATION_ACTOR"]
        or not hmac.compare_digest(
            str(credential),
            required["CODEX_ORCHESTRATION_CREDENTIAL"],
        )
        or _canonical_path(os.getcwd())
        != _canonical_path(required["CODEX_ORCHESTRATION_WORKTREE"])
    ):
        raise StateError("control payload does not match durable task context")


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
                "painted",
                "review",
                "approve-lanes",
                "grant-delivery",
                "revoke-delivery",
                "checkpoint",
                "resume-checkpoint",
            }:
                raise StateError("operation is not available through the control CLI")
            _require_cli_identity(
                payload,
                payload.get("arguments", {}).get("actor", ""),
                payload["credential"],
            )
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
        if arguments == ["run"]:
            payload = _read_cli_payload()
            if set(payload) != {
                "state",
                "task",
                "actor",
                "credential",
                "command",
                "worktree",
            }:
                raise StateError(
                    "command runner requires exact task, actor, command, and worktree input"
                )
            _require_cli_identity(payload, payload["actor"], payload["credential"])
            if (
                _canonical_path(payload["worktree"])
                != _canonical_path(
                    os.environ["CODEX_ORCHESTRATION_WORKTREE"]
                )
            ):
                raise StateError("command payload worktree does not match task context")
            result = run_painter_command(
                Path(payload["state"]),
                payload["task"],
                payload["actor"],
                payload["credential"],
                payload["command"],
                payload["worktree"],
            )
            print(json.dumps(result, sort_keys=True))
            return 0 if result["exit_code"] == 0 else 2
        if arguments == ["deliver"]:
            payload = _read_cli_payload()
            _require_cli_identity(payload, payload["actor"], payload["credential"])
            result = run_delivery(
                Path(payload["state"]),
                payload["task"],
                int(payload["revision"]),
                payload["actor"],
                payload["credential"],
                payload["grant"],
                payload["action"],
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if arguments == ["recover-delivery"]:
            payload = _read_cli_payload()
            _require_cli_identity(payload, payload["actor"], payload["credential"])
            result = recover_delivery(
                Path(payload["state"]),
                payload["task"],
                int(payload["revision"]),
                payload["actor"],
                payload["credential"],
                payload["grant"],
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if arguments == ["classify"]:
            payload = _read_cli_payload()
            expected = {
                "needs_branch",
                "concurrent",
                "external_effect",
                "high_risk",
            }
            if set(payload) != expected or any(
                not isinstance(payload[name], bool) for name in expected
            ):
                raise StateError("classification requires four explicit booleans")
            print(
                json.dumps(
                    {
                        "class": work_class(
                            payload["needs_branch"],
                            payload["concurrent"],
                            payload["external_effect"],
                            payload["high_risk"],
                        )
                    },
                    sort_keys=True,
                )
            )
            return 0
        if arguments == ["orchestrate"]:
            payload = _read_cli_payload()
            if set(payload) != {
                "state",
                "task",
                "actor",
                "credential",
                "lanes",
            }:
                raise StateError(
                    "orchestration requires task and decision actor approval"
                )
            _require_cli_identity(payload, payload["actor"], payload["credential"])
            result = orchestrate_lanes(
                Path(payload["state"]),
                payload["task"],
                payload["actor"],
                payload["credential"],
                payload["lanes"],
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        print(
            "usage: voice_state.py create|transition|run|deliver|recover-delivery|classify|orchestrate|hook",
            file=sys.stderr,
        )
        return 1
    except Exception as error:
        print(f"voice-state: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
