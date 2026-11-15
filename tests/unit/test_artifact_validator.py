"""Experiment v2b: final artifact contract validator."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from inspect_ai.tool import ToolError

from bixbench3.inspect_harness import artifact_validator as module
from bixbench3.inspect_harness.artifact_validator import (
    CHECK_SCRIPT_PATH,
    MAX_SUBMIT_REFUSALS,
    VALIDATOR_NOTICE,
    append_validator_notice,
    format_outcome,
)

PROMPT = """# Analysis Task

## Required Outputs

### `counts`

- Path: `outputs/counts.csv`
- Format: `csv`
- Row key: `gene_probe_id`
- Columns: one numeric count column per sample. Required sample columns are: `S1`, `S2`.

### `degs`

- Path: `outputs/degs.csv`
- Format: `csv`
- Row key: `contrast_gene_id`
- Required columns: `contrast_gene_id`, `timepoint`, `stat`, `pvalue`, `padj`
"""
EXPECTED = [
    {"artifact_id": "counts", "path": "outputs/counts.csv", "format": "csv", "row_key": "gene_probe_id"},
    {"artifact_id": "degs", "path": "outputs/degs.csv", "format": "csv", "row_key": "contrast_gene_id"},
]
GOOD_COUNTS = "gene_probe_id,S1,S2\nA_1,10,0\nB_2,3,7\n"
GOOD_DEGS = "contrast_gene_id,timepoint,stat,pvalue,padj\nday7|A_1,day7,2.5,0.01,0.05\nday7|B_2,day7,-3.1,0.002,NA\n"


def _checker():
    spec = importlib.util.spec_from_file_location("artifact_contract_check", CHECK_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def _workspace(root: Path, *, counts: str | None = GOOD_COUNTS, degs: str | None = GOOD_DEGS,
               methods: str | None = "## counts\n\n## degs\n") -> Path:
    outputs = root / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    for name, content in (("counts.csv", counts), ("degs.csv", degs), ("METHODS.md", methods)):
        if content is not None:
            (outputs / name).write_text(content, encoding="utf-8", newline="\n")
    return root


def _validate(root: Path) -> dict:
    return _checker().validate({"prompt": PROMPT, "expected_outputs": EXPECTED}, root)


def test_contract_is_parsed_from_the_prompt_only() -> None:
    contract = _checker().parse_contract_from_prompt(PROMPT)
    assert contract["counts"]["required_columns"] == ["S1", "S2"]
    assert contract["degs"]["required_columns"] == ["contrast_gene_id", "timepoint", "stat", "pvalue", "padj"]
    assert contract["degs"]["row_key"] == "contrast_gene_id"
    source = CHECK_SCRIPT_PATH.read_text(encoding="utf-8").lower()
    assert "ground_truth" not in source and "ground-truth" not in source


def test_well_formed_outputs_pass_and_missing_padj_values_are_allowed(tmp_path: Path) -> None:
    outcome = _validate(_workspace(tmp_path))
    assert outcome["severe"] == [] and outcome["warnings"] == []


@pytest.mark.parametrize(
    ("counts", "degs", "methods", "expected"),
    [
        (None, GOOD_DEGS, "x", "outputs/counts.csv: required output file does not exist"),
        ("", GOOD_DEGS, "x", "outputs/counts.csv: required output file is empty (0 bytes)"),
        ("gene_probe_id,S1,S2\n", GOOD_DEGS, "x", "outputs/counts.csv: file has a header but no data rows"),
        ("gene_probe_id,S1\nA_1,1\n", GOOD_DEGS, "x", "outputs/counts.csv: missing required column(s): 'S2'"),
        ("probe,S1,S2\nA_1,1,2\n", GOOD_DEGS, "x", "outputs/counts.csv: row-key column `gene_probe_id` is missing"),
        ("gene_probe_id,S1,S2\nA_1,1,2\nA_1,3,4\n", GOOD_DEGS, "x",
         "outputs/counts.csv: 1 duplicate `gene_probe_id` value(s): 'A_1'"),
        ("gene_probe_id,S1,S2\n,1,2\n", GOOD_DEGS, "x", "outputs/counts.csv: 1 row(s) have an empty `gene_probe_id`"),
        ("gene_probe_id,S1,S2\nA_1,1\n", GOOD_DEGS, "x",
         "outputs/counts.csv: 1 row(s) have a different number of fields than the header (first at line 2)"),
        (GOOD_COUNTS, GOOD_DEGS.replace("0.01", "1.7"), "x",
         "outputs/degs.csv: column `pvalue` has 1 value(s) outside [0, 1]: '1.7'"),
        (GOOD_COUNTS, GOOD_DEGS.replace("0.002", "<0.001"), "x",
         "outputs/degs.csv: column `pvalue` has 1 non-numeric value(s): '<0.001'"),
        (GOOD_COUNTS, "contrast_gene_id,timepoint,stat,pvalue,padj\na,NA,NA,NA,NA\n", "x",
         "outputs/degs.csv: every non-key cell is missing (NA-only table)"),
        (GOOD_COUNTS, GOOD_DEGS, None, "outputs/METHODS.md: required file `outputs/METHODS.md` is missing or empty"),
    ],
)
def test_severe_structural_failures(tmp_path: Path, counts: str | None, degs: str | None,
                                    methods: str | None, expected: str) -> None:
    assert expected in _validate(_workspace(tmp_path, counts=counts, degs=degs, methods=methods))["severe"]


def test_non_structural_oddities_are_warnings_only(tmp_path: Path) -> None:
    degs = "timepoint,contrast_gene_id,stat,pvalue,padj\nTODO,day7|A_1,2.5,0.01,0.05\nday7,day7|B_2,abc,0.5,0.6\n"
    counts = "gene_probe_id,S1,S2\n" + "".join(f"P{i},{i},{i}\n" for i in range(12)).replace("P11,11,11", "P11,x,11")
    outcome = _validate(_workspace(tmp_path, counts=counts, degs=degs, methods="## counts\n"))
    assert outcome["severe"] == []
    warnings = "\n".join(outcome["warnings"])
    assert "row-key column `contrast_gene_id` is not the first column" in warnings
    assert "column `timepoint` contains placeholder-like value(s): 'TODO'" in warnings
    assert "column `S1` has 1 non-numeric value(s): 'x'" in warnings
    assert "no `## <artifact_id>` section for: 'degs'" in warnings


def test_checker_script_runs_standalone(tmp_path: Path) -> None:
    _workspace(tmp_path, counts=None)
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps({"prompt": PROMPT, "expected_outputs": EXPECTED}))
    report = tmp_path / "work" / "artifact_validation.md"
    result = subprocess.run(
        (sys.executable, str(CHECK_SCRIPT_PATH), "--contract", str(contract), "--workspace", str(tmp_path),
         "--report", str(report)),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["severe"] == ["outputs/counts.csv: required output file does not exist"]
    assert "## `outputs/counts.csv`: SEVERE" in report.read_text(encoding="utf-8")


class _LocalSandbox:
    def __init__(self, root: Path, *, broken: bool = False) -> None:
        self.root, self.broken = root, broken

    def _local(self, path: str) -> str:
        return str(self.root) if path == "/workspace" else str(self.root / path.lstrip("/"))

    async def write_file(self, file: str, contents: str) -> None:
        target = Path(self._local(file))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8", newline="\n")

    async def exec(self, cmd: list[str], input: str | None = None, timeout: int | None = None) -> SimpleNamespace:
        del timeout
        if cmd[0] == "sh":
            events = self.root / "workspace" / "work" / "artifact_validation_events.jsonl"
            events.parent.mkdir(parents=True, exist_ok=True)
            with events.open("a", encoding="utf-8") as handle:
                handle.write(input or "")
            return SimpleNamespace(success=True, stdout="", stderr="")
        if self.broken:
            return SimpleNamespace(success=False, stdout="", stderr="boom")
        argv = [sys.executable, *(self._local(part) if part.startswith("/") else part for part in cmd[1:])]
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        return SimpleNamespace(success=done.returncode == 0, stdout=done.stdout, stderr=done.stderr)


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, broken: bool = False) -> dict:
    fake = _LocalSandbox(tmp_path, broken=broken)
    values: dict = {}
    monkeypatch.setattr(module, "sandbox", lambda: fake)
    monkeypatch.setattr(module, "store", lambda: SimpleNamespace(get=values.get, set=values.__setitem__))
    asyncio.run(fake.write_file(module.SANDBOX_SCRIPT_PATH, CHECK_SCRIPT_PATH.read_text(encoding="utf-8")))
    asyncio.run(fake.write_file(module.SANDBOX_CONTRACT_PATH, json.dumps({"prompt": PROMPT, "expected_outputs": EXPECTED})))
    return values


def test_submit_is_refused_for_severe_problems_then_accepted_once_fixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _install(tmp_path, monkeypatch)
    _workspace(tmp_path, counts=None)
    submit = module.validated_submit()
    with pytest.raises(ToolError, match="Submission not accepted yet") as refused:
        asyncio.run(submit("done"))
    assert "outputs/counts.csv: required output file does not exist" in str(refused.value)
    assert values[module.REFUSALS_KEY] == 1
    _workspace(tmp_path)
    assert asyncio.run(submit("done")) == "done"
    events = (tmp_path / "workspace" / "work" / "artifact_validation_events.jsonl").read_text().splitlines()
    assert [json.loads(line)["severe"] for line in events] == [1, 0]


def test_submit_can_never_trap_a_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(tmp_path, monkeypatch)
    _workspace(tmp_path, counts=None)
    submit = module.validated_submit()
    for _ in range(MAX_SUBMIT_REFUSALS):
        with pytest.raises(ToolError):
            asyncio.run(submit("done"))
    assert asyncio.run(submit("done")) == "done"


def test_warnings_and_a_broken_validator_never_block_submit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(tmp_path, monkeypatch)
    _workspace(tmp_path, methods="no sections")
    assert asyncio.run(module.validated_submit()("done")) == "done"
    assert "warning(s):" in asyncio.run(module.validate_outputs()())
    _install(tmp_path, monkeypatch, broken=True)
    _workspace(tmp_path, counts=None)
    assert asyncio.run(module.validated_submit()("done")) == "done"
    assert "could not run" in asyncio.run(module.validate_outputs()())


def test_submit_tool_keeps_the_baseline_name_and_signature() -> None:
    from inspect_ai.tool import ToolDef

    definition = ToolDef(module.validated_submit())
    assert definition.name == "submit"
    assert definition.description == "Submit an answer for evaluation."
    assert list(definition.parameters.properties) == ["answer"]


def test_notice_and_messages() -> None:
    prompt = append_validator_notice("# Analysis Task\n")
    assert prompt.endswith(VALIDATOR_NOTICE) and "`validate_outputs`" in prompt
    assert "does not judge scientific correctness" in prompt.replace("\n", " ")
    assert format_outcome({"severe": [], "warnings": []}).startswith("Output contract check passed")


def test_agent_and_task_are_wired_to_the_validator() -> None:
    root = Path(__file__).resolve().parents[2] / "src/bixbench3/inspect_harness"
    assert "submit=AgentSubmit(tool=validated_submit())," in (root / "solvers.py").read_text()
    assert "validate_outputs()," in (root / "solvers.py").read_text()
    assert "setup=[prepare_case_workspace(), prepare_artifact_validator()]," in (root / "tasks.py").read_text()
