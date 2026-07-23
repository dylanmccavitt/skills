"""Event handlers and policy for Codex orchestration hooks."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from typing import Any, Callable

from orchestration_packets import canonical_packet_digest, parse_packet_message
from orchestration_state import verify_merge_authority, write_state


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
MERGE_ENDPOINT = re.compile(r"pulls/\d+/merge\b|\bmergePullRequest\b")
PYTHON_EXECUTABLE = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")
STATE_MODULES = {"orchestration_state", "hooks.orchestration_state"}
SHELL_CONTROLS = {";", "&&", "||", "&", "|"}
FILE_WRITE_COMMANDS = {
    "cp", "dd", "install", "ln", "mkdir", "mv", "patch", "rm", "rmdir",
    "rsync", "sponge", "tee", "touch", "truncate", "unlink",
}
GIT_WRITE_COMMANDS = {
    "add", "am", "apply", "branch", "checkout", "cherry-pick", "clean", "commit",
    "config", "fast-import", "fetch", "gc", "init", "maintenance", "merge", "mv",
    "notes", "pack-refs", "prune", "pull", "push", "rebase", "remote", "replace",
    "reset", "restore", "rm", "sparse-checkout", "stash", "submodule", "switch",
    "tag", "update-index", "update-ref", "worktree",
}
GH_GLOBAL_VALUE_OPTIONS = {"--hostname", "--repo", "-R"}


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


def _bash_writes_files(command: str, *, _depth: int = 0) -> bool:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return bool(re.search(
            r"(?:^|\s)(?:cp|install|ln|mkdir|mv|patch|rm|rmdir|tee|touch|truncate|unlink)\b|>",
            command,
        ))
    if any(">" in token for token in tokens):
        return True
    for index, token in enumerate(tokens):
        executable = os.path.basename(token)
        if executable in FILE_WRITE_COMMANDS:
            return True
        if executable in {"bash", "dash", "ksh", "sh", "zsh"}:
            shell_arguments = tokens[index + 1:]
            for argument_index, argument in enumerate(shell_arguments):
                if argument in SHELL_CONTROLS:
                    break
                if argument == "-c" or (
                    argument.startswith("-")
                    and not argument.startswith("--")
                    and "c" in argument[1:]
                ):
                    if argument_index + 1 < len(shell_arguments):
                        if _depth >= 4 or _bash_writes_files(
                            shell_arguments[argument_index + 1], _depth=_depth + 1
                        ):
                            return True
                    break
        if executable == "eval":
            arguments: list[str] = []
            for argument in tokens[index + 1:]:
                if argument in SHELL_CONTROLS:
                    break
                arguments.append(argument)
            if arguments and (
                _depth >= 4
                or _bash_writes_files(" ".join(arguments), _depth=_depth + 1)
            ):
                return True
        if executable in {"sed", "perl"}:
            arguments = tokens[index + 1:]
            for argument in arguments:
                if argument in SHELL_CONTROLS or argument == "--":
                    break
                if (
                    argument == "--in-place"
                    or argument.startswith("--in-place=")
                    or (
                        argument.startswith("-")
                        and not argument.startswith("--")
                        and "i" in argument[1:]
                    )
                ):
                    return True
        if executable == "git" and index + 1 < len(tokens):
            arguments = tokens[index + 1:]
            value_options = {
                "-C", "-c", "--config-env", "--exec-path", "--git-dir",
                "--namespace", "--super-prefix", "--work-tree",
            }
            subcommand = None
            skip = False
            for argument in arguments:
                if argument in SHELL_CONTROLS or argument == "--":
                    break
                if skip:
                    skip = False
                    continue
                if argument in value_options:
                    skip = True
                    continue
                if any(argument.startswith(f"{option}=") for option in value_options):
                    continue
                if argument.startswith(("-C", "-c")) and len(argument) > 2:
                    continue
                if argument.startswith("-"):
                    continue
                subcommand = argument
                break
            if subcommand in GIT_WRITE_COMMANDS:
                return True
            if subcommand == "symbolic-ref":
                subcommand_index = arguments.index(subcommand)
                symbolic_arguments = arguments[subcommand_index + 1:]
                positionals: list[str] = []
                skip = False
                for argument in symbolic_arguments:
                    if argument in SHELL_CONTROLS:
                        break
                    if skip:
                        skip = False
                        continue
                    if argument == "-m":
                        skip = True
                        continue
                    if argument == "--delete":
                        return True
                    if not argument.startswith("-"):
                        positionals.append(argument)
                if len(positionals) >= 2:
                    return True
            if subcommand == "reflog":
                subcommand_index = arguments.index(subcommand)
                action = "show"
                for argument in arguments[subcommand_index + 1:]:
                    if argument in SHELL_CONTROLS:
                        break
                    if not argument.startswith("-"):
                        action = argument
                        break
                if action in {"delete", "drop", "expire", "write"}:
                    return True
        if executable == "find" and "-delete" in tokens[index + 1:]:
            return True
    return False


def _gh_invocations(
    command: str, *, _depth: int = 0
) -> tuple[list[list[str]], bool]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return [], bool(re.search(r"(?:^|[\s\"'\\/])gh(?:[\s\"'\\/]|$)", command))

    invocations: list[list[str]] = []
    ambiguous = False
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in SHELL_CONTROLS:
            segments.append([])
        else:
            segments[-1].append(token)
    for segment in segments:
        for index, token in enumerate(segment):
            executable = os.path.basename(token)
            if executable == "gh":
                invocations.append(segment[index + 1:])
            if executable in {"bash", "dash", "ksh", "sh", "zsh"}:
                shell_arguments = segment[index + 1:]
                for argument_index, argument in enumerate(shell_arguments):
                    if argument == "-c" or (
                        argument.startswith("-")
                        and not argument.startswith("--")
                        and "c" in argument[1:]
                    ):
                        if argument_index + 1 < len(shell_arguments):
                            if _depth >= 4:
                                ambiguous = True
                            else:
                                nested, nested_ambiguous = _gh_invocations(
                                    shell_arguments[argument_index + 1],
                                    _depth=_depth + 1,
                                )
                                invocations.extend(nested)
                                ambiguous = ambiguous or nested_ambiguous
                        break
            if executable == "eval":
                evaluated = segment[index + 1:]
                if evaluated:
                    if _depth >= 4:
                        ambiguous = True
                    else:
                        nested, nested_ambiguous = _gh_invocations(
                            " ".join(evaluated), _depth=_depth + 1
                        )
                        invocations.extend(nested)
                        ambiguous = ambiguous or nested_ambiguous
    return invocations, ambiguous


def _gh_subcommand(arguments: list[str]) -> tuple[str | None, int | None]:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in GH_GLOBAL_VALUE_OPTIONS:
            if index + 1 >= len(arguments):
                return None, None
            index += 2
            continue
        if any(
            argument.startswith(f"{option}=")
            for option in GH_GLOBAL_VALUE_OPTIONS
        ):
            index += 1
            continue
        if argument.startswith("-R") and len(argument) > 2:
            index += 1
            continue
        if argument in {"--help", "--version"}:
            index += 1
            continue
        if argument.startswith("-"):
            return None, None
        return argument, index
    return None, None


def _merge_scope(command: str) -> tuple[str, str, str] | None:
    from orchestration_contract import normalize_github_url, normalize_repository

    invocations, ambiguous = _gh_invocations(command)
    if ambiguous:
        return None
    merge_invocations: list[tuple[list[str], int]] = []
    for invocation in invocations:
        subcommand, subcommand_index = _gh_subcommand(invocation)
        if (
            subcommand == "pr"
            and isinstance(subcommand_index, int)
            and invocation[subcommand_index + 1:subcommand_index + 2] == ["merge"]
        ):
            merge_invocations.append((invocation, subcommand_index))
    if len(merge_invocations) != 1:
        return None
    for invocation, subcommand_index in merge_invocations:
        arguments = invocation[subcommand_index + 2:]
        if any(
            argument in {"--admin", "--delete-branch"}
            or argument.startswith(("--admin=", "--delete-branch="))
            or (
                argument.startswith("-")
                and not argument.startswith("--")
                and "d" in argument[1:]
            )
            for argument in arguments
        ):
            return None
        def option_values(source: list[str], *options: str) -> list[str]:
            found: list[str] = []
            skip = False
            for argument_index, argument in enumerate(source):
                if skip:
                    skip = False
                    continue
                if argument in options:
                    if argument_index + 1 >= len(source):
                        return []
                    found.append(source[argument_index + 1])
                    skip = True
                    continue
                for option in options:
                    if argument.startswith(f"{option}="):
                        found.append(argument.split("=", 1)[1])
                    elif option == "-R" and argument.startswith("-R") and len(argument) > 2:
                        found.append(argument[2:])
            return found

        repositories = option_values(
            invocation[:subcommand_index] + arguments, "--repo", "-R"
        )
        heads = option_values(arguments, "--match-head-commit")
        if len(repositories) != 1 or len(heads) != 1:
            return None
        repository = repositories[0]
        head = heads[0]
        value_options = {
            "--repo", "-R", "--match-head-commit", "--subject", "--body", "--body-file",
        }
        targets: list[str] = []
        skip = False
        for argument_index, argument in enumerate(arguments):
            if skip:
                skip = False
                continue
            if argument in value_options:
                skip = True
                continue
            if any(argument.startswith(f"{option}=") for option in value_options):
                continue
            if argument.startswith("-"):
                continue
            targets.append(argument)
        if len(targets) != 1:
            return None
        target = targets[0]
        repository = normalize_repository(repository)
        if target.isdigit() and int(target) > 0:
            pr_url = f"https://github.com/{repository}/pull/{int(target)}"
        else:
            pr_url = normalize_github_url(target, kind="pull")
        return repository, pr_url, head.lower()
    return None


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

    if (
        tool_name == "Bash"
        and context.role in READ_ONLY_ROLES
        and _bash_writes_files(command)
    ):
        return deny_tool(
            f"The registered {context.role} role cannot run Bash commands that write files."
        )

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
            if state_command == "claim" and arguments[1:2] in (["acquire"], ["release"]):
                return deny_tool("Only the active Gepetto coordinator may mutate ownership claims.")
            if state_command == "complete":
                target = _argv_option(arguments, "--session-id")
                if target != context.state.get("session_id"):
                    return deny_tool("A child lane cannot complete another orchestration session.")
            if state_command == "continue":
                source = _argv_option(arguments, "--source-id")
                if source != context.state.get("session_id"):
                    return deny_tool("A child lane cannot continue another orchestration session.")

    gh_invocations, ambiguous_gh = _gh_invocations(command) if tool_name == "Bash" else ([], False)
    if ambiguous_gh and "merge" in command:
        return deny_tool("Ambiguous GitHub CLI merge invocation is denied.")

    classified_gh = [
        (arguments, *_gh_subcommand(arguments))
        for arguments in gh_invocations
    ]
    if any(
        subcommand == "api"
        and isinstance(subcommand_index, int)
        and any(
            MERGE_ENDPOINT.search(argument)
            for argument in arguments[subcommand_index + 1:]
        )
        for arguments, subcommand, subcommand_index in classified_gh
    ):
        return deny_tool(
            "Merging via gh api is forbidden; use gh pr merge --match-head-commit <sha> "
            "from a merge-authorized Jiminy task."
        )

    pr_merge_invocations = [
        arguments
        for arguments, subcommand, subcommand_index in classified_gh
        if subcommand == "pr"
        and isinstance(subcommand_index, int)
        and arguments[subcommand_index + 1:subcommand_index + 2] == ["merge"]
    ]
    if pr_merge_invocations:
        if context.role != "jiminy":
            return deny_tool(
                "Only a merge-authorized Jiminy task may merge a Gepetto-managed PR."
            )
        scope = _merge_scope(command)
        if scope is None:
            return deny_tool(
                "Bind the merge to an explicit --repo, PR, and verified full head SHA."
            )
        try:
            verify_merge_authority(context.state["session_id"], *scope)
        except ValueError as error:
            return deny_tool(f"Coordinator-scoped merge authority denied this command: {error}")
    return None


HANDLERS: dict[str, Callable[[HookContext], HookResult]] = {
    "SessionStart": session_start,
    "SubagentStart": subagent_start,
    "SubagentStop": subagent_stop,
    "Stop": stop,
    "PreToolUse": pre_tool_use,
}
