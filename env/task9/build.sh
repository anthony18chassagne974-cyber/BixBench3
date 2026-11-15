#!/bin/bash
# Build the task 9 environment inside the sandbox image (Docker RUN or Apptainer %post).
# Needs network (conda, github.com) at build time only.
set -euo pipefail
D=/opt/task-env
export MAMBA_ROOT_PREFIX=${MAMBA_ROOT_PREFIX:-/opt/conda}
micromamba create -y -f "$D/environment.yml"
micromamba create -y -f "$D/tools.yml"
micromamba clean -a -y

# tag-based_RNAseq (the only method URL in the task's network allowlist), pinned to a commit
TAGSEQ_REF=${TAGSEQ_REF:-}
micromamba run -n task9-tools git clone --quiet https://github.com/z0on/tag-based_RNAseq "$D/tag-based_RNAseq"
if [ -n "$TAGSEQ_REF" ]; then git -C "$D/tag-based_RNAseq" checkout --quiet "$TAGSEQ_REF"; fi
TAGSEQ_COMMIT=$(git -C "$D/tag-based_RNAseq" rev-parse HEAD)
rm -rf "$D/tag-based_RNAseq/.git"
test -f "$D/tag-based_RNAseq/tagseq_clipper.pl"
chmod +x "$D"/tag-based_RNAseq/*.pl

micromamba env export -n task9 --explicit > "$D/task9.lock.txt"
micromamba env export -n task9-tools --explicit > "$D/task9-tools.lock.txt"
TAGSEQ_COMMIT=$TAGSEQ_COMMIT bash "$D/verify_env.sh" --report "$D/verify_build.json" --manifest "$D/MANIFEST.json"
