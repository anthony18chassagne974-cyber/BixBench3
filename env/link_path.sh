#!/bin/bash
# Expose the task env's executables on a fixed PATH entry (/opt/task-env/bin), first dir wins.
# Python interpreters/pip are NOT linked, so the sandbox's system python3 (pandas, numpy...) stays default.
set -euo pipefail
. /opt/task-env/path.env
mkdir -p /opt/task-env/bin
IFS=: read -ra DIRS <<< "$TASK_ENV_PATH"
for d in "${DIRS[@]}"; do
  [ -d "$d" ] || continue
  for f in "$d"/*; do
    name=$(basename "$f")
    case "$name" in python|python3*|pip|pip3*|2to3*|idle*|pydoc*|conda*|activate|deactivate) continue;; esac
    [ -x "$f" ] && [ ! -d "$f" ] && [ ! -e "/opt/task-env/bin/$name" ] && ln -s "$f" "/opt/task-env/bin/$name"
  done
done
echo "[link_path] $(ls /opt/task-env/bin | wc -l) executables in /opt/task-env/bin"
