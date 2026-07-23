#!/usr/bin/env python3
"""Small, private state registry for Codex orchestration hooks."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
ROLES = {"gepetto", "jiminy", "research", "implementation", "review"}
PROOF_DOMAINS = ("contract", "base", "head")
INVALIDATION_EVENTS = {
    "MATERIAL_CONTRACT_CHANGED": "contract",
    "MATERIAL_BASE_CHANGED": "base",
    "PR_HEAD_CHANGED": "head",
}
PROTECTED_CONTEXT_KEYS = {
    "acceptance_receipts", "expected_pr_urls", "merge_ready",
    "proof_lifecycle", "ready", "resume_node", "reviewed_head_sha",
}
FULL_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
CONTENT_REF_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
REVIEW_ATTEMPT_PATTERN = CONTENT_REF_PATTERN


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


def continuation_journal_path() -> Path:
    return state_root() / "transactions" / "continuation.json"


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def recover_transactions() -> None:
    journal_path = continuation_journal_path()
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    if not isinstance(journal, dict) or journal.get("kind") != "continuation-v1":
        raise ValueError(f"invalid continuation journal: {journal_path}")
    source_id = _safe_id(str(journal.get("source_id", "")))
    successor_id = _safe_id(str(journal.get("successor_id", "")))
    source_next = journal.get("source_next")
    successor = journal.get("successor")
    if not isinstance(source_next, dict) or not isinstance(successor, dict):
        raise ValueError(f"invalid continuation journal payload: {journal_path}")
    current_successor = load_state(successor_id)
    write_state(
        successor_id,
        successor,
        expected_revision=(int(current_successor.get("state_revision", 0)) if current_successor else None),
    )
    current_source = load_state(source_id)
    if current_source is None:
        raise ValueError(f"continuation source disappeared during recovery: {source_id}")
    write_state(
        source_id,
        source_next,
        expected_revision=int(current_source.get("state_revision", 0)),
    )
    journal_path.unlink()


@contextmanager
def registry_lock():
    root = state_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    lock_path = root / ".registry.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


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


def _stage_state(session_id: str, value: dict[str, Any], expected_revision: int | None) -> tuple[Path, Path]:
    path = state_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    current = load_state(session_id)
    current_revision = int(current.get("state_revision", 0)) if current else None
    if expected_revision is not None and current_revision != expected_revision:
        raise ValueError(
            f"state revision conflict for {session_id}: expected {expected_revision}, observed {current_revision}"
        )
    next_revision = (current_revision or 0) + 1
    value["session_id"] = session_id
    value["state_revision"] = next_revision
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return path, temporary


def write_state(
    session_id: str,
    value: dict[str, Any],
    *,
    expected_revision: int | None = None,
) -> Path:
    path, temporary = _stage_state(session_id, value, expected_revision)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
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
    if role == "gepetto" and coordinator_thread_id is not None:
        raise ValueError("Gepetto registration cannot have a coordinator")
    if role != "gepetto" and coordinator_thread_id is None:
        raise ValueError(f"{role} registration requires an active Gepetto coordinator")
    existing = load_state(session_id) or {}
    if coordinator_thread_id:
        coordinator_thread_id = _safe_id(coordinator_thread_id)
        coordinator = load_state(coordinator_thread_id)
        if not coordinator or coordinator.get("role") != "gepetto" or not coordinator.get("active"):
            raise ValueError(
                f"child registration requires an active Gepetto coordinator: {coordinator_thread_id}"
            )
    if existing:
        registered_role = existing.get("role")
        registered_coordinator = existing.get("coordinator_thread_id")
        if registered_role != role:
            raise ValueError(
                f"conflicting registration for {session_id}: role {registered_role} is already authoritative"
            )
        if registered_coordinator != coordinator_thread_id:
            raise ValueError(
                f"conflicting registration for {session_id}: coordinator "
                f"{registered_coordinator or '-'} is already authoritative"
            )
        if not existing.get("active"):
            raise ValueError(f"completed registration cannot be revived: {session_id}")
        if merge_authorized and not existing.get("merge_authorized"):
            raise ValueError(f"merge authority escalation requires a new registration: {session_id}")
        if not checkpoint_on_compact and existing.get("checkpoint_on_compact"):
            raise ValueError(f"checkpoint policy conflict for authoritative registration: {session_id}")
        return state_path(session_id)
    registration = {
        "role": role,
        "active": True,
        "checkpoint_on_compact": checkpoint_on_compact,
        "merge_authorized": merge_authorized,
        "agents": {},
    }
    if coordinator_thread_id:
        registration["coordinator_thread_id"] = coordinator_thread_id
    return write_state(session_id, registration, expected_revision=None)


def continue_session(source_id: str, successor_id: str, *, supervised: bool = False) -> Path | None:
    source_id = _safe_id(source_id)
    successor_id = _safe_id(successor_id)
    if source_id == successor_id:
        raise ValueError("source and successor session IDs must differ")
    source = load_state(source_id)
    if source is None:
        return None
    if not source.get("active") or source.get("successor_id"):
        raise ValueError(f"source session is not an active continuation source: {source_id}")
    if load_state(successor_id) is not None:
        raise ValueError(f"successor session already exists: {successor_id}")
    source_revision = int(source.get("state_revision", 0))
    source_next = dict(source)
    source_next["successor_id"] = successor_id
    source_next["active"] = False
    source_next["checkpoint_on_compact"] = False
    successor = dict(source_next)
    successor.pop("successor_id", None)
    successor.pop("pressure", None)
    successor["source_id"] = source_id
    successor["agents"] = {}
    successor["active"] = True
    successor["checkpoint_on_compact"] = True
    successor["events"] = 0
    successor["last_heartbeat"] = int(time.time())
    successor["restarts"] = source.get("restarts", 0) + (1 if supervised else 0)
    journal_path = continuation_journal_path()
    _write_json_atomic(journal_path, {
        "kind": "continuation-v1",
        "source_id": source_id,
        "successor_id": successor_id,
        "source_next": source_next,
        "successor": successor,
    })
    source_stage: Path | None = None
    successor_stage: Path | None = None
    try:
        source_path_value, source_stage = _stage_state(source_id, source_next, source_revision)
        successor_path_value, successor_stage = _stage_state(successor_id, successor, None)
        successor_stage.replace(successor_path_value)
        if os.environ.get("CODEX_ORCHESTRATION_TEST_CRASH_AFTER") == "successor":
            os._exit(91)
        try:
            source_stage.replace(source_path_value)
        except Exception:
            successor_path_value.unlink(missing_ok=True)
            raise
    except Exception:
        journal_path.unlink(missing_ok=True)
        raise
    finally:
        if source_stage is not None:
            source_stage.unlink(missing_ok=True)
        if successor_stage is not None:
            successor_stage.unlink(missing_ok=True)
    journal_path.unlink(missing_ok=True)
    os.chmod(source_path_value, stat.S_IRUSR | stat.S_IWUSR)
    os.chmod(successor_path_value, stat.S_IRUSR | stat.S_IWUSR)
    return successor_path_value


def _deep_merge(target: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value
    return target


def _dict_container(state: dict[str, Any], key: str) -> dict[str, Any]:
    value = state.get(key)
    if value is None:
        value = {}
        state[key] = value
    if not isinstance(value, dict):
        raise ValueError(f"invalid {key} container in orchestration state")
    return value


def _proof_lifecycle(lane_state: dict[str, Any]) -> dict[str, Any]:
    """Return the coordinator-owned proof lifecycle, initializing legacy lanes safely."""
    lifecycle = lane_state.get("proof_lifecycle")
    legacy_observations = {
        "contract": lane_state.get("research_content_ref"),
        "base": lane_state.get("base_sha"),
        "head": lane_state.get("head_sha"),
    }
    if lifecycle is None:
        observations = dict(legacy_observations)
        for domain, observation in observations.items():
            if observation is None:
                continue
            _validate_invalidation_observation(domain, observation)
        lifecycle = {
            "generations": {domain: 0 for domain in PROOF_DOMAINS},
            "observations": observations,
            "bindings": {},
            "invalidation_history": [],
        }
        lane_state["proof_lifecycle"] = lifecycle
    if not isinstance(lifecycle, dict):
        raise ValueError("invalid proof lifecycle in orchestration state")
    generations = lifecycle.get("generations")
    observations = lifecycle.get("observations")
    bindings = lifecycle.get("bindings")
    history = lifecycle.get("invalidation_history")
    if (
        not isinstance(generations, dict)
        or set(generations) != set(PROOF_DOMAINS)
        or any(
            type(generations[domain]) is not int or generations[domain] < 0
            for domain in PROOF_DOMAINS
        )
        or not isinstance(observations, dict)
        or set(observations) != set(PROOF_DOMAINS)
        or not isinstance(bindings, dict)
        or not isinstance(history, list)
    ):
        raise ValueError("invalid proof lifecycle in orchestration state")
    for domain in PROOF_DOMAINS:
        observation = observations.get(domain)
        legacy_observation = legacy_observations[domain]
        if observation is None and legacy_observation is not None:
            _validate_invalidation_observation(domain, legacy_observation)
            observations[domain] = legacy_observation
        elif observation is not None:
            _validate_invalidation_observation(domain, observation)
    return lifecycle


def _generation_tuple(lifecycle: dict[str, Any]) -> dict[str, int]:
    return {domain: lifecycle["generations"][domain] for domain in PROOF_DOMAINS}


def _binding_is_current(lifecycle: dict[str, Any], name: str) -> bool:
    binding = lifecycle["bindings"].get(name)
    return (
        isinstance(binding, dict)
        and binding.get("generations") == _generation_tuple(lifecycle)
    )


def _binding_evidence(lifecycle: dict[str, Any], name: str) -> dict[str, Any] | None:
    if not _binding_is_current(lifecycle, name):
        return None
    binding = lifecycle["bindings"].get(name)
    evidence = binding.get("evidence") if isinstance(binding, dict) else None
    return evidence if isinstance(evidence, dict) else None


def _accepted_merge_results(
    ledger: dict[str, Any], expected_pr_urls: list[str]
) -> dict[str, str]:
    """Resolve every expected PR to its unique current accepted Jiminy result."""
    accepted: dict[str, str] = {}
    for pr_url in expected_pr_urls:
        matches: list[str] = []
        for lane_state in ledger.values():
            if (
                not isinstance(lane_state, dict)
                or lane_state.get("tombstone")
                or lane_state.get("pr") != pr_url
                or not isinstance(lane_state.get("proof_lifecycle"), dict)
            ):
                continue
            lifecycle = _proof_lifecycle(lane_state)
            ready = _binding_evidence(lifecycle, "expected_merge_set")
            result = _binding_evidence(lifecycle, "merge_result")
            if (
                ready is None
                or ready.get("expected_pr_urls") != expected_pr_urls
                or result is None
                or result.get("pr_url") != pr_url
                or result.get("reviewed_head_sha") != lifecycle["observations"].get("head")
            ):
                continue
            merge_commit_sha = result.get("merge_commit_sha")
            if isinstance(merge_commit_sha, str) and FULL_SHA_PATTERN.fullmatch(
                merge_commit_sha
            ):
                matches.append(merge_commit_sha)
        if len(matches) != 1:
            raise ValueError(
                f"expected one current accepted merge result for {pr_url}, "
                f"found {len(matches)}"
            )
        accepted[pr_url] = matches[0]
    return accepted


def _bind_current_proof(
    lifecycle: dict[str, Any], name: str, evidence: dict[str, Any]
) -> None:
    lifecycle["bindings"][name] = {
        "generations": _generation_tuple(lifecycle),
        "evidence": evidence,
    }


def _validate_invalidation_observation(domain: str, observation: Any) -> str:
    if not isinstance(observation, str):
        raise ValueError(f"{domain} invalidation observation must be a string")
    pattern = CONTENT_REF_PATTERN if domain == "contract" else FULL_SHA_PATTERN
    if not pattern.fullmatch(observation):
        expected = (
            "sha256 content reference" if domain == "contract" else "full lowercase SHA"
        )
        raise ValueError(f"{domain} invalidation observation must be a {expected}")
    return observation


def _apply_invalidation(
    lane_state: dict[str, Any],
    event: str,
    context: dict[str, Any],
    *,
    transition_id: str,
    source_node: str,
    target_node: str,
    runner_session_id: str | None,
) -> bool:
    """Advance one proof generation. Return False for an exact idempotent replay."""
    if set(context) != {"observation", "reason"}:
        raise ValueError(
            f"{event} context must contain exactly observation and reason"
        )
    reason = context.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("invalidation reason must be a non-empty string")
    domain = INVALIDATION_EVENTS[event]
    observation = _validate_invalidation_observation(domain, context.get("observation"))
    lifecycle = _proof_lifecycle(lane_state)
    history = lifecycle["invalidation_history"]
    fingerprint = {"event": event, "observation": observation, "reason": reason}
    old_observation = lifecycle["observations"].get(domain)
    if old_observation == observation:
        if any(
            isinstance(entry, dict)
            and all(entry.get(key) == value for key, value in fingerprint.items())
            for entry in history
        ):
            return False
        raise ValueError(f"{event} requires a genuinely new {domain} observation")
    lifecycle["generations"][domain] += 1
    lifecycle["observations"][domain] = observation
    history.append({
        **fingerprint,
        "domain": domain,
        "transition_id": transition_id,
        "source_node": source_node,
        "target_node": target_node,
        "runner_session_id": runner_session_id,
        "old_observation": old_observation,
        "new_observation": observation,
        "generations": _generation_tuple(lifecycle),
        "timestamp": int(time.time()),
    })

    clear = {
        "contract": {
            "research", "implementation", "implementation_acceptance", "review", "ci",
            "review_attempt", "expected_merge_set", "merge_result", "merge_ready",
            "integration",
        },
        "base": {
            "implementation", "implementation_acceptance", "review", "ci",
            "review_attempt", "expected_merge_set", "merge_result", "merge_ready",
            "integration",
        },
        "head": {
            "implementation", "implementation_acceptance", "review", "ci",
            "review_attempt", "expected_merge_set", "merge_result", "merge_ready",
            "integration",
        },
    }[domain]
    for name in clear:
        lifecycle["bindings"].pop(name, None)
    return True


def _recorded_invalidation_replay(
    lane_state: dict[str, Any], event: str, context: dict[str, Any], current_node: str
) -> dict[str, Any] | None:
    """Return a current exact retry recorded as producing the current node."""
    if set(context) != {"observation", "reason"}:
        raise ValueError(
            f"{event} context must contain exactly observation and reason"
        )
    reason = context.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("invalidation reason must be a non-empty string")
    domain = INVALIDATION_EVENTS[event]
    observation = _validate_invalidation_observation(domain, context.get("observation"))
    lifecycle = _proof_lifecycle(lane_state)
    if lifecycle["observations"].get(domain) != observation:
        return None
    current_generations = _generation_tuple(lifecycle)
    for entry in reversed(lifecycle["invalidation_history"]):
        if (
            isinstance(entry, dict)
            and entry.get("event") == event
            and entry.get("observation") == observation
            and entry.get("reason") == reason
            and entry.get("target_node") == current_node
            and entry.get("generations") == current_generations
            and isinstance(entry.get("source_node"), str)
            and isinstance(entry.get("transition_id"), str)
        ):
            return entry
    return None


def _reject_stale_packet_replay(
    lane_state: dict[str, Any],
    event: str,
    packet_digest: str,
    fresh_review_attempt: bool,
) -> None:
    current = _generation_tuple(_proof_lifecycle(lane_state))
    receipts = lane_state.get("acceptance_receipts", [])
    if not isinstance(receipts, list):
        raise ValueError("invalid acceptance receipt container")
    if any(
        isinstance(receipt, dict)
        and receipt.get("event") == event
        and receipt.get("packet_digest") == packet_digest
        and not (event == "REVIEW_PACKET" and fresh_review_attempt)
        and receipt.get("generations") != current
        for receipt in receipts
    ):
        raise ValueError(f"stale {event} proof cannot be rebound to current generations")


def _require_current_proof(lifecycle: dict[str, Any], event: str) -> None:
    # Pre-F4 lanes may already be in flight without lifecycle bindings. Preserve
    # that compatibility until the lane records proof or its first invalidation.
    if (
        event != "JIMINY_PR_RESULT"
        and not lifecycle["bindings"]
        and not lifecycle["invalidation_history"]
    ):
        return
    required = {
        "JIMINY_READY": ("review", "ci", "merge_ready"),
        "JIMINY_PR_RESULT": ("review", "ci", "merge_ready", "expected_merge_set"),
        "JIMINY_COMPLETE": ("merge_ready",),
    }.get(event, ())
    missing = [name for name in required if not _binding_is_current(lifecycle, name)]
    if missing:
        raise ValueError(
            f"{event} requires current-generation proof: {', '.join(missing)}"
        )


def ledger_set(session_id: str, lane: str, updates: dict[str, Any]) -> Path:
    state = load_state(session_id)
    if state is None:
        raise ValueError(f"no registered session: {session_id}")
    ledger = _dict_container(state, "ledger")
    lane_state = ledger.setdefault(_safe_id(lane), {})
    if not isinstance(lane_state, dict):
        raise ValueError(f"invalid ledger lane container: {lane}")
    protected = sorted(set(updates) & PROTECTED_CONTEXT_KEYS)
    if protected:
        raise ValueError(f"trusted lane state cannot be set directly: {protected}")
    _deep_merge(lane_state, updates)
    return write_state(session_id, state, expected_revision=int(state.get("state_revision", 0)))


def ledger_move(session_id: str, from_lane: str, to_lane: str) -> Path:
    state = load_state(session_id)
    if state is None:
        raise ValueError(f"no registered session: {session_id}")
    from_lane = _safe_id(from_lane)
    to_lane = _safe_id(to_lane)
    if from_lane == to_lane:
        raise ValueError("ledger source and successor lanes must differ")
    ledger = _dict_container(state, "ledger")
    source = ledger.get(from_lane)
    if not isinstance(source, dict) or source.get("tombstone"):
        raise ValueError(f"no active ledger lane: {from_lane}")
    if to_lane in ledger:
        raise ValueError(f"successor ledger lane already exists: {to_lane}")
    successor = dict(source)
    successor["continued_from"] = from_lane
    ledger[to_lane] = successor
    ledger[from_lane] = {"tombstone": True, "successor_lane": to_lane}
    return write_state(session_id, state, expected_revision=int(state.get("state_revision", 0)))


def _is_continuation_successor(source_id: str, successor_id: str) -> bool:
    candidate = _safe_id(source_id)
    successor_id = _safe_id(successor_id)
    visited: set[str] = set()
    while candidate not in visited:
        visited.add(candidate)
        source = load_state(candidate)
        next_id = source.get("successor_id") if source else None
        if not isinstance(next_id, str):
            return False
        next_state = load_state(next_id)
        if not next_state or next_state.get("source_id") != candidate:
            return False
        if next_id == successor_id:
            return True
        candidate = next_id
    return False


def bind_review_attempt(
    session_id: str,
    lane: str,
    expected_revision: int,
    actor_session_id: str,
) -> dict[str, Any]:
    """Persist one authenticated review attempt for the current proof generation."""
    state = load_state(session_id)
    if state is None:
        raise ValueError(f"no registered session: {session_id}")
    if state.get("role") != "gepetto" or not state.get("active"):
        raise ValueError(
            f"review-attempt binding requires an active Gepetto coordinator: {session_id}"
        )
    observed_revision = int(state.get("state_revision", 0))
    if observed_revision != expected_revision:
        raise ValueError(
            f"state revision conflict for {session_id}: expected {expected_revision}, "
            f"observed {observed_revision}"
        )
    ledger = _dict_container(state, "ledger")
    lane = _safe_id(lane)
    actor_session_id = _safe_id(actor_session_id)
    lane_state = ledger.get(lane)
    if not isinstance(lane_state, dict) or lane_state.get("tombstone"):
        raise ValueError(f"no active ledger lane: {lane}")
    if lane_state.get("node") != "review":
        raise ValueError(f"review attempt can bind only at review: {lane}")
    if actor_session_id != lane:
        raise ValueError(f"actor {actor_session_id} does not own ledger lane {lane}")
    verify_registration(
        actor_session_id,
        "review",
        coordinator_thread_id=session_id,
    )

    lifecycle = _proof_lifecycle(lane_state)
    existing = _binding_evidence(lifecycle, "review_attempt")
    if (
        existing is not None
        and existing.get("actor_session_id") == actor_session_id
        and existing.get("consumed") is False
        and isinstance(existing.get("attempt_id"), str)
    ):
        return {
            "lane": lane,
            "state": lane_state,
            "review_attempt_id": existing["attempt_id"],
            "idempotent": True,
        }

    attempt_payload = json.dumps({
        "actor_session_id": actor_session_id,
        "coordinator_session_id": session_id,
        "generations": _generation_tuple(lifecycle),
        "lane": lane,
        "state_revision": observed_revision,
    }, sort_keys=True, separators=(",", ":")).encode()
    attempt_id = "sha256:" + hashlib.sha256(
        b"codex-review-attempt-v1\0" + attempt_payload
    ).hexdigest()
    _bind_current_proof(lifecycle, "review_attempt", {
        "actor_session_id": actor_session_id,
        "attempt_id": attempt_id,
        "consumed": False,
        "created_at": int(time.time()),
    })
    write_state(session_id, state, expected_revision=observed_revision)
    return {
        "lane": lane,
        "state": lane_state,
        "review_attempt_id": attempt_id,
        "idempotent": False,
    }


def bind_jiminy_ready(
    session_id: str,
    lane: str,
    expected_revision: int,
    packet: dict[str, Any],
    runner_session_id: str,
) -> dict[str, Any]:
    """Persist an authorized, generation-bound expected merge set."""
    from orchestration_packets import canonical_packet_digest, validate_packet

    validate_packet("JIMINY_READY", packet)
    state = load_state(session_id)
    if state is None:
        raise ValueError(f"no registered session: {session_id}")
    if state.get("role") != "gepetto" or not state.get("active"):
        raise ValueError(f"ready binding requires an active Gepetto coordinator: {session_id}")
    observed_revision = int(state.get("state_revision", 0))
    if observed_revision != expected_revision:
        raise ValueError(
            f"state revision conflict for {session_id}: expected {expected_revision}, "
            f"observed {observed_revision}"
        )
    if packet["coordinator_thread_id"] != session_id:
        raise ValueError("JIMINY_READY coordinator does not match the persisted coordinator")

    ledger = _dict_container(state, "ledger")
    lane = _safe_id(lane)
    lane_state = ledger.get(lane)
    if not isinstance(lane_state, dict) or lane_state.get("tombstone"):
        raise ValueError(f"no active ledger lane: {lane}")
    if lane_state.get("node") != "merge":
        raise ValueError(f"JIMINY_READY can bind only at merge: {lane}")

    runner_session_id = _safe_id(runner_session_id)
    verify_registration(runner_session_id, "jiminy", coordinator_thread_id=session_id)
    bound_runner = lane_state.get("jiminy_runner_session_id")
    if not isinstance(bound_runner, str):
        raise ValueError(f"Jiminy-owned lane is not bound to a runner: {lane}")
    if (
        runner_session_id != bound_runner
        and not _is_continuation_successor(bound_runner, runner_session_id)
    ):
        raise ValueError(
            f"lane is bound to Jiminy runner {bound_runner}; "
            f"{runner_session_id} is not its checkpoint successor"
        )

    lifecycle = _proof_lifecycle(lane_state)
    legacy_in_flight = not lifecycle["bindings"] and not lifecycle["invalidation_history"]
    _require_current_proof(lifecycle, "JIMINY_READY")
    pr_url = lane_state.get("pr")
    if not isinstance(pr_url, str):
        raise ValueError(f"ledger lane has no persisted PR URL: {lane}")
    ready_entry = next(
        (item for item in packet["pull_requests"] if item["pr_url"] == pr_url),
        None,
    )
    if ready_entry is None:
        raise ValueError(f"JIMINY_READY does not contain the lane PR: {pr_url}")
    trusted_head = lifecycle["observations"].get("head")
    if ready_entry["reviewed_head_sha"] != trusted_head:
        raise ValueError("JIMINY_READY reviewed head does not match current proof")
    if any(
        item["gates"]["review_packet_verified"] is not True
        or item["gates"]["required_checks_green"] is not True
        or item["gates"]["approvals_satisfied"] is not True
        or item["gates"]["mergeable"] is not True
        or type(item["gates"]["unresolved_required_threads"]) is not int
        or item["gates"]["unresolved_required_threads"] != 0
        for item in packet["pull_requests"]
    ):
        raise ValueError("JIMINY_READY requires every merge gate to be satisfied")

    packet_digest = canonical_packet_digest(packet)
    existing = lifecycle["bindings"].get("expected_merge_set")
    existing_evidence = existing.get("evidence") if isinstance(existing, dict) else None
    if (
        _binding_is_current(lifecycle, "expected_merge_set")
        and isinstance(existing_evidence, dict)
        and existing_evidence.get("packet_digest") == packet_digest
    ):
        return {"lane": lane, "state": lane_state, "idempotent": True}
    if _binding_is_current(lifecycle, "merge_result"):
        raise ValueError("cannot replace JIMINY_READY after accepting merge results")

    lane_state["jiminy_runner_session_id"] = runner_session_id
    if legacy_in_flight:
        _bind_current_proof(lifecycle, "review", {
            "migration_source": "JIMINY_READY",
            "reviewed_head_sha": ready_entry["reviewed_head_sha"],
        })
        _bind_current_proof(lifecycle, "ci", {
            "migration_source": "JIMINY_READY",
            "required_checks_green": True,
            "reviewed_head_sha": ready_entry["reviewed_head_sha"],
        })
        _bind_current_proof(lifecycle, "merge_ready", {
            "migration_source": "JIMINY_READY",
            "reviewed_head_sha": ready_entry["reviewed_head_sha"],
        })
    _bind_current_proof(lifecycle, "expected_merge_set", {
        "packet_digest": packet_digest,
        "merge_authority": packet["merge_authority"],
        "expected_pr_urls": list(packet["expected_pr_urls"]),
        "merge_order": list(packet["merge_order"]),
        "reviewed_heads": {
            item["pr_url"]: item["reviewed_head_sha"]
            for item in packet["pull_requests"]
        },
    })
    write_state(session_id, state, expected_revision=observed_revision)
    return {"lane": lane, "state": lane_state, "idempotent": False}


def apply_graph_transition(
    session_id: str,
    lane: str,
    current_node: str,
    event: str,
    context: dict[str, Any],
    workflow: dict[str, Any],
    runner_session_id: str | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    from orchestration_graph import eligible_transitions, resolve_target
    from orchestration_packets import PACKET_TYPES

    if event in PACKET_TYPES:
        raise ValueError(
            f"packet event {event} must use graph accept, not administrative graph apply"
        )

    state = load_state(session_id)
    if state is None:
        raise ValueError(f"no registered session: {session_id}")
    observed_revision = int(state.get("state_revision", 0))
    if expected_revision is not None and observed_revision != expected_revision:
        raise ValueError(
            f"state revision conflict for {session_id}: expected {expected_revision}, "
            f"observed {observed_revision}"
        )
    ledger = _dict_container(state, "ledger")
    lane = _safe_id(lane)
    lane_state = ledger.get(lane)
    if not isinstance(lane_state, dict) or lane_state.get("tombstone"):
        raise ValueError(f"no active ledger lane: {lane}")
    if lane_state.get("node") != current_node:
        raise ValueError(
            f"ledger node mismatch for {lane}: expected {current_node}, observed {lane_state.get('node')}"
        )
    if event in INVALIDATION_EVENTS and expected_revision is None:
        raise ValueError(f"{event} requires --expected-revision")
    if event not in INVALIDATION_EVENTS and set(context) & PROTECTED_CONTEXT_KEYS:
        protected = sorted(set(context) & PROTECTED_CONTEXT_KEYS)
        raise ValueError(f"caller context cannot overwrite trusted state: {protected}")

    lifecycle = _proof_lifecycle(lane_state)
    recorded_replay = (
        _recorded_invalidation_replay(lane_state, event, context, current_node)
        if event in INVALIDATION_EVENTS
        else None
    )
    if recorded_replay is not None:
        transition = next(
            (
                candidate
                for candidate in workflow["transitions"]
                if candidate["id"] == recorded_replay["transition_id"]
                and candidate["event"] == event
                and recorded_replay["source_node"] in candidate["from"]
                and candidate.get("to") == current_node
            ),
            None,
        )
        if transition is None:
            raise ValueError("recorded invalidation retry no longer matches the workflow")
        source_owner = workflow["nodes"][recorded_replay["source_node"]].get("owner")
        target_owner = workflow["nodes"][current_node].get("owner")
        if "jiminy" in {source_owner, target_owner}:
            if not runner_session_id:
                raise ValueError("Jiminy runner session ID is required for this transition")
            verify_registration(
                runner_session_id,
                "jiminy",
                coordinator_thread_id=session_id,
            )
            recorded_runner = recorded_replay.get("runner_session_id")
            if not isinstance(recorded_runner, str) or (
                runner_session_id != recorded_runner
                and not _is_continuation_successor(recorded_runner, runner_session_id)
            ):
                raise ValueError(
                    f"invalidation was authorized by Jiminy runner {recorded_runner}; "
                    f"{runner_session_id} is not its checkpoint successor"
                )
            bound_runner = lane_state.get("jiminy_runner_session_id")
            if not isinstance(bound_runner, str) or (
                runner_session_id != bound_runner
                and not _is_continuation_successor(bound_runner, runner_session_id)
            ):
                raise ValueError(
                    f"lane is bound to Jiminy runner {bound_runner}; "
                    f"{runner_session_id} is not its checkpoint successor"
                )
        return {
            "transition_id": transition["id"],
            "lane": lane,
            "state": lane_state,
            "idempotent": True,
        }
    if event == "MERGES_VERIFIED":
        if set(context) != {"packet"}:
            raise ValueError("MERGES_VERIFIED context must contain exactly packet")
        if not _binding_is_current(lifecycle, "merge_result"):
            raise ValueError("MERGES_VERIFIED requires current-generation merge results")
        if not _binding_is_current(lifecycle, "expected_merge_set"):
            raise ValueError("MERGES_VERIFIED requires an authorized expected merge set")
        binding = lifecycle["bindings"]["expected_merge_set"]
        evidence = binding.get("evidence") if isinstance(binding, dict) else None
        expected = evidence.get("expected_pr_urls") if isinstance(evidence, dict) else None
        packet = context.get("packet")
        results = packet.get("merge_results") if isinstance(packet, dict) else None
        if (
            not isinstance(expected, list) or not expected
            or not all(isinstance(url, str) and url for url in expected)
            or not isinstance(results, dict)
        ):
            raise ValueError("MERGES_VERIFIED has invalid persisted merge authorization")
        accepted_results = _accepted_merge_results(ledger, expected)
        if results != accepted_results:
            raise ValueError(
                "MERGES_VERIFIED results must equal current accepted per-PR results"
            )

    evaluation_context = dict(lane_state)
    evaluation_context.update(context)
    if event == "MERGES_VERIFIED":
        evaluation_context["ready"] = {"expected_pr_urls": list(expected)}
    evaluation_context["persisted"] = dict(lane_state)
    evaluation_context["persisted"]["head_sha"] = lifecycle["observations"].get("head")
    matches = eligible_transitions(workflow, current_node, event, evaluation_context)
    if len(matches) != 1:
        raise ValueError(f"expected one eligible transition, found {len(matches)}")
    transition = matches[0]
    target_node = resolve_target(transition, evaluation_context, workflow["nodes"])
    source_owner = workflow["nodes"][current_node].get("owner")
    target_owner = workflow["nodes"][target_node].get("owner")
    if "jiminy" in {source_owner, target_owner}:
        if not runner_session_id:
            raise ValueError("Jiminy runner session ID is required for this transition")
        verify_registration(
            runner_session_id,
            "jiminy",
            coordinator_thread_id=session_id,
        )
        bound_runner = lane_state.get("jiminy_runner_session_id")
        if bound_runner is not None and (
            not isinstance(bound_runner, str)
            or (
                runner_session_id != bound_runner
                and not _is_continuation_successor(bound_runner, runner_session_id)
            )
        ):
            raise ValueError(
                f"lane is bound to Jiminy runner {bound_runner}; "
                f"{runner_session_id} is not its checkpoint successor"
            )
    if event in INVALIDATION_EVENTS:
        if not _apply_invalidation(
            lane_state,
            event,
            context,
            transition_id=transition["id"],
            source_node=current_node,
            target_node=target_node,
            runner_session_id=runner_session_id,
        ):
            return {
                "transition_id": transition["id"],
                "lane": lane,
                "state": lane_state,
                "idempotent": True,
            }
    if "jiminy" in {source_owner, target_owner}:
        lane_state["jiminy_runner_session_id"] = runner_session_id
    for key, value in transition.get("set", {}).items():
        lane_state[key] = value
    increment = transition.get("increment")
    if increment:
        path = increment.get("path")
        amount = increment.get("by")
        if not isinstance(path, str) or "." in path or not isinstance(amount, int):
            raise ValueError(f"unsupported transition increment: {increment}")
        current = lane_state.get(path, 0)
        if type(current) is not int:
            raise ValueError(f"transition counter is not an integer: {path}")
        lane_state[path] = current + amount
    if workflow["nodes"][target_node].get("resumable") is True:
        lane_state["resume_node"] = current_node
    elif event == "RESUME_AUTHORIZED":
        lane_state.pop("resume_node", None)
    if event == "MERGES_VERIFIED":
        _bind_current_proof(lifecycle, "merge_ready", {
            "expected_pr_urls": list(expected),
            "merge_results": dict(context["packet"]["merge_results"]),
        })
    lane_state["node"] = target_node
    write_state(session_id, state, expected_revision=observed_revision)
    return {"transition_id": transition["id"], "lane": lane, "state": lane_state}


def accept_graph_event(
    session_id: str,
    lane: str,
    actor_session_id: str,
    expected_revision: int,
    event: str,
    packet: dict[str, Any],
    observed_pr_head_sha: str | None,
    runner_session_id: str | None = None,
    review_attempt_id: str | None = None,
) -> dict[str, Any]:
    """Validate and persist one packet-driven transition as one coordinator revision."""
    from orchestration_graph import eligible_transitions, load_workflow, resolve_target
    from orchestration_packets import (
        FULL_SHA,
        PACKET_TYPES,
        canonical_packet_digest,
        validate_packet,
    )

    workflow = load_workflow()
    if event not in PACKET_TYPES:
        raise ValueError(f"graph accept requires a supported packet event: {event}")
    validate_packet(event, packet)
    if observed_pr_head_sha is not None and not FULL_SHA.fullmatch(observed_pr_head_sha):
        raise ValueError("observed PR head must be a full 40-character SHA")

    state = load_state(session_id)
    if state is None:
        raise ValueError(f"no registered session: {session_id}")
    if state.get("role") != "gepetto" or not state.get("active"):
        raise ValueError(f"event acceptance requires an active Gepetto coordinator: {session_id}")
    observed_revision = int(state.get("state_revision", 0))
    if observed_revision != expected_revision:
        raise ValueError(
            f"state revision conflict for {session_id}: expected {expected_revision}, "
            f"observed {observed_revision}"
        )

    ledger = _dict_container(state, "ledger")
    lane = _safe_id(lane)
    actor_session_id = _safe_id(actor_session_id)
    lane_state = ledger.get(lane)
    if not isinstance(lane_state, dict) or lane_state.get("tombstone"):
        raise ValueError(f"no active ledger lane: {lane}")
    current_node = lane_state.get("node")
    if not isinstance(current_node, str) or current_node not in workflow["nodes"]:
        raise ValueError(f"ledger lane has no valid workflow node: {lane}")
    lifecycle = _proof_lifecycle(lane_state)
    packet_digest = canonical_packet_digest(packet)
    candidates = [
        transition
        for transition in workflow["transitions"]
        if current_node in transition["from"] and transition["event"] == event
    ]
    if not candidates or any(transition.get("packet_type") != event for transition in candidates):
        raise ValueError(f"no packet-driven transition for {event} from {current_node}")
    head_bound = any(
        condition.get("equals_path") == "live.pr_head_sha"
        for transition in candidates
        for condition in transition.get("all", [])
    )
    if head_bound and observed_pr_head_sha is None:
        raise ValueError(f"event {event} requires an observed PR head")

    expected_role = workflow["nodes"][current_node].get("lane_role")
    if expected_role not in ROLES:
        raise ValueError(f"packet event {event} has no supported actor role at {current_node}")
    artifact = packet.get("artifact")
    inline_research = (
        workflow.get("policies", {}).get("inline_research") == "single_leaf_keep"
        and event == "RESEARCH_PACKET"
        and current_node == "research"
        and expected_role == "research"
        and actor_session_id == session_id
        and lane == session_id
        and packet.get("decision") == "keep"
        and len(packet.get("delivery_issue_urls", [])) == 1
        and isinstance(artifact, dict)
        and artifact.get("kind") == "github_issue"
        and artifact.get("status") == "persisted"
    )
    terminal_actor = False
    if not inline_research:
        actor_state = load_state(actor_session_id)
        if actor_state is None:
            raise ValueError(f"no registered session: {actor_session_id}")
        if actor_state.get("active"):
            verify_registration(
                actor_session_id,
                expected_role,
                coordinator_thread_id=session_id,
            )
        else:
            if actor_state.get("role") != expected_role:
                raise ValueError(
                    f"role mismatch for {actor_session_id}: expected {expected_role}, "
                    f"registered {actor_state.get('role')}"
                )
            registered_coordinator = actor_state.get("coordinator_thread_id")
            candidate = registered_coordinator
            visited: set[str] = set()
            resolved_coordinator = None
            while isinstance(candidate, str) and candidate not in visited:
                visited.add(candidate)
                observed = load_state(candidate)
                if observed and observed.get("role") == "gepetto" and observed.get("active"):
                    resolved_coordinator = candidate
                    break
                candidate = observed.get("successor_id") if observed else None
            if resolved_coordinator != session_id:
                raise ValueError(
                    f"coordinator mismatch for {actor_session_id}: expected {session_id}, "
                    f"registered {registered_coordinator}"
                )
            if actor_state.get("successor_id"):
                raise ValueError(
                    f"checkpoint predecessor cannot accept a packet: {actor_session_id}"
                )
            if actor_state.get("forced_stop_without_receipt"):
                raise ValueError(f"forced stop cannot authorize a packet: {actor_session_id}")
            if (
                actor_state.get("terminal_packet_type") != event
                or actor_state.get("terminal_packet_digest") != canonical_packet_digest(packet)
            ):
                raise ValueError(
                    f"inactive actor has no matching terminal receipt: {actor_session_id}"
                )
            terminal_actor = True
    if expected_role != "jiminy" and actor_session_id != lane:
        raise ValueError(f"actor {actor_session_id} does not own ledger lane {lane}")
    if runner_session_id is not None:
        runner_session_id = _safe_id(runner_session_id)
    if expected_role == "jiminy" and runner_session_id not in {None, actor_session_id}:
        raise ValueError("a Jiminy packet actor must also be the selected Jiminy runner")
    review_attempt_evidence: dict[str, Any] | None = None
    if event == "REVIEW_PACKET" and (
        lifecycle["invalidation_history"] or review_attempt_id is not None
    ):
        if (
            not isinstance(review_attempt_id, str)
            or not REVIEW_ATTEMPT_PATTERN.fullmatch(review_attempt_id)
        ):
            raise ValueError(
                "REVIEW_PACKET after invalidation requires a current review attempt"
            )
        review_attempt_evidence = _binding_evidence(lifecycle, "review_attempt")
        if (
            review_attempt_evidence is None
            or review_attempt_evidence.get("attempt_id") != review_attempt_id
            or review_attempt_evidence.get("actor_session_id") != actor_session_id
            or review_attempt_evidence.get("consumed") is not False
        ):
            raise ValueError(
                "REVIEW_PACKET review attempt is stale, altered, consumed, or unauthorized"
            )
    elif review_attempt_id is not None:
        raise ValueError("review attempt ID is valid only for REVIEW_PACKET")
    _reject_stale_packet_replay(
        lane_state,
        event,
        packet_digest,
        fresh_review_attempt=review_attempt_evidence is not None,
    )

    evaluation_context = dict(lane_state)
    evaluation_context["packet"] = packet
    evaluation_context["persisted"] = dict(lane_state)
    evaluation_context["persisted"]["head_sha"] = lifecycle["observations"].get("head")
    evaluation_context["live"] = (
        {"pr_head_sha": observed_pr_head_sha} if observed_pr_head_sha is not None else {}
    )
    matches = eligible_transitions(workflow, current_node, event, evaluation_context)
    if len(matches) != 1:
        raise ValueError(f"expected one eligible transition, found {len(matches)}")
    transition = matches[0]
    target_node = resolve_target(transition, evaluation_context, workflow["nodes"])

    if event == "RESEARCH_PACKET":
        contract_observation = packet["artifact"]["content_ref"]
        current_contract = lifecycle["observations"].get("contract")
        if current_contract not in {None, contract_observation}:
            raise ValueError(
                "changed research contract requires MATERIAL_CONTRACT_CHANGED"
            )
        lifecycle["observations"]["contract"] = contract_observation
    elif event == "IMPLEMENTATION_PACKET":
        lifecycle["observations"]["head"] = packet["pr_head_sha"]
    elif event == "REVIEW_PACKET":
        trusted_head = lifecycle["observations"].get("head")
        if trusted_head not in {None, packet["reviewed_head_sha"]}:
            raise ValueError("changed PR head requires PR_HEAD_CHANGED")
        lifecycle["observations"]["head"] = packet["reviewed_head_sha"]

    source_owner = workflow["nodes"][current_node].get("owner")
    target_owner = workflow["nodes"][target_node].get("owner")
    selected_runner = runner_session_id
    if "jiminy" in {source_owner, target_owner}:
        selected_runner = selected_runner or (
            actor_session_id if expected_role == "jiminy" else None
        )
        if selected_runner is None:
            raise ValueError("Jiminy runner session ID is required for this transition")
        if not (
            terminal_actor
            and expected_role == "jiminy"
            and selected_runner == actor_session_id
        ):
            verify_registration(selected_runner, "jiminy", coordinator_thread_id=session_id)
        bound_runner = lane_state.get("jiminy_runner_session_id")
        if source_owner == "jiminy" and bound_runner is None:
            raise ValueError(f"Jiminy-owned lane is not bound to a runner: {lane}")
        if bound_runner is not None and (
            not isinstance(bound_runner, str)
            or (
                selected_runner != bound_runner
                and not _is_continuation_successor(bound_runner, selected_runner)
            )
        ):
            raise ValueError(
                f"lane is bound to Jiminy runner {bound_runner}; "
                f"{selected_runner} is not its checkpoint successor"
            )
        lane_state["jiminy_runner_session_id"] = selected_runner
    _require_current_proof(lifecycle, event)
    if event == "JIMINY_PR_RESULT":
        expected_binding = lifecycle["bindings"].get("expected_merge_set")
        expected_evidence = (
            expected_binding.get("evidence")
            if isinstance(expected_binding, dict) else None
        )
        expected_urls = (
            expected_evidence.get("expected_pr_urls")
            if isinstance(expected_evidence, dict) else None
        )
        if (
            not isinstance(expected_evidence, dict)
            or expected_evidence.get("merge_authority") != "merge"
        ):
            raise ValueError("JIMINY_PR_RESULT requires persisted merge authority")
        if not isinstance(expected_urls, list) or packet["pr_url"] not in expected_urls:
            raise ValueError("JIMINY_PR_RESULT is outside the authorized expected merge set")
        if lane_state.get("pr") != packet["pr_url"]:
            raise ValueError("JIMINY_PR_RESULT does not match the persisted lane PR")
    for key, value in transition.get("set", {}).items():
        lane_state[key] = value
    increment = transition.get("increment")
    if increment:
        path = increment.get("path")
        amount = increment.get("by")
        if not isinstance(path, str) or "." in path or not isinstance(amount, int):
            raise ValueError(f"unsupported transition increment: {increment}")
        current = lane_state.get(path, 0)
        if type(current) is not int:
            raise ValueError(f"transition counter is not an integer: {path}")
        lane_state[path] = current + amount
    lane_state["node"] = target_node

    if event == "RESEARCH_PACKET":
        _bind_current_proof(lifecycle, "research", {
            "packet_digest": packet_digest,
            "content_ref": packet["artifact"]["content_ref"],
        })
    elif event == "IMPLEMENTATION_PACKET":
        implementation_evidence = {
            "packet_digest": packet_digest,
            "pr_head_sha": packet["pr_head_sha"],
        }
        _bind_current_proof(lifecycle, "implementation", implementation_evidence)
        _bind_current_proof(lifecycle, "implementation_acceptance", implementation_evidence)
    elif event == "REVIEW_PACKET":
        review_evidence = {
            "packet_digest": packet_digest,
            "reviewed_head_sha": packet["reviewed_head_sha"],
        }
        if review_attempt_evidence is not None:
            review_attempt_evidence["consumed"] = True
            review_attempt_evidence["packet_digest"] = packet_digest
            review_evidence["review_attempt_id"] = review_attempt_id
        _bind_current_proof(lifecycle, "review", review_evidence)
        _bind_current_proof(lifecycle, "ci", {
            "checks": packet["ci_checks"],
            "reviewed_head_sha": packet["reviewed_head_sha"],
        })
        _bind_current_proof(lifecycle, "merge_ready", {
            "ready_for_jiminy": packet["ready_for_jiminy"],
            "reviewed_head_sha": packet["reviewed_head_sha"],
        })
    elif event == "JIMINY_PR_RESULT":
        _bind_current_proof(lifecycle, "merge_result", {
            "packet_digest": packet_digest,
            "pr_url": packet["pr_url"],
            "reviewed_head_sha": packet["reviewed_head_sha"],
            "merge_commit_sha": packet["merge_commit_sha"],
        })
    elif event == "JIMINY_COMPLETE":
        _bind_current_proof(lifecycle, "integration", {
            "packet_digest": packet_digest,
            "verified_default_head_sha": packet["verified_default_head_sha"],
        })

    receipts = lane_state.get("acceptance_receipts", [])
    if not isinstance(receipts, list):
        raise ValueError(f"invalid acceptance receipt container for lane: {lane}")
    receipt = {
        "packet_digest": packet_digest,
        "actor_session_id": actor_session_id,
        "timestamp": int(time.time()),
        "event": event,
        "transition_id": transition["id"],
        "resulting_node": target_node,
        "generations": _generation_tuple(lifecycle),
    }
    if head_bound:
        receipt["observed_pr_head_sha"] = observed_pr_head_sha
    if event == "REVIEW_PACKET" and review_attempt_id is not None:
        receipt["review_attempt_id"] = review_attempt_id
    lane_state["acceptance_receipts"] = [*receipts, receipt]

    write_state(session_id, state, expected_revision=expected_revision)
    return {
        "transition_id": transition["id"],
        "lane": lane,
        "state": lane_state,
        "acceptance_receipt": receipt,
    }


def context_bind(
    session_id: str,
    key: str,
    files: list[Path],
    source: str | None,
    *,
    content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    content = content or context_digest(files)
    state = load_state(session_id)
    if state is None:
        raise ValueError(f"no registered session: {session_id}")
    key = _safe_id(key)
    source = source or ",".join(content["files"])
    digest = content["digest"]
    context_refs = _dict_container(state, "context_refs")
    previous = context_refs.get(key)
    reload_required = not isinstance(previous, dict) or previous.get("digest") != digest
    reference = {
        "source": source,
        "digest": digest,
        "ref": f"sha256:{digest}",
        "digest_version": content["digest_version"],
        "arity": content["arity"],
    }
    context_refs[key] = reference
    write_state(session_id, state, expected_revision=int(state.get("state_revision", 0)))
    return {"key": key, **reference, "reload_required": reload_required}


def context_digest(files: list[Path]) -> dict[str, Any]:
    if not files:
        raise ValueError("at least one context file is required")
    resolved = sorted(path.expanduser().resolve() for path in files)
    contents = [path.read_bytes() for path in resolved]
    hasher = hashlib.sha256()
    hasher.update(b"codex-orchestration-context\x00v1\x00")
    hasher.update(len(contents).to_bytes(8, "big"))
    for content in contents:
        hasher.update(len(content).to_bytes(8, "big"))
        hasher.update(content)
    digest = hasher.hexdigest()
    return {
        "digest": digest,
        "ref": f"sha256:{digest}",
        "digest_version": 1,
        "arity": len(contents),
        "files": [str(path) for path in resolved],
    }


def verify_registration(
    session_id: str,
    role: str,
    coordinator_thread_id: str | None = None,
) -> dict[str, Any]:
    state = load_state(session_id)
    if state is None:
        raise ValueError(f"no registered session: {session_id}")
    if state.get("role") != role:
        raise ValueError(
            f"role mismatch for {session_id}: expected {role}, registered {state.get('role')}"
        )
    resolved_coordinator = None
    if role != "gepetto":
        registered_coordinator = state.get("coordinator_thread_id")
        if not isinstance(registered_coordinator, str):
            raise ValueError(f"child registration has no coordinator: {session_id}")
        candidate = registered_coordinator
        visited: set[str] = set()
        while isinstance(candidate, str) and candidate not in visited:
            visited.add(candidate)
            observed = load_state(candidate)
            if observed and observed.get("role") == "gepetto" and observed.get("active"):
                resolved_coordinator = candidate
                break
            candidate = observed.get("successor_id") if observed else None
        if resolved_coordinator is None:
            raise ValueError(
                f"verification requires an active Gepetto coordinator: {registered_coordinator}"
            )
        if coordinator_thread_id is not None:
            coordinator_thread_id = _safe_id(coordinator_thread_id)
        if coordinator_thread_id is not None and resolved_coordinator != coordinator_thread_id:
            raise ValueError(
                f"coordinator mismatch for {session_id}: expected {coordinator_thread_id}, "
                f"registered {registered_coordinator}"
            )
    if not state.get("active"):
        raise ValueError(f"registered session is not active: {session_id}")
    return {
        "session_id": session_id,
        "role": role,
        "coordinator_thread_id": resolved_coordinator,
        "active": True,
        "verified": True,
    }


def record_pressure(
    session_id: str,
    context_used_tokens: int,
    context_limit_tokens: int,
) -> dict[str, Any]:
    state = load_state(session_id)
    if state is None:
        raise ValueError(f"no registered session: {session_id}")
    if context_used_tokens < 0 or context_limit_tokens <= 0:
        raise ValueError("context token counts require used >= 0 and limit > 0")
    if context_used_tokens > context_limit_tokens:
        raise ValueError("context used tokens cannot exceed the context limit")
    pressure = {
        "context_used_tokens": context_used_tokens,
        "context_limit_tokens": context_limit_tokens,
        "context_ratio": context_used_tokens / context_limit_tokens,
        "state_bytes": 0,
        "observed_at": int(time.time()),
    }
    state["pressure"] = pressure
    _stabilize_pressure_size(session_id, state, pressure)
    write_state(session_id, state, expected_revision=int(state.get("state_revision", 0)))
    return pressure


def _stabilize_pressure_size(
    session_id: str, state: dict[str, Any], pressure: dict[str, Any]
) -> None:
    next_revision = int(state.get("state_revision", 0)) + 1
    for _ in range(8):
        candidate = dict(state)
        candidate["session_id"] = session_id
        candidate["state_revision"] = next_revision
        persisted_size = len(
            (json.dumps(candidate, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        )
        if pressure["state_bytes"] == persisted_size:
            return
        pressure["state_bytes"] = persisted_size
    raise ValueError("could not stabilize persisted pressure state size")


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
    continue_parser.add_argument("--supervised", action="store_true")

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--session-id", required=True)
    verify_parser.add_argument("--role", choices=sorted(ROLES), required=True)
    verify_parser.add_argument("--coordinator-thread-id")

    complete_parser = subparsers.add_parser("complete")
    complete_parser.add_argument("--session-id", required=True)

    ledger_parser = subparsers.add_parser("ledger")
    ledger_actions = ledger_parser.add_subparsers(dest="ledger_command", required=True)
    ledger_set_parser = ledger_actions.add_parser("set")
    ledger_set_parser.add_argument("--session-id", required=True)
    ledger_set_parser.add_argument("--lane", required=True)
    ledger_set_parser.add_argument("--json", required=True)
    ledger_show_parser = ledger_actions.add_parser("show")
    ledger_show_parser.add_argument("--session-id", required=True)
    ledger_move_parser = ledger_actions.add_parser("move")
    ledger_move_parser.add_argument("--session-id", required=True)
    ledger_move_parser.add_argument("--from-lane", required=True)
    ledger_move_parser.add_argument("--to-lane", required=True)

    context_parser = subparsers.add_parser("context")
    context_actions = context_parser.add_subparsers(dest="context_command", required=True)
    context_bind_parser = context_actions.add_parser("bind")
    context_bind_parser.add_argument("--session-id", required=True)
    context_bind_parser.add_argument("--key", required=True)
    context_bind_parser.add_argument("--source")
    context_bind_parser.add_argument("--file", action="append", type=Path, required=True)
    context_digest_parser = context_actions.add_parser("digest")
    context_digest_parser.add_argument("--file", action="append", type=Path, required=True)

    pressure_parser = subparsers.add_parser("pressure")
    pressure_actions = pressure_parser.add_subparsers(dest="pressure_command", required=True)
    pressure_record_parser = pressure_actions.add_parser("record")
    pressure_record_parser.add_argument("--session-id", required=True)
    pressure_record_parser.add_argument("--context-used-tokens", type=int, required=True)
    pressure_record_parser.add_argument("--context-limit-tokens", type=int, required=True)

    graph_parser = subparsers.add_parser("graph")
    graph_actions = graph_parser.add_subparsers(dest="graph_command", required=True)
    graph_apply_parser = graph_actions.add_parser("apply")
    graph_apply_parser.add_argument("--session-id", required=True)
    graph_apply_parser.add_argument("--lane", required=True)
    graph_apply_parser.add_argument("--current-node", required=True)
    graph_apply_parser.add_argument("--event", required=True)
    graph_apply_parser.add_argument("--expected-revision", type=int)
    graph_apply_parser.add_argument("--context-json", default="{}")
    graph_apply_parser.add_argument("--runner-session-id")
    graph_apply_parser.add_argument(
        "--workflow", type=Path,
        default=Path(__file__).parents[1] / "gepetto" / "references" / "workflow.json",
    )
    graph_ready_parser = graph_actions.add_parser("ready")
    graph_ready_parser.add_argument("--session-id", required=True)
    graph_ready_parser.add_argument("--lane", required=True)
    graph_ready_parser.add_argument("--expected-revision", type=int, required=True)
    graph_ready_parser.add_argument("--packet-json", required=True)
    graph_ready_parser.add_argument("--runner-session-id", required=True)
    graph_attempt_parser = graph_actions.add_parser("review-attempt")
    graph_attempt_parser.add_argument("--session-id", required=True)
    graph_attempt_parser.add_argument("--lane", required=True)
    graph_attempt_parser.add_argument("--actor-session-id", required=True)
    graph_attempt_parser.add_argument("--expected-revision", type=int, required=True)
    graph_accept_parser = graph_actions.add_parser("accept")
    graph_accept_parser.add_argument("--session-id", required=True)
    graph_accept_parser.add_argument("--lane", required=True)
    graph_accept_parser.add_argument("--actor-session-id", required=True)
    graph_accept_parser.add_argument("--expected-revision", type=int, required=True)
    graph_accept_parser.add_argument("--event", required=True)
    graph_accept_parser.add_argument("--packet-json", required=True)
    graph_accept_parser.add_argument("--observed-pr-head-sha")
    graph_accept_parser.add_argument("--runner-session-id")
    graph_accept_parser.add_argument("--review-attempt-id")

    subparsers.add_parser("status")
    return parser


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "verify":
        try:
            result = verify_registration(
                args.session_id,
                args.role,
                coordinator_thread_id=args.coordinator_thread_id,
            )
        except ValueError as error:
            print(f"orchestration_state: {error}", file=sys.stderr)
            return 1
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "pressure":
        try:
            result = record_pressure(
                args.session_id,
                args.context_used_tokens,
                args.context_limit_tokens,
            )
        except (OSError, ValueError) as error:
            print(f"orchestration_state: {error}", file=sys.stderr)
            return 1
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "ledger":
        if args.ledger_command == "set":
            try:
                updates = json.loads(args.json)
            except json.JSONDecodeError as error:
                print(f"orchestration_state: invalid --json: {error}", file=sys.stderr)
                return 1
            if not isinstance(updates, dict):
                print("orchestration_state: --json must be a JSON object", file=sys.stderr)
                return 1
            try:
                print(ledger_set(args.session_id, args.lane, updates))
            except ValueError as error:
                print(f"orchestration_state: {error}", file=sys.stderr)
                return 1
        elif args.ledger_command == "move":
            try:
                print(ledger_move(args.session_id, args.from_lane, args.to_lane))
            except ValueError as error:
                print(f"orchestration_state: {error}", file=sys.stderr)
                return 1
        else:
            state = load_state(args.session_id) or {}
            print(json.dumps(state.get("ledger", {}), sort_keys=True))
        return 0
    if args.command == "status":
        sessions = state_root() / "sessions"
        for path in sorted(sessions.glob("*.json")) if sessions.is_dir() else []:
            state = json.loads(path.read_text(encoding="utf-8"))
            coordinator = state.get("coordinator_thread_id") or "-"
            active = "true" if state.get("active") else "false"
            print(f"{state.get('session_id', path.stem)} role={state.get('role', '-')} active={active} coordinator={coordinator}")
        return 0
    if args.command == "register":
        try:
            path = register(
                args.session_id,
                args.role,
                checkpoint_on_compact=not args.no_checkpoint,
                merge_authorized=args.merge_authorized,
                coordinator_thread_id=args.coordinator_thread_id,
            )
        except ValueError as error:
            print(f"orchestration_state: {error}", file=sys.stderr)
            return 1
    elif args.command == "continue":
        try:
            path = continue_session(args.source_id, args.successor_id, supervised=args.supervised)
        except ValueError as error:
            print(f"orchestration_state: {error}", file=sys.stderr)
            return 1
    else:
        state = load_state(args.session_id)
        if state is None:
            path = None
        else:
            state["active"] = False
            state["checkpoint_on_compact"] = False
            path = write_state(
                args.session_id,
                state,
                expected_revision=int(state.get("state_revision", 0)),
            )
    if path:
        print(path)
    return 0


def main() -> int:
    args = _parser().parse_args()
    if args.command == "context":
        try:
            content = context_digest(args.file)
        except (OSError, ValueError) as error:
            print(f"orchestration_state: {error}", file=sys.stderr)
            return 1
        if args.context_command == "digest":
            print(json.dumps(content, sort_keys=True))
            return 0
        with registry_lock():
            recover_transactions()
            try:
                result = context_bind(
                    args.session_id, args.key, args.file, args.source, content=content
                )
            except (OSError, ValueError) as error:
                print(f"orchestration_state: {error}", file=sys.stderr)
                return 1
            print(json.dumps(result, sort_keys=True))
            return 0
    if args.command == "graph":
        from orchestration_graph import load_workflow
        try:
            if args.graph_command == "apply":
                workflow = load_workflow(args.workflow)
                context = json.loads(args.context_json)
                if not isinstance(context, dict):
                    raise ValueError("--context-json must be a JSON object")
            elif args.graph_command in {"ready", "accept"}:
                def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                    value: dict[str, Any] = {}
                    for key, item in pairs:
                        if key in value:
                            raise ValueError(f"--packet-json contains duplicate key: {key}")
                        value[key] = item
                    return value

                packet = json.loads(args.packet_json, object_pairs_hook=unique_object)
                if not isinstance(packet, dict):
                    raise ValueError("--packet-json must be a JSON object")
        except (json.JSONDecodeError, OSError, ValueError) as error:
            print(f"orchestration_state: {error}", file=sys.stderr)
            return 1
        with registry_lock():
            recover_transactions()
            try:
                if args.graph_command == "apply":
                    result = apply_graph_transition(
                        args.session_id, args.lane, args.current_node, args.event,
                        context, workflow, runner_session_id=args.runner_session_id,
                        expected_revision=args.expected_revision,
                    )
                elif args.graph_command == "ready":
                    result = bind_jiminy_ready(
                        args.session_id, args.lane, args.expected_revision, packet,
                        args.runner_session_id,
                    )
                elif args.graph_command == "review-attempt":
                    result = bind_review_attempt(
                        args.session_id,
                        args.lane,
                        args.expected_revision,
                        args.actor_session_id,
                    )
                else:
                    result = accept_graph_event(
                        args.session_id, args.lane, args.actor_session_id,
                        args.expected_revision, args.event, packet,
                        args.observed_pr_head_sha,
                        runner_session_id=args.runner_session_id,
                        review_attempt_id=args.review_attempt_id,
                    )
            except (OSError, ValueError) as error:
                print(f"orchestration_state: {error}", file=sys.stderr)
                return 1
            print(json.dumps(result, sort_keys=True))
            return 0
    with registry_lock():
        recover_transactions()
        return _dispatch(args)


if __name__ == "__main__":
    raise SystemExit(main())
