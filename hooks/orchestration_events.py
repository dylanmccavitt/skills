"""Event handlers and policy for Codex orchestration hooks."""

from __future__ import annotations

import os
import re
import shlex
import time
from dataclasses import dataclass
from typing import Any, Callable

from orchestration_packets import canonical_packet_digest, parse_packet_message
from orchestration_state import write_state


JsonObject = dict[str, Any]
HookResult = JsonObject | None

LANE_RECEIPTS = {
    "research": "RESEARCH_PACKET",
    "implementation": "IMPLEMENTATION_PACKET",
    "review": "REVIEW_PACKET",
    "jiminy": "JIMINY_COMPLETE",
}

AGENT_CONTRACTS = {
    "research": (
        "Work code-read-only; issue create/update remains allowed within declared authority. "
        "Decide keep, split, consolidate, clarify, or block; persist and reread the contract, "
        "then return compact verified evidence to the parent lane, not a terminal RESEARCH_PACKET."
    ),
    "implementation": (
        "Use Pinocchio as the sole writer. Deliver one leaf, branch, and PR; persist proof and return compact "
        "verified evidence to the parent lane, not a terminal IMPLEMENTATION_PACKET."
    ),
    "reviewer": (
        "Collect all actionable findings, run one fixer pass, re-review the changed delta, and "
        "return compact review evidence to the parent lane, not a terminal REVIEW_PACKET."
    ),
    "fixer": (
        "Apply the assigned fixes serially, test each, push without force, and return compact "
        "proof to the reviewer."
    ),
}

READ_ONLY_ROLES = {"gepetto", "jiminy", "research"}
PR_MERGE = re.compile(r"\bgh\s+pr\s+merge\b")
API_MERGE = re.compile(r"\bgh\s+api\b")
MERGE_ENDPOINT = re.compile(r"pulls/\d+/merge\b|\bmergePullRequest\b")
BOUND_HEAD = re.compile(r"--match-head-commit(?:=|\s+)[0-9a-fA-F]{40}\b")
PYTHON_EXECUTABLE = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")
STATE_MODULES = {"orchestration_state", "hooks.orchestration_state"}
SHELL_CONTROLS = {";", "&&", "||", "&", "|"}


def _is_force_push(command: str) -> bool:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return bool(
            re.search(
                r"\bgit\s+push\b[^\n]*(?:--force(?:-with-lease)?|\s-[A-Za-z]*f[A-Za-z]*\b)",
                command,
            )
        )

    for index, token in enumerate(tokens[:-1]):
        if os.path.basename(token) != "git" or tokens[index + 1] != "push":
            continue
        options_enabled = True
        for argument in tokens[index + 2:]:
            if argument in SHELL_CONTROLS:
                break
            if argument == "--":
                options_enabled = False
                continue
            if not options_enabled:
                continue
            if argument == "--force" or argument.startswith("--force-with-lease"):
                return True
            if (
                argument.startswith("-")
                and not argument.startswith("--")
                and "f" in argument[1:]
            ):
                return True
    return False


def _argv_option(arguments: list[str], option: str) -> str | None:
    for index, value in enumerate(arguments):
        if value == option and index + 1 < len(arguments):
            return arguments[index + 1]
        if value.startswith(f"{option}="):
            return value.split("=", 1)[1]
    return None


def _state_cli_invocations(command: str) -> tuple[list[list[str]], bool]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return [], "orchestration_state" in command

    invocations: list[list[str]] = []
    ambiguous = False
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in SHELL_CONTROLS:
            segments.append([])
        else:
            segments[-1].append(token)

    for segment in segments:
        matched_positions: set[int] = set()
        for index, token in enumerate(segment):
            if os.path.basename(token) == "orchestration_state.py":
                invocations.append(segment[index + 1:])
                matched_positions.add(index)
                continue
            executable = os.path.basename(token)
            if (
                PYTHON_EXECUTABLE.fullmatch(executable)
                and index + 2 < len(segment)
                and segment[index + 1] == "-m"
                and segment[index + 2] in STATE_MODULES
            ):
                invocations.append(segment[index + 3:])
                matched_positions.add(index + 2)
        if any(
            "orchestration_state" in token and index not in matched_positions
            for index, token in enumerate(segment)
        ):
            ambiguous = True
    return invocations, ambiguous


@dataclass
class HookContext:
    payload: JsonObject
    state: JsonObject | None

    @property
    def event(self) -> str:
        return str(self.payload.get("hook_event_name", ""))

    @property
    def role(self) -> str | None:
        return self.state.get("role") if self.state else None

    @property
    def active(self) -> bool:
        return bool(self.state and self.state.get("active"))

    def save(self) -> None:
        if self.state:
            write_state(
                self.state["session_id"],
                self.state,
                expected_revision=int(self.state.get("state_revision", 0)),
            )


def additional_context(event: str, message: str) -> JsonObject:
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": message,
        }
    }


def deny_tool(reason: str) -> JsonObject:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def continue_turn(reason: str) -> JsonObject:
    return {"decision": "block", "reason": reason}


def session_start(context: HookContext) -> HookResult:
    if context.payload.get("source") != "compact" or not context.active:
        return None
    if not context.state.get("checkpoint_on_compact"):
        return None
    return additional_context(
        "SessionStart",
        f"This {context.role} task was compacted. Use $checkpoint now. "
        "Preserve the registered role and task relationships; leave both tasks unarchived.",
    )


