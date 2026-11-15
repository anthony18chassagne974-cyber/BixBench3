"""Experiment v2b: final artifact contract validator.

Adds a deterministic, structure-only check of the required output files:

* a ``validate_outputs`` tool the agent can call at any time;
* a ``submit`` tool that runs the same check and refuses the submission, with an
  actionable error list, while *severe* structural problems remain (bounded number of
  refusals, so a run can never be trapped);
* a setup stage that stages the checker and tells the agent about both.

The contract is read from the task prompt and the case's expected-output list. No
grader output or reference artifact is used, and no analysis method is changed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import Tool, ToolError, ToolResult, tool
from inspect_ai.util import sandbox, store

from .models import RuntimeCase

CHECK_SCRIPT_PATH = Path(__file__).parent / "sandbox_scripts" / "artifact_contract_check.py"

SANDBOX_SCRIPT_PATH = "/tmp/bixbench3_artifact_contract_check.py"
SANDBOX_CONTRACT_PATH = "/tmp/bixbench3_artifact_contract.json"
SANDBOX_WORKSPACE = "/workspace"
SANDBOX_REPORT_PATH = "/workspace/work/artifact_validation.md"
SANDBOX_EVENTS_PATH = "/workspace/work/artifact_validation_events.jsonl"
CHECK_TIMEOUT_SECONDS = 300
MAX_SUBMIT_REFUSALS = 3
REFUSALS_KEY = "artifact_validator_submit_refusals"
MAX_LISTED_PROBLEMS = 25

VALIDATOR_NOTICE = """## Output Validation

A structural validator for the required outputs is available as the `validate_outputs` \
tool. It checks only the output contract stated in this prompt (files present and \
non-empty, tables parse, required columns, unique non-empty row keys, numeric columns, \
probability columns within [0, 1], placeholder values). It does not judge scientific \
correctness. Call it once your outputs are written and fix what it reports.

`submit` runs the same check. While severe structural problems remain it returns an \
error listing them instead of ending the task, at most 3 times: fix them, then submit again.
"""


def append_validator_notice(prompt: str) -> str:
    return prompt.rstrip() + "\n\n" + VALIDATOR_NOTICE


def build_contract(case: RuntimeCase, prompt: str) -> dict[str, Any]:
    return {
        "prompt": prompt,
        "expected_outputs": [
            {"artifact_id": output.artifact_id, "path": output.path, "format": output.format, "row_key": output.row_key}
            for output in case.expected_outputs
        ],
    }


def format_problems(title: str, problems: list[str]) -> str:
    lines = [title] + [f"- {problem}" for problem in problems[:MAX_LISTED_PROBLEMS]]
    if len(problems) > MAX_LISTED_PROBLEMS:
        lines.append(f"- ... and {len(problems) - MAX_LISTED_PROBLEMS} more (see ./work/artifact_validation.md)")
    return "\n".join(lines)


def format_outcome(outcome: dict[str, Any]) -> str:
    severe, warnings = outcome["severe"], outcome["warnings"]
    if not severe and not warnings:
        return "Output contract check passed: no structural problem found. Full report: ./work/artifact_validation.md"
    parts = []
    if severe:
        parts.append(format_problems(f"{len(severe)} severe structural problem(s):", severe))
    if warnings:
        parts.append(format_problems(f"{len(warnings)} warning(s):", warnings))
    parts.append("Full report: ./work/artifact_validation.md")
    return "\n\n".join(parts)


async def run_contract_check(trigger: str) -> dict[str, Any] | None:
    """Run the checker in the sandbox. Returns None if the checker itself failed."""
    environment = sandbox()
    try:
        result = await environment.exec(
            ["python3", SANDBOX_SCRIPT_PATH, "--contract", SANDBOX_CONTRACT_PATH,
             "--workspace", SANDBOX_WORKSPACE, "--report", SANDBOX_REPORT_PATH],
            timeout=CHECK_TIMEOUT_SECONDS,
        )
        if not result.success:
            return None
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        event = json.dumps({"trigger": trigger, "severe": len(outcome["severe"]), "warnings": len(outcome["warnings"]),
                            "severe_messages": outcome["severe"][:MAX_LISTED_PROBLEMS]})
        await environment.exec(["sh", "-c", f"cat >> {SANDBOX_EVENTS_PATH}"], input=event + "\n")
    except Exception:  # a broken validator must never block or fail a run
        return None
    return outcome


@tool
def validate_outputs() -> Tool:
    async def execute() -> ToolResult:
        """Check the structure of the required output files against the task's output contract.

        Verifies that every required file exists and is non-empty, that tables parse,
        that required columns are present, that row keys are non-empty and unique, that
        numeric columns parse and probability columns lie in [0, 1]. It does not assess
        scientific correctness and does not modify any file.

        Returns:
            A list of severe structural problems and warnings, or a pass message.
        """
        outcome = await run_contract_check("validate_outputs")
        if outcome is None:
            return "The output validator could not run; check the required outputs yourself."
        return format_outcome(outcome)

    return execute


@tool(name="submit")
def validated_submit() -> Tool:
    async def execute(answer: str) -> ToolResult:
        """Submit an answer for evaluation.

        Args:
          answer (str): Submitted answer
        """
        outcome = await run_contract_check("submit")
        if outcome is None or not outcome["severe"]:
            return answer
        refusals = int(store().get(REFUSALS_KEY, 0))
        if refusals >= MAX_SUBMIT_REFUSALS:
            return answer
        store().set(REFUSALS_KEY, refusals + 1)
        remaining = MAX_SUBMIT_REFUSALS - refusals - 1
        raise ToolError(
            "Submission not accepted yet: the required outputs have severe structural problems.\n\n"
            + format_outcome(outcome)
            + f"\n\nFix these and call submit again. ({remaining} more refusal(s) possible before "
            "submit is accepted regardless.)"
        )

    return execute


@solver
def prepare_artifact_validator() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        del generate
        environment = sandbox()
        prompt = state.user_prompt.text
        contract = build_contract(state.metadata_as(RuntimeCase), prompt)
        await environment.write_file(SANDBOX_SCRIPT_PATH, CHECK_SCRIPT_PATH.read_text(encoding="utf-8"))
        await environment.write_file(SANDBOX_CONTRACT_PATH, json.dumps(contract))
        state.user_prompt.text = append_validator_notice(prompt)
        return state

    return solve
