# Running BixBench3 on Stanford Sherlock

Runs the official pipeline (same Inspect command, staged HF case, grading, process judge,
redaction, GCS upload and bixdash live dashboard as `scripts/run_team.py` on GCP) on Sherlock
compute nodes through Slurm.

## What is identical to GCP, what differs

Resources: set per run with `BB3_CPUS` / `BB3_MEM_GB` (team experiments: **6 CPU / 32 GB**, the GCP VM shape n2-custom-6-32768; the `normal` partition allows at most 8 GB per CPU, so Slurm would silently add CPUs to e.g. 3 CPU / 32 GB, and `submit.sh` refuses such combinations). `submit.sh` pins every run to one CPU type with `BB3_CONSTRAINT` (default `CPU_GEN:RME`, AMD EPYC 7502) so repeats and conditions run on the same hardware.

Identical: the agent sandbox image (`bixbench3-sandbox:1.0.0`, converted to a SIF), the gateway
image (`bixbench3-network-gateway:1.0.0`), the network policy, the host-side Python environment
(exact `uv.lock`, Python 3.12), model / effort / limits, prompt and SHA-verified v1.0.0 inputs,
the run script (`gcp_runner.remote_run_script`, host paths rewritten), the harness overlay for
experiment branches.

Different, because of Sherlock: CPU model (speed), network isolation mechanism (`bb3jail`
instead of Docker networks: Sherlock has no network or PID namespaces), the proxy port number,
`/proc/cpuinfo` and `/sys/devices/system/cpu/online` are bound to show the job's CPUs only,
an apt config (`/etc/apt/apt.conf.d/99-sherlock-fakeroot`) lets `apt-get` run as root under
`--fakeroot` as it does in Docker (without it apt fails with `setgroups ... Operation not permitted`;
runs submitted before this fix, e.g. the first 4 t11 baselines, had no working apt), `/proc/meminfo` is
bound to show the job's memory limit (`mem_view.py`), and `bb3jail` also confines signals: without a
PID namespace the sandbox could otherwise `kill` the run's host processes (Inspect, gateway) by pid,
by process group or with `kill -1`; such signals fail with ESRCH or reach sandbox processes only.
**Compare Sherlock runs with Sherlock runs** (run a Sherlock baseline per task).

## Pieces

| File | Role |
|---|---|
| `prepare_run.py` | Local: builds a run bundle with the official runner code (case, run.sh, manifest, overlay). |
| `submit.sh` | Local: prepares N repeats, copies bundles to Sherlock, `sbatch`es them. |
| `setup.sbatch` | Sherlock, once: images → SIFs, compiles `bb3jail`, builds the exact venv in a python:3.12 runner image, stages task data, runs tests. |
| `run_job.sbatch` | Sherlock: one run (gateway + sandbox via Apptainer, Inspect in the runner image, grading, judge, upload, dashboard collector). |
| `provider/` | Inspect sandbox provider `apptainer` (mirrors `sandbox/compose.yaml`). |
| `jail/bb3jail.c` | seccomp + ptrace network jail: only the gateway proxy and sandbox-owned loopback ports are reachable; works for static binaries (micromamba). `jail/test_jail.sh` tests it. |
| `bridge/` | The runner image cannot nest Apptainer; its `apptainer`/`gcloud` are forwarded to the host by a job-local helper (Unix socket, only those two commands). |
| `cpu_view.py`, `mem_view.py`, `probe_container.sh` | CPU and memory view files; container feature probe. |

## Usage

```bash
# once per task (on Sherlock, dev partition is interactive-only):
srun -p dev -c 4 --mem=16G -t 2:00:00 bash BixBench3/sherlock/setup.sbatch <bundle-dir-of-the-task> ...

# baseline, 2 repeats, 6 CPU / 32 GB, from the main checkout
BB3_CPUS=6 BB3_MEM_GB=32 BB3_REPEATS=2 sherlock/submit.sh t11-baseline . 11 baseline "Baseline harness" normal

# experiment branch (worktree of exp/v2f), harness overlay
BB3_CPUS=6 BB3_MEM_GB=32 BB3_REPEATS=2 sherlock/submit.sh t11-v2f ../wt/v2f 11 v2f-enrichment-validator "..." normal --harness-overlay

# smoke test (30 messages, whole pipeline in minutes)
BB3_SMOKE_MESSAGES=30 srun -p dev -c 4 --mem=16G -t 1:30:00 bash BixBench3/sherlock/run_job.sbatch <bundle>
```

Requirements on Sherlock: `~/.bixbench3_secrets` (mode 600) with `OPENAI_API_KEY`, a `gcloud auth login`
with write access to the results bucket and the dashboard prefix. Layout: code in
`~/bixbench3-sherlock`, images/data/bundles/runs in `$SCRATCH/bixbench3` (reconstructible).
Follow `/etc/agents/AGENTS.md` on Sherlock.
