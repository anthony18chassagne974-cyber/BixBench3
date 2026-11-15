"""Experiment v2d: input / sample manifest auditor."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bixbench3.inspect_harness import manifest_auditor as module
from bixbench3.inspect_harness.manifest_auditor import (
    AUDIT_NOTICE,
    AUDIT_SCRIPT_PATH,
    append_audit_notice,
)

PROMPT = (
    "Start from the 6 staged single-end FASTQs. Required sample columns are: "
    "`L1`, `L2`, `L3`, `L4`, `L5`, `L6`.\n"
)


def _auditor():
    spec = importlib.util.spec_from_file_location("input_manifest_audit", AUDIT_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def _stage(root: Path, *, runs: range = range(1, 7), samples: range = range(1, 7)) -> Path:
    data = root / "data"
    (data / "raw" / "fastq").mkdir(parents=True)
    (data / "metadata").mkdir()
    for run in runs:
        (data / "raw" / "fastq" / f"SRR{run}.fastq.gz").write_bytes(b"x" * 10)
    dose = {1: "0ppm", 2: "0ppm", 3: "40ppm", 4: "40ppm", 5: "0ppm", 6: "40ppm"}
    time = {1: "day7", 2: "day7", 3: "day7", 4: "day7", 5: "day28", 6: "day28"}
    sample_lines = ["library_id\tgeo_accession\ttreatment_ppm\ttimepoint\ttissue"]
    sample_lines += [f"L{i}\tGSM{i}\t{dose[i]}\t{time[i]}\tliver" for i in samples]
    (data / "metadata" / "sample_metadata.tsv").write_text("\n".join(sample_lines) + "\n")
    run_lines = ["run_accession\tsample_alias\tsample_title"]
    run_lines += [f"SRR{i}\tGSM{i}\tL{i}" for i in range(1, 7)]
    (data / "metadata" / "ena_run_metadata.tsv").write_text("\n".join(run_lines) + "\n")
    return data


def test_consistent_inputs_are_linked_grouped_and_contrasted(tmp_path: Path) -> None:
    report, rows, summary = _auditor().audit(_stage(tmp_path), PROMPT)
    assert summary["samples"] == 6 and summary["raw_files"] == 6 and summary["linked_files"] == 6
    assert summary["sample_id_column"] == "library_id"
    assert summary["groups"] == 4 and summary["contrasts"] == 2
    assert "6 of 6 raw files map to a sample; 6 of 6 samples have at least one raw file." in report
    assert "| 0ppm | day7 | 2 |" in report and "| 40ppm | day28 | 1 |" in report
    assert "| day28 | 40ppm | 1 | 0ppm | 1 |" in report
    assert "`tissue` = liver" in report
    assert "The prompt lists 6 identifiers that match `library_id`." in report
    assert rows[0] == ["library_id", "geo_accession", "treatment_ppm", "timepoint", "raw_files"]
    assert rows[1] == ["L1", "GSM1", "0ppm", "day7", "data/raw/fastq/SRR1.fastq.gz"]
    assert summary["flags"] == [
        "group(s) with fewer than 3 replicates: `0ppm / day7`, `0ppm / day28`, `40ppm / day7`, `40ppm / day28`"
    ]


def test_missing_extra_and_unlisted_samples_are_flagged(tmp_path: Path) -> None:
    data = _stage(tmp_path, runs=range(1, 6))
    (data / "raw" / "fastq" / "SRR99.fastq.gz").write_bytes(b"")
    prompt = PROMPT.replace("`L6`", "`L7`")
    report, _, summary = _auditor().audit(data, prompt)
    flags = "\n".join(summary["flags"])
    assert "1 empty raw file(s): `SRR99.fastq.gz`" in flags
    assert "1 raw file(s) not linked to any sample: `SRR99.fastq.gz`" in flags
    assert "1 sample(s) in the metadata without a raw file: `L6`" in flags
    assert "1 sample(s) in the metadata are not listed in the prompt: `L6`" in flags
    assert "identifier(s) listed in the prompt but absent from the metadata: `L7`" in flags
    assert "## Flags" in report


def test_auditor_never_reads_outside_the_data_directory_or_needs_a_sample_table(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    report, rows, summary = _auditor().audit(tmp_path / "data", "")
    assert rows == []
    assert summary["flags"] == ["no raw sequencing files found", "no sample table could be identified"]
    assert report.startswith("# Input manifest audit")
    source = AUDIT_SCRIPT_PATH.read_text(encoding="utf-8")
    assert "ground" not in source.lower() and "outputs/" not in source


def test_script_runs_standalone_and_writes_report_and_sample_table(tmp_path: Path) -> None:
    data = _stage(tmp_path)
    prompt = tmp_path / "prompt.md"
    prompt.write_text(PROMPT)
    report = tmp_path / "work" / "input_manifest_audit.md"
    samples = tmp_path / "work" / "input_manifest_audit_samples.tsv"
    result = subprocess.run(
        (sys.executable, str(AUDIT_SCRIPT_PATH), "--data", str(data), "--prompt", str(prompt),
         "--report", str(report), "--samples-table", str(samples)),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])["samples_table"] == samples.name
    assert "`./work/input_manifest_audit_samples.tsv`" in report.read_text(encoding="utf-8")
    assert samples.read_text(encoding="utf-8").splitlines()[0].split("\t")[0] == "library_id"


def test_notice_points_to_the_report_and_defers_to_method_guidance() -> None:
    prompt = append_audit_notice("# Analysis Task\n\nBody.\n")
    assert prompt.startswith("# Analysis Task\n\nBody.\n\n## Input Manifest Audit")
    assert prompt.count(AUDIT_NOTICE) == 1
    assert "./work/input_manifest_audit.md" in prompt
    assert "Method Guidance takes precedence" in prompt


class _LocalSandbox:
    def __init__(self, root: Path, data: Path, *, fail: bool = False) -> None:
        self.root, self.data, self.fail = root, data, fail

    def _local(self, path: str) -> str:
        return str(self.data) if path == "/data" else str(self.root / path.lstrip("/"))

    async def write_file(self, file: str, contents: str) -> None:
        target = Path(self._local(file))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8", newline="\n")

    async def exec(self, cmd: list[str], timeout: int | None = None) -> SimpleNamespace:
        del timeout
        if self.fail:
            return SimpleNamespace(success=False, stdout="", stderr="python3: boom")
        argv = [sys.executable, *(self._local(part) if part.startswith("/") else part for part in cmd[1:])]
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        return SimpleNamespace(success=done.returncode == 0, stdout=done.stdout, stderr=done.stderr)


@pytest.mark.parametrize("fail", [False, True])
def test_solver_writes_report_and_extends_prompt_even_when_audit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail: bool,
) -> None:
    from inspect_ai.model import ChatMessageUser, ModelName
    from inspect_ai.solver import TaskState

    fake = _LocalSandbox(tmp_path / "sandbox", _stage(tmp_path), fail=fail)
    monkeypatch.setattr(module, "sandbox", lambda: fake)
    state = TaskState(
        model=ModelName("mockllm/model"), sample_id="case", epoch=1, input="prompt",
        messages=[ChatMessageUser(content=PROMPT)],
    )
    state = asyncio.run(module.manifest_auditor()(state, None))
    report = (tmp_path / "sandbox" / "workspace" / "work" / "input_manifest_audit.md").read_text(encoding="utf-8")
    assert report.startswith("# Input manifest audit")
    assert ("auditor itself failed" in report) is fail
    assert state.user_prompt.text.endswith(AUDIT_NOTICE)
    assert state.store.get("manifest_auditor")["status"] == ("audit_failed" if fail else "ok")
    if not fail:
        staged_prompt = (tmp_path / "sandbox" / "tmp" / "bixbench3_input_manifest_audit_prompt.md").read_text()
        assert staged_prompt == PROMPT  # the auditor sees the original prompt, without the notice


def test_task_setup_runs_workspace_preparation_then_auditor() -> None:
    source = (Path(__file__).resolve().parents[2] / "src/bixbench3/inspect_harness/tasks.py").read_text()
    assert "setup=[prepare_case_workspace(), manifest_auditor()]," in source
