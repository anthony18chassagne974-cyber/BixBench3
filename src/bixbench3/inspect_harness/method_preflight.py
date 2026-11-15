"""Experiment v2c: required-method preflight stage.

Before the analyst starts, probe the sandbox for the tools, versions and resources
named in the task's method guidance, write ``work/preflight_methods.md`` and tell the
agent to read it and to repair missing methods before substituting them.

The stage only observes. It does not install packages, does not spend the agent's
``request_web_access`` budget, and does not touch scientific analysis code. Tasks
without a requirement spec are left exactly as in the baseline harness.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.util import sandbox

_HERE = Path(__file__).parent
PROBE_SCRIPT_PATH = _HERE / "sandbox_scripts" / "method_preflight_probe.py"
SPEC_DIRECTORY = _HERE / "preflight_specs"

SANDBOX_PROBE_PATH = "/tmp/bixbench3_method_preflight_probe.py"
SANDBOX_SPEC_PATH = "/tmp/bixbench3_method_preflight_spec.json"
SANDBOX_REPORT_PATH = "/workspace/work/preflight_methods.md"
PROBE_TIMEOUT_SECONDS = 600

PREFLIGHT_NOTICE = """## Method Preflight

Before this task started, the harness checked whether the tools, versions and external \
resources named under "Method Guidance" are available in the sandbox. The report is at \
`./work/preflight_methods.md`. Read it before you begin the analysis.

For every requirement the report does not mark `OK`, repair it first: install the named \
tool and version, or obtain access to the named resource. Do not silently substitute a \
different tool, version, database or mapping. Use a fallback only after a real repair \
attempt has failed, and document the diagnosis (what you tried, the exact error, why the \
fallback is the closest equivalent) in `./outputs/METHODS.md`.
"""


def load_preflight_spec(sample_id: str) -> dict[str, Any] | None:
    """Return the hand-written requirement spec for one case, if it has one."""
    path = SPEC_DIRECTORY / f"{sample_id}.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"preflight spec must be a JSON object: {path.name}")
    return value


def append_preflight_notice(prompt: str) -> str:
    return prompt.rstrip() + "\n\n" + PREFLIGHT_NOTICE


def failure_report(detail: str) -> str:
    return (
        "# Required-method preflight\n\n"
        "The preflight probe itself failed, so no availability information is available.\n"
        "Check each tool and resource named under Method Guidance yourself before relying on it.\n\n"
        f"Probe error:\n\n```\n{detail.strip()[-1500:]}\n```\n"
    )


@solver
def method_preflight() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        del generate
        spec = load_preflight_spec(str(state.sample_id))
        if spec is None:
            return state
        environment = sandbox()
        await environment.write_file(SANDBOX_PROBE_PATH, PROBE_SCRIPT_PATH.read_text(encoding="utf-8"))
        await environment.write_file(SANDBOX_SPEC_PATH, json.dumps(spec))
        summary: dict[str, Any]
        try:
            result = await environment.exec(
                ["python3", SANDBOX_PROBE_PATH, "--spec", SANDBOX_SPEC_PATH, "--report", SANDBOX_REPORT_PATH],
                timeout=PROBE_TIMEOUT_SECONDS,
            )
            if result.success:
                summary = {"status": "ok", **json.loads(result.stdout.strip().splitlines()[-1])}
            else:
                summary = {"status": "probe_failed", "detail": (result.stderr or result.stdout)[-1500:]}
        except Exception as error:  # a broken probe must not cost the whole run
            summary = {"status": "probe_failed", "detail": f"{type(error).__name__}: {error}"}
        if summary["status"] != "ok":
            await environment.write_file(SANDBOX_REPORT_PATH, failure_report(str(summary.get("detail", ""))))
        state.store.set("method_preflight", summary)
        state.user_prompt.text = append_preflight_notice(state.user_prompt.text)
        return state

    return solve
