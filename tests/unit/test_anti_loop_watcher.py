"""Experiment v2e: conservative anti-loop watcher."""

from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from inspect_ai.agent import AgentState
from inspect_ai.model import ChatMessageAssistant, ChatMessageTool, ChatMessageUser
from inspect_ai.tool import ToolCall, ToolCallError

from bixbench3.inspect_harness import anti_loop_watcher as module
from bixbench3.inspect_harness.anti_loop_watcher import (
    COOLDOWN_CALLS,
    EMPTY_READ_THRESHOLD,
    FAILURE_THRESHOLD,
    INSTALL_FAILURE_THRESHOLD,
    MAX_WARNINGS,
    POLL_THRESHOLD,
    WARNING_PREFIX,
    detect,
    extract_events,
    warning_message,
)

REPO = Path(__file__).resolve().parents[2]


def _turn(index: int, function: str, arguments: dict, output: str, *, error: bool = False) -> list:
    call = ToolCall(id=f"call-{index}", function=function, arguments=arguments)
    return [
        ChatMessageAssistant(content="", tool_calls=[call]),
        ChatMessageTool(
            content=output, tool_call_id=call.id, function=function,
            error=ToolCallError(type="unknown", message=output) if error else None,
        ),
    ]


def _bash(index: int, command: str, output: str, **kwargs) -> list:
    return _turn(index, "bash_session", {"action": "type_submit", "input": command}, output, **kwargs)


def _history(turns: list[list]) -> list:
    return [ChatMessageUser(content="task"), *[message for turn in turns for message in turn]]


def test_varied_work_and_progressing_polls_are_never_flagged() -> None:
    varied = _history([_bash(i, f"step {i}", f"result {i}") for i in range(30)])
    assert detect(extract_events(varied)) is None
    progressing = _history([_bash(i, "ls counts | wc -l", f"root@box:/workspace# {i * 3}") for i in range(30)])
    assert detect(extract_events(progressing)) is None


def test_identical_poll_output_is_flagged_only_at_the_threshold() -> None:
    def polls(count: int) -> list:
        return _history([_bash(i, "tail -n 2 run.log", "root@box:/workspace# aligning sample 7") for i in range(count)])

    assert detect(extract_events(polls(POLL_THRESHOLD - 1))) is None
    detection = detect(extract_events(polls(POLL_THRESHOLD)))
    assert detection is not None and detection.kind == "polling_no_change" and detection.count == POLL_THRESHOLD


def test_empty_output_polling_and_empty_shell_reads() -> None:
    empty = _history([_bash(i, "cat build.log", "root@box:/workspace# ") for i in range(POLL_THRESHOLD)])
    assert detect(extract_events(empty)).kind == "empty_output_polling"
    reads = _history([_turn(i, "bash_session", {"action": "read"}, "") for i in range(EMPTY_READ_THRESHOLD)])
    assert detect(extract_events(reads)).kind == "empty_reads"
    few = _history([_turn(i, "bash_session", {"action": "read"}, "") for i in range(POLL_THRESHOLD - 1)])
    assert detect(extract_events(few)) is None


def test_repeated_failures_and_install_failures_use_lower_thresholds() -> None:
    failing = [_bash(i, "Rscript count.R", "Error in library(QuasR) : there is no package called 'QuasR'")
               for i in range(FAILURE_THRESHOLD)]
    assert detect(extract_events(_history(failing[:-1]))) is None
    assert detect(extract_events(_history(failing))).kind == "repeated_failure"
    install = [_bash(i, 'Rscript -e \'BiocManager::install("piano")\'', "ERROR: dependency 'x' is not available")
               for i in range(INSTALL_FAILURE_THRESHOLD)]
    assert detect(extract_events(_history(install))).kind == "repeated_install_failure"
    tool_error = [_turn(i, "python", {"code": "import piano"}, "ModuleNotFoundError", error=True)
                  for i in range(FAILURE_THRESHOLD)]
    assert detect(extract_events(_history(tool_error))).kind == "repeated_failure"


def test_a_different_error_each_time_is_not_a_loop() -> None:
    turns = [_bash(i, "make", f"error: undefined reference to symbol_{i}") for i in range(FAILURE_THRESHOLD + 2)]
    assert detect(extract_events(_history(turns))) is None


def test_warning_text_is_safe_for_str_format() -> None:
    turns = [_bash(i, "awk '{print $1}' x | Rscript -e 'f <- function() {1}'", "Error: boom") for i in range(4)]
    message = warning_message(detect(extract_events(_history(turns))))
    rendered = message.format(submit="submit")
    assert rendered.startswith(WARNING_PREFIX)
    assert "awk '{print $1}'" in rendered


