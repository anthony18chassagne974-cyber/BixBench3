#!/bin/bash
# One-time setup of the BixBench3 Sherlock runtime (idempotent; safe to resubmit).
#   - converts the exported Docker images to SIFs in $SCRATCH/bixbench3/images
#     (reconstructible from gs://bixbench3-sofia-results/_sherlock/images, so scratch is fine)
#   - compiles bb3jail inside the sandbox image (static, matches the container's libc)
#   - creates the harness venv in $HOME/bixbench3-sherlock/venv inside a python:3.12 runner image
#   - stages the official v1.0.0 inputs of the tasks given as arguments (SHA-verified)
#   - runs the jail test suite
#SBATCH --job-name=bb3-setup
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=02:00:00
set -euo pipefail
ml purge
ml python/3.12.1 uv/0.10.8
ml system google-cloud-sdk/448.0.0

BB3=$HOME/bixbench3-sherlock
REPO=$BB3/BixBench3
IMAGES=$SCRATCH/bixbench3/images
mkdir -p "$IMAGES" "$SCRATCH/bixbench3/data" "$SCRATCH/bixbench3/cache/uv" "$SCRATCH/bixbench3/cache/apptainer" "$BB3/jail"
export APPTAINER_CACHEDIR=$SCRATCH/bixbench3/cache/apptainer
export APPTAINER_TMPDIR=$L_SCRATCH_JOB
export UV_CACHE_DIR=$SCRATCH/bixbench3/cache/uv
export UV_PYTHON_DOWNLOADS=never

echo "== images"
declare -A SHA=(
  [bixbench3-sandbox_1.0.0]=e03832b60b31bd01ff1df1352dcdbc0ed82ece46145646d099b1a53a372701b4
  [bixbench3-network-gateway_1.0.0]=c5c92b4d2996727ef4e7e8697c3ee3098f49acb8f371afb53175d3733aadf3cf
)
for name in "${!SHA[@]}"; do
  if [ -f "$IMAGES/$name.sif" ]; then echo "$name.sif present"; continue; fi
  gcloud storage cp "gs://bixbench3-sofia-results/_sherlock/images/$name.tar.gz" "$L_SCRATCH_JOB/"
  echo "${SHA[$name]}  $L_SCRATCH_JOB/$name.tar.gz" | sha256sum --check -
  gunzip "$L_SCRATCH_JOB/$name.tar.gz"
  apptainer build "$L_SCRATCH_JOB/$name.sif" "docker-archive://$L_SCRATCH_JOB/$name.tar"
  mv "$L_SCRATCH_JOB/$name.sif" "$IMAGES/$name.sif"
  rm -f "$L_SCRATCH_JOB/$name.tar"
done

echo "== bb3jail"
apptainer exec --bind "$REPO/sherlock/jail:/src:ro" --bind "$BB3/jail:/out" "$IMAGES/bixbench3-sandbox_1.0.0.sif" \
  gcc -O2 -Wall -Wextra -static -o /out/bb3jail /src/bb3jail.c
ls -la "$BB3/jail/bb3jail"

echo "== runner image + venv"
# The locked wheels (numpy 2.4.4, pandas 3.0.3, scipy 1.17.1, pyarrow 24, tiktoken 0.13) need
# glibc >= 2.28; Sherlock (CentOS 7) has 2.17. The host side therefore runs in python:3.12 on
# Debian 12 (same glibc as the sandbox image) with the exact uv.lock environment.
RUNNER=$IMAGES/bixbench3-runner-py312.sif
if [ ! -f "$RUNNER" ]; then
  apptainer build "$L_SCRATCH_JOB/runner.sif" docker://python:3.12-slim-bookworm
  mv "$L_SCRATCH_JOB/runner.sif" "$RUNNER"
fi
rm -rf "$REPO/.venv"
UVBIN=$(dirname "$(readlink -f "$(command -v uv)")")
apptainer exec --cleanenv --bind "$BB3" --bind "$SCRATCH/bixbench3/cache/uv" --bind "$UVBIN:/opt/uvbin:ro" \
  --env "UV_CACHE_DIR=$UV_CACHE_DIR,UV_PROJECT_ENVIRONMENT=$BB3/venv,UV_PYTHON_DOWNLOADS=never,UV_LINK_MODE=copy" \
  "$RUNNER" sh -c "cd $REPO && /opt/uvbin/uv sync --locked --python /usr/local/bin/python3.12 && \
    /opt/uvbin/uv pip install --python $BB3/venv/bin/python --no-deps -e sherlock/provider && \
    $BB3/venv/bin/python -c 'import platform, inspect_ai, numpy, pandas, bb3_sherlock._registry; print(\"python\", platform.python_version(), platform.libc_ver(), \"inspect\", inspect_ai.__version__, \"numpy\", numpy.__version__, \"pandas\", pandas.__version__)'"

echo "== data"
for bundle in "$@"; do
  paper=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['paper_id'])" "$bundle/job.json")
  dest=$SCRATCH/bixbench3/data/$paper
  if [ -f "$dest/.bixbench3-ready.json" ]; then echo "$paper data present"; continue; fi
  mapfile -t args < "$bundle/stage_data.args"
  python3 "$bundle/stage_data.py" "${args[0]}" "$dest" "${args[1]}"
  du -sh "$dest"
done

sha256sum "$IMAGES"/*.sif | tee "$IMAGES/SHA256SUMS"
echo "== container probe"
bash "$REPO/sherlock/probe_container.sh" "$IMAGES/bixbench3-sandbox_1.0.0.sif"
echo "== jail tests"
bash "$REPO/sherlock/jail/test_jail.sh" "$IMAGES/bixbench3-sandbox_1.0.0.sif" "$BB3/jail/bb3jail"
echo "SETUP OK"
