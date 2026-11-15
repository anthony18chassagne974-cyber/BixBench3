#!/bin/bash
# Build the task 11 environment inside the sandbox image (Docker RUN or Apptainer %post).
# Needs network (conda, bioconductor.org, ftp.ensembl.org) at build time only.
set -euo pipefail
D=/opt/task-env
export MAMBA_ROOT_PREFIX=${MAMBA_ROOT_PREFIX:-/opt/conda}
micromamba create -y -f "$D/environment.yml"
micromamba clean -a -y
# Ensembl 100 mouse->human orthologs from the release-100 BioMart dump (live archive retired: 301)
python3 "$D/fetch_ensembl100_orthologs.py" --out "$D/reference"
if [ "${EXACT_PINS:-1}" = "1" ]; then
  micromamba run -n task11 Rscript "$D/exact_pins.R" --manifest "$D/MANIFEST.json"
else
  micromamba run -n task11 Rscript -e 'jsonlite::write_json(list(task="11", exact_pins="skipped", installed=as.list(sapply(c("QuasR","DESeq2","biomaRt","piano"), function(p) as.character(packageVersion(p))))), "/opt/task-env/MANIFEST.json", auto_unbox=TRUE, pretty=TRUE)'
fi
micromamba env export -n task11 --explicit > "$D/task11.lock.txt"
micromamba run -n task11 Rscript "$D/verify_env.R" --report "$D/verify_build.json"