def _run_hook(messages: list, memory: dict, logged: list, monkeypatch: pytest.MonkeyPatch):
    async def fake_exec(cmd: list[str], input: str | None = None):
        logged.append(json.loads(input))
        return SimpleNamespace(success=True)

    monkeypatch.setattr(module, "sandbox", lambda: SimpleNamespace(exec=fake_exec))
    monkeypatch.setattr(module, "store", lambda: SimpleNamespace(get=memory.get, set=memory.__setitem__))
    return asyncio.run(module.anti_loop_watcher()(AgentState(messages=messages)))


def test_hook_warns_once_then_respects_cooldown_and_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    memory: dict = {}
    logged: list = []
    turns = [_bash(i, "tail run.log", "root@box:/workspace# same") for i in range(POLL_THRESHOLD)]
    assert _run_hook(_history(turns[:-1]), memory, logged, monkeypatch) is True
    first = _run_hook(_history(turns), memory, logged, monkeypatch)
    assert isinstance(first, str) and first.startswith(WARNING_PREFIX)
    assert logged[0]["kind"] == "polling_no_change" and logged[0]["warning_number"] == 1
    for extra in range(1, COOLDOWN_CALLS):
        more = turns + [_bash(100 + i, "tail run.log", "root@box:/workspace# same") for i in range(extra)]
        assert _run_hook(_history(more), memory, logged, monkeypatch) is True
    more = turns + [_bash(100 + i, "tail run.log", "root@box:/workspace# same") for i in range(COOLDOWN_CALLS)]
    assert isinstance(_run_hook(_history(more), memory, logged, monkeypatch), str)
    memory[module.STORE_KEY]["warnings"] = MAX_WARNINGS
    memory[module.STORE_KEY]["last_warned"] = {}
    assert _run_hook(_history(more), memory, logged, monkeypatch) is True
    assert len(logged) == 2


def test_hook_keeps_baseline_behaviour_when_idle_or_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run_hook(_history([_bash(1, "ls", "a b c")]), {}, [], monkeypatch) is True
    assert _run_hook([ChatMessageUser(content="task"), ChatMessageAssistant(content="thinking")], {}, [], monkeypatch) is True

    def broken(messages):
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "extract_events", broken)
    assert asyncio.run(module.anti_loop_watcher()(AgentState(messages=[]))) is True


def test_watcher_is_wired_into_the_agent_without_other_changes() -> None:
    source = (REPO / "src/bixbench3/inspect_harness/solvers.py").read_text()
    assert "on_continue=anti_loop_watcher()," in source
    assert "bash_session(timeout=600)" in source and "compaction=CompactionAuto()" in source
    watcher = (REPO / "src/bixbench3/inspect_harness/anti_loop_watcher.py").read_text()
    assert "kill" not in watcher.replace("never kills", "") and "interrupt" not in watcher


def test_replay_on_the_recorded_baseline_trace_is_sparse() -> None:
    """Replay the 8-vCPU baseline transcript turn by turn: warnings must stay rare."""
    trace = REPO / "iterations" / "baseline-task11" / "inspect.json.gz"
    if not trace.is_file():
        pytest.skip("baseline trace is not available in this checkout")
    with gzip.open(trace, "rt", encoding="utf-8") as handle:
        raw = json.load(handle)["samples"][0]["messages"]
    messages: list = []
    for item in raw:
        text = item["content"] if isinstance(item["content"], str) else json.dumps(item["content"])
        if item["role"] == "assistant":
            calls = [ToolCall(id=c["id"], function=c["function"], arguments=c["arguments"])
                     for c in item.get("tool_calls") or []]
            messages.append(ChatMessageAssistant(content=text, tool_calls=calls or None))
        elif item["role"] == "tool":
            messages.append(ChatMessageTool(content=text, tool_call_id=item.get("tool_call_id"),
                                            function=item.get("function")))
        else:
            messages.append(ChatMessageUser(content=text))
    memory: dict = {}
    warnings: list[str] = []
    tool_turns = 0
    for end in range(1, len(messages) + 1):
        if not isinstance(messages[end - 1], ChatMessageTool):
            continue
        tool_turns += 1
        detection = detect(extract_events(messages[:end]))
        if detection is None:
            continue
        state = memory.setdefault("state", {})
        if module.should_warn(detection, state, tool_turns):
            state["warnings"] = state.get("warnings", 0) + 1
            state.setdefault("last_warned", {})[f"{detection.kind}:{detection.key}"] = tool_turns
            warnings.append(detection.kind)
    assert tool_turns > 100
    assert len(warnings) <= MAX_WARNINGS
    print(f"baseline replay: {tool_turns} tool results, {len(warnings)} warning(s): {warnings}")
