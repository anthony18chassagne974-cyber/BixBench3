import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_sif_definition_is_rendered_for_each_task(tmp_path):
    base = tmp_path / "base.sif"
    base.write_text("")
    for task in ("task9", "task11"):
        out = subprocess.run(["bash", str(ROOT / "env" / "build_sif.sh"), task, str(base)],
                             env={**os.environ, "PRINT_DEF": "1"}, capture_output=True, text=True, check=True).stdout
        assert f"From: {base}" in out and f"bixbench3.task_env {task}" in out
        assert f"env/{task} /opt/task-env" in out and "link_path.sh" in out
        assert "export PATH=/opt/task-env/bin:$PATH" in out and "@" not in out


def test_task9_pins_match_prompt():
    deps = yaml.safe_load((ROOT / "env/task9/environment.yml").read_text())["dependencies"]
    tools = yaml.safe_load((ROOT / "env/task9/tools.yml").read_text())["dependencies"]
    assert "bioconductor-deseq2=1.30.1" in deps and "bioconductor-clusterprofiler=3.18.1" in deps
    assert "bioconductor-org.hs.eg.db=3.12.0" in deps and "bioconductor-go.db=3.12.1" in deps
    assert "hisat2=2.2.1" in tools and "subread=2.0.1" in tools


def test_shell_scripts_parse():
    for script in ["env/build_sif.sh", "env/link_path.sh", "env/task9/build.sh", "env/task9/verify_env.sh",
                   "env/task11/build.sh", "sherlock/run_job.sbatch", "sherlock/submit.sh"]:
        subprocess.run(["bash", "-n", str(ROOT / script)], check=True)
