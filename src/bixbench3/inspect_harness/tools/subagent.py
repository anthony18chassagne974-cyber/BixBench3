"""v4 dag-orchestrator: a `run_subagent` tool that starts a fresh agent on a self-contained brief.

The sub-agent is a new `react` agent with the same sandbox tools as the main agent
(its own bash session, python, text editor, web access request) and an empty context:
the brief is the only thing it knows. Its final answer is returned as the tool result.

Depth: the main agent is depth 0. A sub-agent at depth d gets `run_subagent` only while
d < MAX_DEPTH (orchestrator -> artifact builder -> brick worker), so recursion is bounded.

Counting (never touches /outputs, the grade or the judge):
  - Inspect sample store key `bixbench3_subagents` (saved in the .eval log);
  - $BIXBENCH3_BROKER_LOG_DIR/subagents.jsonl (one line per sub-agent start/end) and
    subagents_summary.json (running totals); broker_logs/ is uploaded with the run;
  - every sub-agent runs in an Inspect agent span named `subagent-<n>-<role>`, which the
    bixdash collector counts (`n_subagents` on the live dashboard).
A failure inside a sub-agent is returned to the caller as text; it never ends the sample.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from inspect_ai.agent import AgentSubmit, react, run
from inspect_ai.tool import Tool, ToolError, bash_session, python, text_editor, tool
from inspect_ai.util import message_limit, store

from .web_access import request_web_access

MAX_DEPTH = 2  # 0 = main agent, 1 = planner / artifact builder, 2 = brick worker (no further sub-agents)
MAX_SUBAGENTS = 200  # per sample, all depths
MESSAGE_LIMITS = {1: 800, 2: 300}  # per sub-agent, by depth
STORE_KEY = "bixbench3_subagents"

SUBAGENT_INSTRUCTIONS = """You are a sub-agent of a computational biology reproduction run.
You start with no memory of the main conversation: the brief below is everything you know.

Rules:
- Read ONLY the inputs the brief lists as allowed. Never open forbidden paths, other artifacts,
  or anything that looks like expected results, reference outputs or published values.
- Write ONLY inside the output folder the brief gives you (create it if needed). Never write to
  /outputs unless the brief explicitly tells you to.
- Save every script you run in your output folder, so the work can be audited and re-run.
- If something blocks you, try reasonable fixes; if you still cannot finish, stop and report
  exactly what failed, what you tried and what partial files exist.
- When done, call submit() with a short report: output paths, what you checked, any issues.
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "task"


def _log_dir() -> Path | None:
    value = os.environ.get("BIXBENCH3_BROKER_LOG_DIR")
    return Path(value) if value else None


def _record(event: dict, summary: dict) -> None:
    """Best effort: the counter files must never break the run."""
    directory = _log_dir()
    if directory is None:
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "subagents.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
        tmp = directory / ".subagents_summary.json.tmp"
        tmp.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(directory / "subagents_summary.json")
    except OSError:
        pass


def _summary(state: dict) -> dict:
    by_role: dict[str, int] = {}
    by_depth: dict[str, int] = {}
    for item in state["calls"]:
        by_role[item["role"]] = by_role.get(item["role"], 0) + 1
        by_depth[str(item["depth"])] = by_depth.get(str(item["depth"]), 0) + 1
    return {
        "total_subagents": state["count"],
        "finished": sum(1 for c in state["calls"] if c.get("status")),
        "failed": sum(1 for c in state["calls"] if c.get("status") not in (None, "ok")),
        "by_role": by_role,
        "by_depth": by_depth,
        "updated_at": _now(),
    }


def _sub_tools(depth: int, number: int) -> list[Tool]:
    tools: list[Tool] = [
        bash_session(timeout=600, instance=f"subagent-{number}"),  # own shell: no shared cwd/jobs
        python(timeout=600),
        text_editor(timeout=120),
        request_web_access(),
    ]
    if depth < MAX_DEPTH:
        tools.append(run_subagent(depth=depth + 1))
    return tools


@tool
def run_subagent(depth: int = 1) -> Tool:
    async def execute(role: str, brief: str) -> str:
        """Start a new sub-agent with an empty context and a self-contained brief.

        The sub-agent has its own shell plus python, the text editor and web access
        requests, in the same sandbox and filesystem as you. It knows NOTHING except
        the brief, so the brief must contain: the goal, the exact input paths it is
        allowed to read, what it must not read, the output folder to write to, and
        how to check success. It returns the sub-agent's final report.

        Args:
            role: Short role label, e.g. "planner", "artifact:deseq2", "brick:3-normalize".
            brief: The complete, self-contained instructions for the sub-agent.

        Returns:
            The sub-agent's final report (or an error description if it failed).
        """
        state = store().get(STORE_KEY) or {"count": 0, "calls": []}
        if state["count"] >= MAX_SUBAGENTS:
            raise ToolError(f"sub-agent limit reached ({MAX_SUBAGENTS}); finish the work yourself")
        state["count"] += 1
        number = state["count"]
        name = f"subagent-{number}-{_slug(role)}"
        call = {"n": number, "role": role, "depth": depth, "name": name, "started_at": _now(), "status": None}
        state["calls"].append(call)
        store().set(STORE_KEY, state)
        _record({"event": "start", **call, "brief_chars": len(brief)}, _summary(state))

        agent = react(
            name=name,
            description=f"sub-agent ({role})",
            prompt=SUBAGENT_INSTRUCTIONS,
            tools=_sub_tools(depth, number),
            submit=AgentSubmit(answer_only=True),
        )
        report = ""
        status = "ok"
        try:
            result, limit_error = await run(agent, brief, limits=[message_limit(MESSAGE_LIMITS.get(depth, 300))], name=name)
            report = (result.output.completion or "").strip()
            if limit_error is not None:
                status = "message_limit"
                report = (report + "\n\n" if report else "") + (
                    f"[sub-agent stopped: message limit {MESSAGE_LIMITS.get(depth, 300)} reached; "
                    "check its output folder for partial results]"
                )
            if not report:
                report = "[sub-agent ended without a report; check its output folder]"
        except Exception as error:  # report, never crash the caller
            status = "error"
            report = f"[sub-agent failed: {type(error).__name__}: {str(error)[:500]}]"

        state = store().get(STORE_KEY) or state
        for item in state["calls"]:
            if item["n"] == number:
                item.update({"status": status, "ended_at": _now(), "report_chars": len(report)})
        store().set(STORE_KEY, state)
        _record({"event": "end", "n": number, "role": role, "depth": depth, "name": name,
                 "status": status, "ended_at": _now()}, _summary(state))
        return report

    return execute
