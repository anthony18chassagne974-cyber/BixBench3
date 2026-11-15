"""Prepare one BixBench3 run bundle for Stanford Sherlock (runs locally, no cloud calls).

Uses the official runner code so the Sherlock run gets exactly what a GCP run gets:
the same Inspect command, staged HF case, grading, process judge, redaction and upload
script (gcp_runner.remote_run_script), and for harness experiments the same committed
harness overlay (create_harness_overlay). Only host paths are rewritten on Sherlock
(sherlock/run_job.sbatch) and `--sandbox apptainer:...` replaces the Docker sandbox.

Run from the checkout whose harness should run (main for a baseline, an exp/* worktree
for an experiment), with the main checkout's interpreter:

  uv run --project <main> python <main>/sherlock/prepare_run.py --task 11 \
      --condition baseline-sherlock --description "..." --owner sofia --out <bundle-dir>
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from bixbench3.cli import _runner
from bixbench3.config import resolve_model
from bixbench3.gcp_runner import (
    TaskSpec,
    create_harness_overlay,
    load_public_task,
    loader_copy_script,
    remote_run_script,
    stage_hf_case,
)

TEAM_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "team-6vcpu.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--condition", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--owner", default="")
    parser.add_argument("--results-bucket", default="gs://bixbench3-sofia-results")
    parser.add_argument("--project", default="project-9e1a27ee-8352-4437-845")
    parser.add_argument("--dashboard-uri", default="gs://bixbench3-task11-jex20-results/_dashboard")
    parser.add_argument("--harness-overlay", action="store_true")
    parser.add_argument("--checkout", type=Path, default=Path.cwd(), help="checkout whose committed harness runs")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cpus", type=int, default=6)
    parser.add_argument("--mem-gb", type=int, default=32)
    parser.add_argument("--repeat", type=int, default=None, help="repeat index (recorded in the manifest)")
    args = parser.parse_args()

    team = json.loads(TEAM_CONFIG.read_text())
    task: TaskSpec = load_public_task(args.task)
    model = resolve_model(team["model"], team["effort"], user_model_args={})
    runner = _runner(
        args.project,
        machine_type=team["machine_type"],
        boot_disk_gb=team["boot_disk_gb"],
        boot_disk_type=team["boot_disk_type"],
        runtime_image=team["runtime_image"],
        no10x_runtime=team["no10x_runtime"],
        track_live=True,
        run_description=args.description,
        run_condition=args.condition,
        tracking_grade_every=team["tracking_grade_every"],
        dashboard_uri=args.dashboard_uri,
        owner=args.owner,
        harness_overlay=args.harness_overlay,
    )
    if args.out.exists():
        raise FileExistsError(args.out)
    args.out.mkdir(parents=True)

    staged = stage_hf_case(task, args.out / "dataset")
    plan = runner.build_run_plan(
        task=task,
        model=model,
        results_bucket=args.results_bucket,
        case_dataset_dir=staged.dataset_dir,
    )
    script = remote_run_script(plan, runner.settings)
    (args.out / "run.sh.template").write_text(script, encoding="utf-8")
    (args.out / "inspect_argv.json").write_text(json.dumps(list(plan.inspect_argv)) + "\n")

    overlay = {"enabled": False}
    if args.harness_overlay:
        overlay = create_harness_overlay(args.checkout.resolve(), args.out)

    # Data staging: the official loader's verified-download code, minus the GCP disk handling.
    loader = loader_copy_script(task)
    body_start = loader.index("<<'PY'\n") + len("<<'PY'\n")
    body_end = loader.index("\nPY\n", body_start)
    (args.out / "stage_data.py").write_text(loader[body_start:body_end] + "\n", encoding="utf-8")
    manifest_url = task.input_manifest_uri.replace("gs://", "https://storage.googleapis.com/", 1)
    (args.out / "stage_data.args").write_text(f"{manifest_url}\n{task.input_manifest_sha256}\n")

    manifest = {
        "schema_version": 1,
        "package_version": None,
        "task_id": task.task_id,
        "paper_id": task.paper_id,
        "run_id": plan.layout.run_id,
        "model": {"id": model.model_id, "arguments": model.model_args},
        "effort": {"requested": model.requested_effort, "resolved": model.resolved_effort},
        "limits": {"time_seconds": runner.settings.time_limit_seconds, "messages": runner.settings.message_limit},
        "hf_revision": task.hf_revision,
        "hf_case_files_sha256": staged.file_sha256,
        "gcs_release": task.gcs_release,
        "input_manifest_sha256": task.input_manifest_sha256,
        "ground_truth_manifest_sha256": task.grader_manifest_sha256,
        "platform": {
            "name": "sherlock",
            "cpus": args.cpus,
            "memory_gb": args.mem_gb,
            "repeat": args.repeat,
            "sandbox": "apptainer SIF of bixbench3-sandbox:1.0.0 + bb3jail network jail",
            "gateway": "apptainer SIF of bixbench3-network-gateway:1.0.0",
            "gcp_equivalent_machine_type": team["machine_type"],
        },
        "retention": {"mode": "full_bundle", "results_uri": plan.results_uri},
        "live_tracking": {
            "enabled": True,
            "description": args.description,
            "condition": args.condition,
            "dashboard_uri": args.dashboard_uri,
            "owner": args.owner or None,
        },
        "harness_overlay": overlay,
        "prepared_at": datetime.now(UTC).isoformat(),
        "status": "created",
        "phase": "provisioning",
    }
    from bixbench3 import __version__
    from bixbench3.gcp_runner import process_judge_config

    manifest["package_version"] = __version__
    manifest["process_judge"] = process_judge_config()
    (args.out / "run_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    run_meta = {
        "description": args.description,
        "condition": args.condition,
        "owner": args.owner or None,
        "dashboard_uri": args.dashboard_uri,
    }
    (args.out / "run_meta.json").write_text(json.dumps(run_meta, indent=2, ensure_ascii=False) + "\n")
    job = {
        "task_id": task.task_id,
        "paper_id": task.paper_id,
        "run_id": plan.layout.run_id,
        "results_uri": plan.results_uri,
        "harness_overlay": bool(args.harness_overlay),
        "cpus": args.cpus,
        "mem_gb": args.mem_gb,
        "repeat": args.repeat,
    }
    (args.out / "job.json").write_text(json.dumps(job, indent=2) + "\n")
    print(json.dumps(job, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
