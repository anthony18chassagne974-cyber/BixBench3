"""Experiment t9-env-reviewer: format reviewer gating every required artifact."""

from __future__ import annotations

import asyncio
import importlib.util
import math
from pathlib import Path
from typing import Any

import pytest
from inspect_ai import Task, eval
from inspect_ai.agent import AgentSubmit, react
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.tool import ToolError

from bixbench3.inspect_harness import artifact_review as review_module
from bixbench3.inspect_harness.artifact_review import (
    APPROVED_KEY,
    LOG_KEY,
    MAX_REJECTIONS,
    OUTPUTS_KEY,
    PROMPT_KEY,
    REVIEW_NOTICE,
    SUMMARY_SCRIPT_PATH,
    ArtifactReviewer,
    append_review_notice,
    artifact_spec,
    parse_verdict,
    review_setup,
    review_submit,
    reviewer_messages,
    submit_artifact,
)

PROMPT = """# Analysis Task

## Required Outputs

### `counts`

- Path: `outputs/counts.tsv`
- Format: `tsv`
- Row key: `gene_id`
- Required columns: `gene_id`, `S1`, `S2`

### `degs`

- Path: `outputs/degs.csv`
- Format: `csv`
- Row key: `gene_id`
"""

OUTPUTS = [
    {"artifact_id": "counts", "path": "outputs/counts.tsv", "format": "tsv", "row_key": "gene_id"},
    {"artifact_id": "degs", "path": "outputs/degs.csv", "format": "csv", "row_key": "gene_id"},
]


