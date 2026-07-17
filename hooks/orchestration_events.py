"""Event handlers and policy for Codex orchestration hooks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from orchestration_state import write_state


JsonObject = dict[str, Any]
HookResult = JsonObject | None

LANE_RECEIPTS = {
    "research": "RESEARCH_PACKET:",
    "implementation": "IMPLEMENTATION_PACKET:",
    "review": "REVIEW_PACKET:",
}

AGENT_CONTRACTS = {
    "research": (
        "Work code-read-only; issue create/update remains allowed within declared authority. "
        "Decide keep, split, consolidate, clarify, or block; persist and reread the contract, "
        "then return only RESEARCH_PACKET."
    ),
    "implementation": (
        "Use Pinocchio as the sole writer. Deliver one leaf, branch, and PR; persist proof and return only "
        "IMPLEMENTATION_PACKET."
    ),
    "reviewer": (
        "Review the exact live head. Run fixers serially, rereview every new head, and return only "
        "REVIEW_PACKET."
    ),
    "fixer": (
        "Fix only the assigned finding, test it, push without force, and return compact proof to "
        "the reviewer."
    ),
}

READ_ONLY_ROLES = {"gepetto", "jiminy", "research"}
FORCE_PUSH = re.compile(r"\bgit\s+push\b[^\n]*(?:--force(?:-with-lease)?|-f\b)")
PR_MERGE = re.compile(r"\bgh\s+pr\s+merge\b")
BOUND_HEAD = re.compile(r"--match-head-commit(?:=|\s+)[0-9a-fA-F]{40}\b")


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
            write_state(self.state["session_id"], self.state)


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


def starts_with_receipt(message: Any, receipt: str) -> bool:
    return isinstance(message, str) and message.lstrip().startswith(receipt)


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


def next_agent_contract(state: JsonObject) -> tuple[str, str | None]:
    if state["role"] != "review":
        role = state["role"]
        return role, LANE_RECEIPTS.get(role)

    active_roles = {
        agent.get("role")
        for agent in state.get("agents", {}).values()
        if agent.get("active")
    }
    return ("fixer", None) if "reviewer" in active_roles else ("reviewer", "REVIEW_PACKET:")


def subagent_start(context: HookContext) -> HookResult:
    if not context.active or context.role not in LANE_RECEIPTS:
        return None

    agent_id = str(context.payload.get("agent_id", ""))
    if not agent_id:
        return None

    agent_role, receipt = next_agent_contract(context.state)
    context.state.setdefault("agents", {})[agent_id] = {
        "role": agent_role,
        "receipt": receipt,
        "active": True,
    }
    context.save()
    return additional_context("SubagentStart", AGENT_CONTRACTS[agent_role])


def subagent_stop(context: HookContext) -> JsonObject:
    if not context.state:
        return {}

    agent_id = str(context.payload.get("agent_id", ""))
    agent = context.state.get("agents", {}).get(agent_id)
    if not agent:
        return {}

    receipt = agent.get("receipt")
    valid = receipt is None or starts_with_receipt(
        context.payload.get("last_assistant_message"), receipt
    )
    if not valid and not context.payload.get("stop_hook_active"):
        return continue_turn(
            f"Finish with exactly one {receipt[:-1]} receipt after verifying its artifact."
        )

    agent["active"] = False
    context.save()
    return {}


def stop(context: HookContext) -> JsonObject:
    if not context.active:
        return {}

    receipt = LANE_RECEIPTS.get(context.role)
    valid = receipt is None or starts_with_receipt(
        context.payload.get("last_assistant_message"), receipt
    )
    if not valid and not context.payload.get("stop_hook_active"):
        return continue_turn(
            f"Verify the lane result and finish with exactly one {receipt[:-1]} receipt."
        )

    if receipt:
        context.state["active"] = False
        context.state["checkpoint_on_compact"] = False
        context.save()
    return {}


def pre_tool_use(context: HookContext) -> HookResult:
    if not context.active:
        return None

    tool_name = str(context.payload.get("tool_name", ""))
    tool_input = context.payload.get("tool_input") or {}
    command = str(tool_input.get("command", "")) if isinstance(tool_input, dict) else ""

    if tool_name == "apply_patch" and context.role in READ_ONLY_ROLES:
        return deny_tool(f"The registered {context.role} role is code-read-only.")

    if tool_name == "Bash" and FORCE_PUSH.search(command):
        return deny_tool("Force-pushing is forbidden in Gepetto-managed work.")

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
