"""Experiment v2e: conservative anti-loop watcher.

A ``react`` continuation hook inspects the agent's recent tool calls after every turn.
When the same command keeps being repeated with no change in its output, keeps failing
with the same error, or a shell is read again and again while empty, the hook inserts
one short user message asking the agent to diagnose and change strategy.

The watcher only observes and warns. It never terminates the run, never kills a
process, never blocks a tool call, and leaves the baseline continuation behaviour
untouched when it has nothing to say. Events are appended to
``work/watcher_events.jsonl`` in the sandbox.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from inspect_ai.agent import AgentContinue, AgentState
from inspect_ai.model import ChatMessage, ChatMessageAssistant, ChatMessageTool
from inspect_ai.util import sandbox, store

SANDBOX_EVENTS_PATH = "/workspace/work/watcher_events.jsonl"
STORE_KEY = "anti_loop_watcher"

WINDOW = 14  # most recent tool calls examined
POLL_THRESHOLD = 6  # identical call with identical non-error output
FAILURE_THRESHOLD = 4  # identical call with the same error
INSTALL_FAILURE_THRESHOLD = 3  # identical package-install call with the same error
EMPTY_READ_THRESHOLD = 8  # shell reads returning nothing
COOLDOWN_CALLS = 12  # tool calls before the same pattern may warn again
MAX_WARNINGS = 12  # per run

WARNING_PREFIX = "Repeated command pattern detected. Diagnose cause and change strategy before retrying."

_ERROR_MARKERS = re.compile(
    r"command not found|No such file or directory|Traceback \(most recent call last\)|"
    r"^Error\b|\bERROR\b|\berror:|Execution halted|non-zero exit status|Permission denied|"
    r"cannot open|is not available for this version of R|there is no package called|"
    r"Could not find a version|No matching distribution|PackagesNotFoundError|Unable to locate package",
    re.IGNORECASE | re.MULTILINE,
)
_INSTALL_MARKERS = re.compile(
    r"pip3? install|install\.packages|BiocManager::install|(conda|mamba|micromamba) install|"
    r"apt(-get)? install|remotes::install|pak::pkg_install|devtools::install",
    re.IGNORECASE,
)
_SHELL_PROMPT = re.compile(r"^\S*@\S+:[^\n#$]*[#$] ?", re.MULTILINE)


@dataclass(frozen=True)
class ToolEvent:
    function: str
    command: str
    key: str
    output_key: str
    empty: bool
    error: bool
    install: bool
    bare_read: bool


@dataclass(frozen=True)
class Detection:
    kind: str
    key: str
    count: int
    command: str
    detail: str


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def normalize_command(function: str, arguments: dict[str, Any]) -> str:
    if function == "bash_session":
        action = str(arguments.get("action", ""))
        text = str(arguments.get("input") or "")
        return f"{action}: {' '.join(text.split())}".strip()
    text = arguments.get("code") or arguments.get("cmd") or arguments.get("command")
    if text is None:
        text = json.dumps(arguments, sort_keys=True, default=str)
    return " ".join(str(text).split())


def normalize_output(text: str) -> str:
    return " ".join(_SHELL_PROMPT.sub("", text).split())


def extract_events(messages: list[ChatMessage], window: int = WINDOW) -> list[ToolEvent]:
    """Pair recent tool calls with their results, oldest first."""
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    events: list[ToolEvent] = []
    for message in messages[-(window * 4):]:
        if isinstance(message, ChatMessageAssistant):
            for call in message.tool_calls or []:
                calls[call.id] = (call.function, dict(call.arguments or {}))
        elif isinstance(message, ChatMessageTool) and message.tool_call_id in calls:
            function, arguments = calls[message.tool_call_id]
            command = normalize_command(function, arguments)
            output = normalize_output(message.text or "")
            failed = message.error is not None or bool(_ERROR_MARKERS.search(message.text or ""))
            events.append(ToolEvent(
                function=function,
                command=command,
                key=_digest(f"{function}\0{command}"),
                output_key=_digest(output),
                empty=not output,
                error=failed,
                install=bool(_INSTALL_MARKERS.search(command)),
                bare_read=function == "bash_session" and command.rstrip(":").strip() in ("read", "read:"),
            ))
    return events[-window:]


def detect(events: list[ToolEvent]) -> Detection | None:
    """Return the strongest repeated pattern in the recent events, if any."""
    if not events:
        return None
    last = events[-1]
    same = [event for event in events if event.key == last.key]
    same_output = [event for event in same if event.output_key == last.output_key]
    shown = last.command if len(last.command) <= 240 else last.command[:240] + " ..."

    if last.error:
        threshold = INSTALL_FAILURE_THRESHOLD if last.install else FAILURE_THRESHOLD
        if len(same_output) >= threshold:
            kind = "repeated_install_failure" if last.install else "repeated_failure"
            what = "package installation" if last.install else "command"
            return Detection(kind, last.key, len(same_output), shown,
                             f"The same {what} has failed {len(same_output)} times with the same error. "
                             "Read the error, find its cause, and try a different approach instead of retrying it.")
    empty_reads = [event for event in events if event.bare_read and event.empty]
    if last.bare_read and last.empty and len(empty_reads) >= EMPTY_READ_THRESHOLD:
        return Detection("empty_reads", "bash_session-empty-read", len(empty_reads), shown,
                         f"The shell has been read {len(empty_reads)} times recently with no output. "
                         "The session may be waiting for input or stuck: check what it is running, or restart it, "
                         "rather than reading again.")
    if not last.error and len(same_output) >= POLL_THRESHOLD:
        kind = "empty_output_polling" if last.empty else "polling_no_change"
        state = "returned nothing" if last.empty else "returned exactly the same output"
        return Detection(kind, last.key, len(same_output), shown,
                         f"The same command has {state} {len(same_output)} times. If a long job is legitimately "
                         "still running, wait much longer between checks (use sleep) or check a signal that "
                         "changes; if it is not running, find out why.")
    return None


def should_warn(detection: Detection, state: dict[str, Any], total_calls: int) -> bool:
    if int(state.get("warnings", 0)) >= MAX_WARNINGS:
        return False
    last = state.get("last_warned", {}).get(f"{detection.kind}:{detection.key}")
    return last is None or total_calls - int(last) >= COOLDOWN_CALLS


def warning_message(detection: Detection) -> str:
    text = f"{WARNING_PREFIX}\n\n{detection.detail}\n\nRepeated command: {detection.command}"
    # react() passes continuation strings through str.format(submit=...): escape braces so
    # shell or R code quoted in the warning can never raise inside the agent loop.
    return text.replace("{", "{{").replace("}", "}}")


async def _log_event(event: dict[str, Any]) -> None:
    try:
        await sandbox().exec(["sh", "-c", f"cat >> {SANDBOX_EVENTS_PATH}"], input=json.dumps(event) + "\n")
    except Exception:  # logging must never disturb the run
        return


def anti_loop_watcher() -> AgentContinue:
    async def on_continue(state: AgentState) -> bool | str:
        try:
            events = extract_events(state.messages)
            detection = detect(events)
            if detection is None:
                return True
            memory: dict[str, Any] = dict(store().get(STORE_KEY, {}))
            total_calls = sum(
                len(message.tool_calls or []) for message in state.messages
                if isinstance(message, ChatMessageAssistant)
            )
            # Message compaction can shrink the history; never let the counter move backwards.
            total_calls = max(total_calls, int(memory.get("total_calls", 0)) + 1)
            memory["total_calls"] = total_calls
            warn = should_warn(detection, memory, total_calls)
            if warn:
                memory["warnings"] = int(memory.get("warnings", 0)) + 1
                memory.setdefault("last_warned", {})[f"{detection.kind}:{detection.key}"] = total_calls
            store().set(STORE_KEY, memory)
            if not warn:
                return True
            await _log_event({
                "kind": detection.kind, "count": detection.count, "command": detection.command,
                "tool_calls_so_far": total_calls, "warning_number": memory["warnings"],
            })
            return warning_message(detection)
        except Exception:  # a broken watcher must behave exactly like no watcher
            return True

    return on_continue