def next_agent_contract(state: JsonObject) -> str:
    if state["role"] != "review":
        return state["role"]

    agents = state.get("agents", {})
    if not isinstance(agents, dict):
        raise ValueError("invalid agents container in orchestration state")
    active_roles = {
        agent.get("role")
        for agent in agents.values()
        if isinstance(agent, dict)
        if agent.get("active")
    }
    return "fixer" if "reviewer" in active_roles else "reviewer"


def subagent_start(context: HookContext) -> HookResult:
    if not context.active or context.role not in {"research", "implementation", "review"}:
        return None

    agent_id = str(context.payload.get("agent_id", ""))
    if not agent_id:
        return None

    agent_role = next_agent_contract(context.state)
    agents = context.state.get("agents")
    if not isinstance(agents, dict):
        raise ValueError("invalid agents container in orchestration state")
    agents[agent_id] = {
        "role": agent_role,
        "receipt": None,
        "active": True,
    }
    context.save()
    return additional_context("SubagentStart", AGENT_CONTRACTS[agent_role])


def subagent_stop(context: HookContext) -> JsonObject:
    if not context.state:
        return {}

    agent_id = str(context.payload.get("agent_id", ""))
    agents = context.state.get("agents", {})
    if not isinstance(agents, dict):
        raise ValueError("invalid agents container in orchestration state")
    agent = agents.get(agent_id)
    if not agent:
        return {}

    agent["active"] = False
    context.save()
    return {}


def stop(context: HookContext) -> JsonObject:
    if not context.active:
        return {}

    receipt = LANE_RECEIPTS.get(context.role)
    valid = receipt is None
    packet_error = ""
    packet: JsonObject | None = None
    if receipt is not None:
        try:
            _, packet = parse_packet_message(
                context.payload.get("last_assistant_message"), expected_type=receipt
            )
            valid = True
        except ValueError as error:
            valid = False
            packet_error = str(error)
    if not valid:
        if context.payload.get("stop_hook_active"):
            context.state["forced_stop_without_receipt"] = True
            context.save()
            return {}
        return continue_turn(
            f"Verify the lane result and finish with exactly one valid {receipt} receipt. "
            f"Packet validation failed: {packet_error}"
        )

    if receipt:
        if packet is None:
            raise ValueError("validated terminal packet is missing")
        context.state["terminal_packet_type"] = receipt
        context.state["terminal_packet_digest"] = canonical_packet_digest(packet)
        context.state["active"] = False
        context.state["checkpoint_on_compact"] = False
        lifecycle = context.state.get("lifecycle")
        if isinstance(lifecycle, dict):
            lifecycle["ended_at"] = int(time.time())
            lifecycle["end_reason"] = "terminal-receipt"
        context.save()
    return {}


def pre_tool_use(context: HookContext) -> HookResult:
    if not context.active:
        return None

    tool_name = str(context.payload.get("tool_name", ""))
    tool_input = context.payload.get("tool_input") or {}
    command = str(tool_input.get("command", "")) if isinstance(tool_input, dict) else ""

    if tool_name in {"apply_patch", "Edit", "Write"} and context.role in READ_ONLY_ROLES:
        return deny_tool(f"The registered {context.role} role is code-read-only.")

    if tool_name == "Bash" and _is_force_push(command):
        return deny_tool("Force-pushing is forbidden in Gepetto-managed work.")

    if tool_name == "Bash" and context.role != "gepetto":
        invocations, ambiguous = _state_cli_invocations(command)
        if ambiguous:
            return deny_tool("Ambiguous orchestration state CLI invocation is denied for child lanes.")
        for arguments in invocations:
            state_command = arguments[0] if arguments else ""
            if "--merge-authorized" in arguments or state_command == "register":
                return deny_tool("Only the active Gepetto coordinator may create or authorize registrations.")
            if state_command in {"ledger", "graph"}:
                return deny_tool("Only the active Gepetto coordinator may mutate the delivery ledger or graph.")
            if state_command == "complete":
                target = _argv_option(arguments, "--session-id")
                if target != context.state.get("session_id"):
                    return deny_tool("A child lane cannot complete another orchestration session.")
            if state_command == "continue":
                source = _argv_option(arguments, "--source-id")
                if source != context.state.get("session_id"):
                    return deny_tool("A child lane cannot continue another orchestration session.")

    if tool_name == "Bash" and API_MERGE.search(command) and MERGE_ENDPOINT.search(command):
        return deny_tool(
            "Merging via gh api is forbidden; use gh pr merge --match-head-commit <sha> "
            "from a merge-authorized Jiminy task."
        )

    if tool_name == "Bash" and PR_MERGE.search(command):
        if context.role != "jiminy" or not context.state.get("merge_authorized"):
            return deny_tool(
                "Only a merge-authorized Jiminy task may merge a Gepetto-managed PR."
            )
        if not BOUND_HEAD.search(command):
            return deny_tool(
                "Bind the merge to the verified head with --match-head-commit <40-character SHA>."
            )
    return None


HANDLERS: dict[str, Callable[[HookContext], HookResult]] = {
    "SessionStart": session_start,
    "SubagentStart": subagent_start,
    "SubagentStop": subagent_stop,
    "Stop": stop,
    "PreToolUse": pre_tool_use,
}
