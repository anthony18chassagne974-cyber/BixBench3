# Preinstalled task environments

Goal: every required artifact of a task can be produced with software already in the
sandbox image — no installation during the run (no setup time, no failed installs, no
silent method substitution). One folder per task:

| Task | Folder | Notes |
|---|---|---|
| 11 (`10.1016/j.crtox.2021.01.003`) | [`task11/`](task11/README.md) | QuasR 1.26.0 only built for R 3.6 (pins conflict); live Ensembl 100 BioMart retired → orthologs rebuilt from the release-100 dump |
| 9 (`10.1186/s12915-024-01879-0`) | [`task9/`](task9/README.md) | every pin has an exact binary; org.Hs.eg.db / GO.db not pinned by the prompt (Bioconductor 3.12 chosen) |

Each folder has `build.sh` (runs inside the image at build time, network allowed),
`path.env` (which env dirs are put first on PATH), a verification script that runs every
pipeline step on synthetic data **without network**, and writes `/opt/task-env/MANIFEST.json`
(installed versions, deviations from the prompt, provenance).

## Sherlock (Apptainer)

Just ask for the environment when submitting; the job takes care of the image:
```bash
BB3_TASK_ENV=auto BB3_CPUS=6 BB3_MEM_GB=32 \
  sherlock/submit.sh t9-env . 9 v1-env "Task 9 with preinstalled env" normal
```
- `BB3_TASK_ENV=auto` means `task<TASK>` (here `task9`); `task9` / `task11` also work.
- The job looks for `$SCRATCH/bixbench3/images/bixbench3-sandbox_1.0.0-task9env-<hash>.sif`, where
  `<hash>` comes from the **committed** `env/` files of the Sherlock checkout
  (`~/bixbench3-sherlock/BixBench3`, which must contain `env/`). Found → reused. Not found →
  built inside the job first (`env/build_sif.sh`, network + `--fakeroot`, offline verification;
  a failing image is never published), under a lock so parallel repeats build it once. The
  agent's clock only starts after that. Changing anything in `env/task9` gives a new hash, hence
  a fresh image; old images stay untouched.
- Each run records `sandbox_image.json` (path, env hash, sha256, task label) and, if it built the
  image, `task_env_build.log`.
- Without `BB3_TASK_ENV` nothing changes: the stock sandbox SIF is used (baselines).
- Pre-building is optional (`srun ... bash env/build_sif.sh task9 <base.sif> <out.sif>`);
  `BB3_SANDBOX_SIF=<path>` forces a given SIF.

## GCP / Docker

```bash
docker build --build-arg BASE=bixbench3-sandbox:1.0.0 --build-arg TASK=task9 \
  -t bixbench3-sandbox:1.0.0-task9env -f env/Dockerfile .
docker run --rm --network none bixbench3-sandbox:1.0.0-task9env bash /opt/task-env/verify_env.sh
```

## In the image

`/opt/task-env/bin` is first on PATH: plain `Rscript`, `hisat2`, `featureCounts`… are the
pinned versions. Python interpreters are not linked there, so `python3` stays the sandbox's
own (pandas, numpy…).

## Rules

- A task environment is an **environment change** (and a data change when it pre-stages
  reference data): run it as its own condition, compare it with a baseline run on the same
  infrastructure, and record `MANIFEST.json`, the lock files and `sandbox_image.json`.
- Built from the task **prompt** only (what the agent receives): no task data, no reference
  outputs, no ground truth in the image.
