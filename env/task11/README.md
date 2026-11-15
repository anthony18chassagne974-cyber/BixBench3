# Task 11 preinstalled environment

**Objective:** every required task 11 artifact can be produced with packages that are
already in the sandbox image — no installation during the run, so no setup time, no
failed installs, and no silent method substitution.

## What the prompt requires vs. what binaries exist

Pins extracted from the Method Guidance (`scripts/task_versions.py`), checked against
the bioconda repodata (2026-11-15):

| Prompt pin | Binary available for R 4.0 | How we get the exact version |
|---|---|---|
| R 4.0.2 | `r-base=4.0.2` | exact |
| piano 2.4.0 | `bioconductor-piano=2.4.0` (r40) | exact |
| DESeq2 1.28.1 | 1.28.0 only | `exact_pins.R` → Bioconductor 3.11 |
| biomaRt 2.44.1 | 2.44.0 only | `exact_pins.R` → Bioconductor 3.11 |
| QuasR 1.26.0 | **built for R 3.6 only** (Bioconductor 3.10); R 4.0 build is 1.28.0 | `exact_pins.R` tries a source build of 1.26.0 under R 4.0.2, else keeps 1.28.0 |

**The prompt's pins are not all satisfiable together from binaries** (QuasR 1.26.0 is a
Bioconductor 3.10 / R 3.6 package; everything else is Bioconductor 3.11 / R 4.0).
Whatever is finally installed — and any deviation — is written to
`/opt/task-env/MANIFEST.json` inside the image.

Also required by the pipeline but not version-pinned by the prompt: Rbowtie (QuasR's
aligner), readxl (DNA-damage workbook), data.table / dplyr / tidyr / readr.

**Ensembl 100 / BioMart: the live service no longer exists.** The prompt asks biomaRt
2.44.1 to query `https://apr2020.archive.ensembl.org/biomart/martservice` (it is even in
the task's network allowlist), but Ensembl retired that archive: it now answers
`301 -> https://www.ensembl.org/help/articles/archives` (checked 2026-11-15). In the
baseline the broker reported `redirect_off_scope` and the agent fell back to babelgene.
No run can follow the prescribed live query any more.
`fetch_ensembl100_orthologs.py` rebuilds the same mouse->human table from the official
**release-100 BioMart MySQL dump** (`ftp.ensembl.org/pub/release-100/mysql/ensembl_mart_100/`)
at image build time, with biomaRt-style columns and a provenance file (URLs, sha256,
column mapping). Pre-staging it is a **declared data change**.

**MSigDB v6.2 GMT files** are reference data, not packages; in the baseline the agent
obtained them through `request_web_access` (approved).

## Files

| File | Role |
|---|---|
| `environment.yml` | conda env `task11`: R 4.0.2 + closest R 4.0 builds + implicit deps |
| `fetch_ensembl100_orthologs.py` | Ensembl 100 mouse->human orthologs from the release-100 BioMart dump (`--inspect` to check columns first) |
| `exact_pins.R` | upgrades to the exact prompt versions at image build time, writes `MANIFEST.json` |
| `verify_env.R` | runs every pipeline step on tiny synthetic data with preinstalled packages only |
| `Dockerfile` | layer on top of `bixbench3-sandbox:<version>` |
| `available_packages.task11.yaml` | section that tells the agent the env exists (prompt change) |

## Build and verify (builder VM with Docker and network)

First check the dump's column names (mart columns carry numeric suffixes):
```bash
python3 env/task11/fetch_ensembl100_orthologs.py --inspect
```

```bash
docker build --build-arg BASE=bixbench3-sandbox:1.0.0 --build-arg EXACT_PINS=1 \
  -t bixbench3-sandbox:1.0.0-task11env -f env/task11/Dockerfile .
docker run --rm --network none bixbench3-sandbox:1.0.0-task11env \
  micromamba run -n task11 Rscript /opt/task-env/verify_env.R          # must print ALL STEPS PASS
docker run --rm bixbench3-sandbox:1.0.0-task11env cat /opt/task-env/MANIFEST.json
docker run --rm bixbench3-sandbox:1.0.0-task11env \
  micromamba run -n task11 Rscript /opt/task-env/verify_env.R --network   # also records the live archive status (expected 301)
```

`--network none` is the point: it proves the steps need nothing from the internet.

## Rules for using it in an experiment

- It is an **environment change** (and, with `available_packages.task11.yaml`, a
  **prompt change**): run it as its own version (e.g. `v2a-env`), never relabel the baseline.
- Record in the tracker: image tag, `MANIFEST.json` (installed versions, deviations),
  `task11.lock.txt` (exact conda build list), `verify_build.json`.
- Built only from the task **prompt** (what the agent receives). It contains no task
  data, no reference outputs and no ground truth.
- Wiring it into the runner's machine image (`setup-runtime`) is a follow-up: the image
  build currently uses `sandbox/Dockerfile` only.
