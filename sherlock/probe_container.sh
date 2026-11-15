#!/bin/bash
# Quick probe of container features the sandbox relies on. Usage: probe_container.sh <sandbox.sif>
set -uo pipefail
SIF=$1; W=$(mktemp -d "${L_SCRATCH_JOB:-/tmp}/probe.XXXX"); HERE=$(cd "$(dirname "$0")" && pwd)
python3 "$HERE/cpu_view.py" "$W/cpuinfo" "$W/online" >/dev/null
mkdir -p "$W/overlay" "$W/wd"
NAME=bb3probe-$$
apptainer instance start --containall --cleanenv --fakeroot --ignore-fakeroot-command --overlay "$W/overlay" --workdir "$W/wd" \
  --bind "$W/cpuinfo:/proc/cpuinfo:ro" --bind "$W/online:/sys/devices/system/cpu/online:ro" "$SIF" "$NAME" 2>&1 | tail -3
E() { apptainer exec --cleanenv "instance://$NAME" "$@"; }
echo "host affinity: $(nproc)"
echo "nproc: $(E nproc)"
echo "python os.cpu_count: $(E python3 -c 'import os;print(os.cpu_count())')"
echo "R detectCores: $(E Rscript -e 'cat(parallel::detectCores())' 2>/dev/null)"
echo "id: $(E id -u)"
echo "/tmp: $(E df -h /tmp | tail -1)"
echo "/dev/shm: $(E df -h /dev/shm | tail -1)"
echo "multiprocessing: $(E python3 -c 'from multiprocessing import Pool
with Pool(3) as p: print(sum(p.map(abs,[-1,-2,-3])))' 2>&1 | tail -1)"
echo "write /opt/conda: $(E sh -c 'mkdir -p /opt/conda/envs/probe && echo ok')"
apptainer instance stop "$NAME" >/dev/null 2>&1; rm -rf "$W"