def _summary_module() -> Any:
    spec = importlib.util.spec_from_file_location("artifact_summary", SUMMARY_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reviewer(answers: list[str], *, files: dict[str, str] | None = None, fail: bool = False) -> tuple[ArtifactReviewer, dict, dict, list]:
    state: dict = {PROMPT_KEY: PROMPT, OUTPUTS_KEY: OUTPUTS}
    files = {"/workspace/work/counts.tsv": "h1"} if files is None else files
    logs: list = []
    queue = list(answers)

    async def summarize(path: str, artifact: dict, seed: int) -> str:
        if fail:
            raise RuntimeError("sandbox gone")
        return f"summary of {path}"

    async def ask_model(messages) -> str:
        return queue.pop(0)

    async def copy(source: str, destination: str) -> str:
        files[destination] = files[source]
        return files[source]

    async def file_hash(path: str) -> str | None:
        return files.get(path)

    async def append_log(entry: str) -> None:
        logs.append(entry)

    reviewer = ArtifactReviewer(
        state=state, summarize=summarize, ask_model=ask_model,
        copy=copy, file_hash=file_hash, append_log=append_log,
    )
    return reviewer, state, files, logs


def test_notice_and_spec_extraction() -> None:
    prompt = append_review_notice(PROMPT)
    assert prompt.count(REVIEW_NOTICE) == 1 and "submit_artifact(artifact_id, path, note)" in prompt
    spec = artifact_spec(PROMPT, "counts")
    assert spec.startswith("### `counts`") and "`gene_id`, `S1`, `S2`" in spec and "degs" not in spec
    assert "outputs/degs.csv" in artifact_spec(PROMPT, "degs")
    assert artifact_spec(PROMPT, "missing") == ""


def test_reviewer_sees_react_prompt_task_spec_summary_and_history_only() -> None:
    messages = reviewer_messages(
        task_prompt=PROMPT, artifact=OUTPUTS[0], summary="Shape: 3 data rows x 3 columns",
        note="S2 is the second replicate", history=[{"attempt": 1, "verdict": "rejected", "feedback": "rename S_2"}],
    )
    system, user = (message.text for message in messages)
    assert "no reference answers" in system and "never guess them" in system
    for text in ("helpful assistant", "call the submit() tool", "# Task description", "### `counts`",
                 "Shape: 3 data rows", "rename S_2", "S2 is the second replicate"):
        assert text in user
    assert parse_verdict("VERDICT: ACCEPT") and parse_verdict("**VERDICT: ACCEPT**\n")
    assert not parse_verdict("VERDICT: REVISE\n- rename") and not parse_verdict("")


def test_two_rejections_then_third_submission_is_accepted_and_copied() -> None:
    reviewer, state, files, logs = _reviewer(["VERDICT: REVISE\n- column S_2 should be S2"] * 2)
    first = asyncio.run(reviewer.submit("counts", "work/counts.tsv"))
    assert "not accepted (submission 1)" in first and "column S_2" in first
    assert "/outputs/counts.tsv" not in files
    asyncio.run(reviewer.submit("counts", "./work/counts.tsv", note="S_2 is the requested name"))
    third = asyncio.run(reviewer.submit("counts", "/workspace/work/counts.tsv"))
    assert "accepted without review (submission 3)" in third
    assert files["/outputs/counts.tsv"] == "h1" and state[APPROVED_KEY] == {"counts": "h1"}
    assert [entry["verdict"] for entry in state[LOG_KEY]] == ["rejected", "rejected", "accepted without review"]
    assert state[LOG_KEY][1]["note"] == "S_2 is the requested name"
    assert logs[0].startswith(f"## counts (submission 1/{MAX_REJECTIONS + 1}): rejected")


def test_accept_copies_and_reviewer_failure_never_blocks() -> None:
    reviewer, _, files, _ = _reviewer(["VERDICT: ACCEPT"])
    assert "accepted (submission 1)" in asyncio.run(reviewer.submit("counts", "work/counts.tsv"))
    assert files["/outputs/counts.tsv"] == "h1"
    reviewer, _, files, _ = _reviewer([], fail=True)
    assert "reviewer unavailable (RuntimeError)" in asyncio.run(reviewer.submit("counts", "work/counts.tsv"))
    assert files["/outputs/counts.tsv"] == "h1"


def test_unknown_artifact_and_missing_file_are_tool_errors() -> None:
    reviewer, state, _, _ = _reviewer([])
    with pytest.raises(ToolError, match="Valid IDs: counts, degs"):
        asyncio.run(reviewer.submit("count", "work/counts.tsv"))
    with pytest.raises(ToolError, match="File not found"):
        asyncio.run(reviewer.submit("degs", "work/degs.csv"))
    assert LOG_KEY not in state  # errors do not use up a submission


def test_final_issues_flag_missing_ungated_and_modified_artifacts() -> None:
    reviewer, _, files, _ = _reviewer(["VERDICT: ACCEPT"])
    asyncio.run(reviewer.submit("counts", "work/counts.tsv"))
    assert asyncio.run(reviewer.final_issues()) == ["- degs: missing"]
    files["/outputs/degs.csv"] = "direct"
    files["/outputs/counts.tsv"] = "edited"
    assert asyncio.run(reviewer.final_issues()) == [
        "- counts: changed in ./outputs after it was accepted",
        "- degs: present in ./outputs but never submitted",
    ]


def test_summary_reports_shape_keys_issues_and_random_sample(tmp_path: Path) -> None:
    summary = _summary_module()
    assert summary.sample_size(60_000, 5) == math.ceil(math.sqrt(60_000) / 10) == 25
    assert summary.sample_size(17, 3) == 3 and summary.sample_size(2, 3) == 2
    table = tmp_path / "counts.tsv"
    table.write_text("gene_id\tS1\tS2\n" + "".join(f"ENSG{i:011d}.1\t{i}\t{2 * i}\n" for i in range(40)) + "ENSG00000000001.1\t1\n")
    text = summary.summarize(table, "tsv", "gene_id", seed=7)
    assert "Shape: 41 data rows x 3 columns" in text and "delimiter detected in the header: tab" in text
    assert "Header: gene_id | S1 | S2" in text
    assert "40 distinct of 41, 0 empty, 1 repeated (e.g. ENSG00000000001.1)" in text
    assert "1 data rows do not have 3 fields" in text
    assert "Random row identifiers (5):" in text and "Random columns (3) with example values:" in text
    assert text == summary.summarize(table, "tsv", "gene_id", seed=7)
    wrong = tmp_path / "degs.csv"
    wrong.write_text("symbol,gene_id\nA,ENSG1\n")
    text = summary.summarize(wrong, "csv", "gene_id", seed=0)
    assert "row key column `gene_id` is column 2, not the first" in text
    assert "File not found" in summary.summarize(tmp_path / "nope.csv", "csv", None, seed=0)


def test_react_loop_gates_artifacts_and_final_submit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, outputs = tmp_path / "workspace", tmp_path / "outputs"
    (workspace / "work").mkdir(parents=True)
    outputs.mkdir()
    (workspace / "work" / "counts.tsv").write_text("gene_id\tS1\tS2\nENSG1.1\t1\t2\n")
    (tmp_path / "prompt.md").write_text(PROMPT)
    monkeypatch.setattr(review_module, "SANDBOX_WORKSPACE", str(workspace))
    monkeypatch.setattr(review_module, "SANDBOX_WORK", str(workspace / "work"))
    monkeypatch.setattr(review_module, "SANDBOX_OUTPUTS", str(outputs))
    monkeypatch.setattr(review_module, "SANDBOX_SUMMARY_PATH", str(tmp_path / "summary.py"))
    # One queue serves the agent and the reviewer, in call order.
    model = get_model("mockllm/model", custom_outputs=[
        ModelOutput.for_tool_call("mockllm/model", "submit_artifact", {"artifact_id": "counts", "path": "work/counts.tsv"}),
        # dual review: the local and the consistency reviewer each take one output per submission
        ModelOutput.from_content("mockllm/model", "VERDICT: REVISE\n- add a third sample"),
        ModelOutput.from_content("mockllm/model", "VERDICT: REVISE\n- add a third sample\nNOTES: none"),
        ModelOutput.for_tool_call("mockllm/model", "submit_artifact", {"artifact_id": "counts", "path": "work/counts.tsv", "note": "two samples are specified"}),
        ModelOutput.from_content("mockllm/model", "VERDICT: ACCEPT"),
        ModelOutput.from_content("mockllm/model", "VERDICT: ACCEPT\nNOTES:\n- count tables use versioned ENSG ids"),
        ModelOutput.for_tool_call("mockllm/model", "submit", {"answer": "done"}),
        ModelOutput.for_tool_call("mockllm/model", "submit", {"answer": "done"}),
    ])
    task = Task(
        dataset=[Sample(input=PROMPT, metadata={
            "task_id": "case", "prompt": {"prompt_path": "prompt.md"}, "expected_outputs": OUTPUTS,
        })],
        setup=review_setup(),
        solver=react(tools=[submit_artifact()], submit=AgentSubmit(tool=review_submit())),
        sandbox="local",
    )
    log = eval(task, model=model, display="none", log_dir=str(tmp_path / "logs"))[0]
    assert log.status == "success"
    sample = log.samples[0]
    assert sample.output.completion.endswith("done")
    # react strips submit calls from the final transcript; the tool events keep what the agent saw.
    tools = [event for event in sample.events if event.event == "tool"]
    assert [event.function for event in tools] == ["submit_artifact", "submit_artifact", "submit", "submit"]
    assert "not accepted (submission 1)" in tools[0].result and "add a third sample" in tools[0].result
    assert "Local reviewer (this table only)" in tools[0].result and "Consistency reviewer (run memory)" in tools[0].result
    assert "accepted (submission 2)" in tools[1].result
    assert tools[2].error is not None and "- degs: missing" in tools[2].error.message
    assert tools[3].error is None and tools[3].result == "done"
    assert (outputs / "counts.tsv").read_text() == (workspace / "work" / "counts.tsv").read_text()
    assert [entry["verdict"] for entry in sample.store[LOG_KEY]] == ["rejected", "accepted"]
    assert "## Artifact Review" in next(message.text for message in sample.messages if message.role == "user")
    review_log = (workspace / "work" / "artifact_review.md").read_text()
    assert "## counts (submission 1/3): rejected" in review_log and "Agent note: two samples are specified" in review_log
