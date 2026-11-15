"""Experiment v2c: required-method preflight."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from bixbench3.inspect_harness.method_preflight import (
    PREFLIGHT_NOTICE,
    PROBE_SCRIPT_PATH,
    SPEC_DIRECTORY,
    append_preflight_notice,
    failure_report,
    load_preflight_spec,
)

TASK_11 = "10.1016_j.crtox.2021.01.003_v1"


def _probe_module():
    spec = importlib.util.spec_from_file_location("method_preflight_probe", PROBE_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_task_11_spec_names_only_method_guidance_requirements() -> None:
    spec = load_preflight_spec(TASK_11)
    assert spec is not None
    versions = {package["name"]: package["required_version"] for package in spec["r_packages"]}
    assert versions == {
        "QuasR": "1.26.0", "DESeq2": "1.28.1", "biomaRt": "2.44.1", "piano": "2.4.0", "fgsea": None,
    }
    assert any("apr2020.archive.ensembl.org/biomart/martservice" in entry["url"] for entry in spec["network"])
    assert any("msigdb/release/6.2" in entry["url"] for entry in spec["network"])
    text = json.dumps(spec).lower()
    assert "bixbench3-ground-truth" not in text
    assert "grader" in spec["source"].lower()  # the provenance note disclaims grader use


def test_cases_without_a_spec_are_left_untouched() -> None:
    assert load_preflight_spec("some-other-case") is None
    assert [path.name for path in SPEC_DIRECTORY.glob("*.json")] == [f"{TASK_11}.json"]


def test_notice_is_appended_once_and_demands_repair_before_substitution() -> None:
    prompt = append_preflight_notice("# Analysis Task\n\nBody.\n")
    assert prompt.startswith("# Analysis Task\n\nBody.\n\n## Method Preflight")
    assert prompt.count(PREFLIGHT_NOTICE) == 1
    assert "./work/preflight_methods.md" in prompt
    assert "Do not silently substitute" in prompt
    assert "METHODS.md" in prompt


def test_probe_reports_missing_wrong_version_blocked_and_staged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _probe_module()

    def fake_run(argv: list[str], timeout: int) -> tuple[int, str]:
        del timeout
        assert argv[0] == "Rscript"
        return 0, "R_VERSION 4.2.2 \nBIOC_VERSION 3.16 \nPKG QuasR NA \nPKG DESeq2 1.38.3 \nPKG piano 2.4.0 \n"

    def fake_url(url: str) -> tuple[str, str]:
        return (probe.BLOCKED, "proxy refused") if "ensembl" in url else (probe.OK, "HTTP 206")

    monkeypatch.setattr(probe, "run", fake_run)
    monkeypatch.setattr(probe, "probe_url", fake_url)
    monkeypatch.setattr(probe.shutil, "which", lambda name: f"/usr/bin/{name}")
    fastq = tmp_path / "data" / "fastq"
    fastq.mkdir(parents=True)
    for index in range(3):
        (fastq / f"S{index}.fastq.gz").touch()
    spec = {
        "r_runtime": {"required": "R 4.0.2-compatible"},
        "r_packages": [
            {"name": "QuasR", "required_version": "1.26.0", "purpose": "counting"},
            {"name": "DESeq2", "required_version": "1.28.1", "purpose": "contrasts"},
            {"name": "piano", "required_version": "2.4.0", "purpose": "gsea"},
        ],
        "network": [
            {"name": "BioMart", "url": "https://apr2020.archive.ensembl.org/biomart/martservice", "purpose": "orthologs"},
            {"name": "MSigDB", "url": "https://data.broadinstitute.org/x.gmt", "purpose": "gene sets"},
        ],
        "staged_inputs": [
            {"path": "data/fastq", "glob": "*.fastq.gz", "expected_count": 3, "purpose": "reads"},
            {"path": "data/missing.tsv", "purpose": "metadata"},
        ],
    }
    runtime, packages = probe.probe_r(spec["r_packages"])
    assert runtime["r_version"] == "4.2.2" and runtime["bioconductor_version"] == "3.16"
    assert [row["status"] for row in packages] == [probe.MISSING, probe.VERSION_MISMATCH, probe.OK]
    report, summary = probe.render(
        spec, runtime, packages, [], probe.probe_network(spec["network"]),
        probe.probe_staged(spec["staged_inputs"], tmp_path),
    )
    assert "| `QuasR` | counting | 1.26.0 | - | **MISSING** |" in report
    assert "| `DESeq2` | contrasts | 1.28.1 | 1.38.3 | **VERSION MISMATCH** |" in report
    assert "**BLOCKED BY PROXY**" in report and "request_web_access" in report
    assert "Do not silently substitute" in report
    assert summary["problems"] == [
        "R package QuasR (MISSING)",
        "R package DESeq2 (VERSION MISMATCH)",
        "resource BioMart (BLOCKED BY PROXY)",
        "staged input data/missing.tsv (MISSING)",
    ]


def test_probe_script_runs_standalone_and_never_fails_without_r(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "r_runtime": {"required": "any"},
        "r_packages": [{"name": "DefinitelyNotInstalledPkg", "required_version": "1.0", "purpose": "test"}],
        "network": [],
        "staged_inputs": [{"path": "nothing-here", "purpose": "test"}],
    }))
    report_path = tmp_path / "work" / "preflight_methods.md"
    result = subprocess.run(
        (sys.executable, str(PROBE_SCRIPT_PATH), "--spec", str(spec_path), "--report", str(report_path),
         "--workspace", str(tmp_path)),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["requirements_checked"] == 3
    assert any("DefinitelyNotInstalledPkg" in problem or "R runtime" in problem for problem in summary["problems"])
    assert report_path.read_text(encoding="utf-8").startswith("# Required-method preflight")


def test_failure_report_tells_the_agent_to_check_itself() -> None:
    report = failure_report("Traceback: boom")
    assert "preflight probe itself failed" in report and "boom" in report


def test_task_setup_runs_workspace_preparation_then_preflight() -> None:
    source = (Path(__file__).resolve().parents[2] / "src/bixbench3/inspect_harness/tasks.py").read_text()
    assert "setup=[prepare_case_workspace(), method_preflight()]," in source


class _LocalSandbox:
    """Runs the probe on the host, remapping the sandbox paths into a temp directory."""

    def __init__(self, root: Path, *, fail: bool = False) -> None:
        self.root = root
        self.fail = fail
        self.written: dict[str, str] = {}

    def _local(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    async def write_file(self, file: str, contents: str) -> None:
        self.written[file] = contents
        target = self._local(file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8", newline="\n")

    async def exec(self, cmd: list[str], timeout: int | None = None):
        from types import SimpleNamespace

        del timeout
        if self.fail:
            return SimpleNamespace(success=False, stdout="", stderr="python3: boom")
        argv = [sys.executable, *(str(self._local(part)) if part.startswith("/") else part for part in cmd[1:])]
        argv += ["--workspace", str(self.root / "workspace")]
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        return SimpleNamespace(success=done.returncode == 0, stdout=done.stdout, stderr=done.stderr)


def _state(sample_id: str):
    from inspect_ai.model import ChatMessageUser, ModelName
    from inspect_ai.solver import TaskState

    return TaskState(
        model=ModelName("mockllm/model"), sample_id=sample_id, epoch=1, input="prompt",
        messages=[ChatMessageUser(content="# Analysis Task\n\nBody.\n")],
    )


@pytest.mark.parametrize("fail", [False, True])
def test_solver_writes_report_and_extends_prompt_even_when_probe_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail: bool,
) -> None:
    import asyncio

    from bixbench3.inspect_harness import method_preflight as module

    fake = _LocalSandbox(tmp_path, fail=fail)
    monkeypatch.setattr(module, "sandbox", lambda: fake)
    monkeypatch.setattr(module, "load_preflight_spec", lambda sample_id: {
        "r_runtime": {"required": "any"}, "r_packages": [], "network": [],
        "staged_inputs": [{"path": "data/absent", "purpose": "test"}],
    })
    state = asyncio.run(module.method_preflight()(_state(TASK_11), None))
    report = (tmp_path / "workspace" / "work" / "preflight_methods.md").read_text(encoding="utf-8")
    assert report.startswith("# Required-method preflight")
    assert ("probe itself failed" in report) is fail
    assert state.user_prompt.text.endswith(module.PREFLIGHT_NOTICE)
    assert state.store.get("method_preflight")["status"] == ("probe_failed" if fail else "ok")


def test_solver_is_a_no_op_for_cases_without_a_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from bixbench3.inspect_harness import method_preflight as module

    def forbidden():
        raise AssertionError("sandbox must not be touched")

    monkeypatch.setattr(module, "sandbox", forbidden)
    state = asyncio.run(module.method_preflight()(_state("another-case"), None))
    assert state.user_prompt.text == "# Analysis Task\n\nBody.\n"
