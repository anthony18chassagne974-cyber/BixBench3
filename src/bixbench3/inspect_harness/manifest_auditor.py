"""Experiment v2d: input / sample manifest auditor stage.

Before the analyst starts, inventory the staged raw files and metadata tables inside
the sandbox, write ``work/input_manifest_audit.md`` plus a per-sample table, and tell
the agent to read the report before analysis.

The auditor uses only the staged inputs under ``/data`` and the task prompt. It uses no
grader or reference output and does not prescribe or change any scientific method.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.util import sandbox

AUDIT_SCRIPT_PATH = Path(__file__).parent / "sandbox_scripts" / "input_manifest_audit.py"

SANDBOX_SCRIPT_PATH = "/tmp/bixbench3_input_manifest_audit.py"
SANDBOX_PROMPT_PATH = "/tmp/bixbench3_input_manifest_audit_prompt.md"
SANDBOX_DATA_PATH = "/data"
SANDBOX_REPORT_PATH = "/workspace/work/input_manifest_audit.md"
SANDBOX_SAMPLES_PATH = "/workspace/work/input_manifest_audit_samples.tsv"
AUDIT_TIMEOUT_SECONDS = 600

AUDIT_NOTICE = """## Input Manifest Audit

Before this task started, the harness inventoried the staged inputs under `./data`: raw \
files, metadata tables, sample identifiers, the link between raw files and samples, \
treatment/timepoint groups, replicate counts, and any missing, extra or duplicate sample. \
The report is at `./work/input_manifest_audit.md`. Read it before you begin the analysis.

Use it to make sure every expected sample is processed exactly once and that your design \
matches the real groups and replicate counts. Resolve every item listed under "Flags" \
before analysis. The audit is heuristic and derived only from the staged files and this \
prompt; where it disagrees with "Method Guidance", Method Guidance takes precedence.
"""


def append_audit_notice(prompt: str) -> str:
    return prompt.rstrip() + "\n\n" + AUDIT_NOTICE


def failure_report(detail: str) -> str:
    return (
        "# Input manifest audit\n\n"
        "The auditor itself failed, so no inventory is available.\n"
        "Inventory the raw files, sample metadata, groups and replicate counts yourself before analysis.\n\n"
        f"Auditor error:\n\n```\n{detail.strip()[-1500:]}\n```\n"
    )


@solver
def manifest_auditor() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        del generate
        environment = sandbox()
        original_prompt = state.user_prompt.text
        await environment.write_file(SANDBOX_SCRIPT_PATH, AUDIT_SCRIPT_PATH.read_text(encoding="utf-8"))
        await environment.write_file(SANDBOX_PROMPT_PATH, original_prompt)
        summary: dict[str, Any]
        try:
            result = await environment.exec(
                [
                    "python3", SANDBOX_SCRIPT_PATH,
                    "--data", SANDBOX_DATA_PATH,
                    "--prompt", SANDBOX_PROMPT_PATH,
                    "--report", SANDBOX_REPORT_PATH,
                    "--samples-table", SANDBOX_SAMPLES_PATH,
                ],
                timeout=AUDIT_TIMEOUT_SECONDS,
            )
            if result.success:
                summary = {"status": "ok", **json.loads(result.stdout.strip().splitlines()[-1])}
            else:
                summary = {"status": "audit_failed", "detail": (result.stderr or result.stdout)[-1500:]}
        except Exception as error:  # a broken auditor must not cost the whole run
            summary = {"status": "audit_failed", "detail": f"{type(error).__name__}: {error}"}
        if summary["status"] != "ok":
            await environment.write_file(SANDBOX_REPORT_PATH, failure_report(str(summary.get("detail", ""))))
        state.store.set("manifest_auditor", summary)
        state.user_prompt.text = append_audit_notice(original_prompt)
        return state

    return solve
