#!/bin/bash
# Prepare run bundles locally and submit them on Sherlock (one Slurm job per repeat).
#
#   sherlock/submit.sh <name> <checkout> <task> <condition> <description> <partition> [prepare_run options]
#
#   <name>       bundle/job name prefix, e.g. t11-v2f  (repeats get -r1, -r2, ...)
#   <checkout>   checkout whose committed harness runs (main checkout for the baseline, a worktree of exp/<x>)
#   <partition>  Slurm partition (normal, ...)
#   env: BB3_CPUS (default 6), BB3_MEM_GB (default 32), BB3_REPEATS (default 1), BB3_OWNER (default team),
#        BB3_CONSTRAINT (default CPU_GEN:RME = AMD EPYC 7502 nodes: one CPU type for every run, so repeats
#        and conditions are comparable; without it Slurm mixes 2016 Broadwell and newer nodes),
#        BB3_SANDBOX_SIF (optional: task-environment SIF on Sherlock built by env/build_sif.sh; recorded per run)
#   pass --harness-overlay for experiment branches (anything that changes src/bixbench3/inspect_harness)
set -euo pipefail
NAME=$1; CHECKOUT=$(cd "$2" && pwd); TASK=$3; CONDITION=$4; DESCRIPTION=$5; PARTITION=$6; shift 6
CPUS=${BB3_CPUS:-6}; MEM=${BB3_MEM_GB:-32}; REPEATS=${BB3_REPEATS:-1}; CONSTRAINT=${BB3_CONSTRAINT-CPU_GEN:RME}
SHERLOCK_HOST=${BB3_SHERLOCK_HOST:-sherlock}
REMOTE_ROOT=${BB3_REMOTE_ROOT:-/scratch/users/sbennani/bixbench3}
# Sherlock partitions cap memory per CPU (normal: MaxMemPerCPU=8000 MB); above it Slurm silently adds CPUs
[ $((MEM * 1024)) -le $((CPUS * 8000)) ] || { echo "BB3_MEM_GB=$MEM needs more than $CPUS CPUs at 8000 MB/CPU: Slurm would change the CPU count"; exit 1; }
MAIN=$(cd "$(dirname "$0")/.." && pwd)
BUNDLES=${BB3_LOCAL_BUNDLES:-$MAIN/runs/sherlock-bundles}
mkdir -p "$BUNDLES"
for r in $(seq 1 "$REPEATS"); do
  B=$NAME-r$r
  (cd "$CHECKOUT" && uv run --project "$MAIN" python "$MAIN/sherlock/prepare_run.py" --task "$TASK" \
    --condition "$CONDITION" --description "$DESCRIPTION [repeat $r/$REPEATS, ${CPUS} CPU/${MEM} GB, Sherlock${BB3_SANDBOX_SIF:+, sandbox $(basename "$BB3_SANDBOX_SIF")}]" \
    --owner "${BB3_OWNER:-team}" --checkout "$CHECKOUT" --cpus "$CPUS" --mem-gb "$MEM" --repeat "$r" \
    --out "$BUNDLES/$B" "$@" >/dev/null)
  REMOTE=$REMOTE_ROOT/bundles/$B
  ssh -o BatchMode=yes "$SHERLOCK_HOST" "test ! -e $REMOTE" 2>/dev/null || { echo "remote bundle exists: $REMOTE"; exit 1; }
  rsync -az "$BUNDLES/$B/" "$SHERLOCK_HOST:$REMOTE/" 2>/dev/null
  job=$(ssh -o BatchMode=yes "$SHERLOCK_HOST" "cd ~/bixbench3-sherlock && sbatch --parsable -p $PARTITION ${CONSTRAINT:+-C $CONSTRAINT} ${BB3_SANDBOX_SIF:+--export=ALL,BB3_SANDBOX_SIF=$BB3_SANDBOX_SIF} -c $CPUS --mem=${MEM}G -J bb3-$B -o $REMOTE/slurm-%j.out BixBench3/sherlock/run_job.sbatch $REMOTE" 2>/dev/null | grep -v '^Automated')
  [ -n "$job" ] || { echo "sbatch failed for $B"; exit 1; }
  echo "$B -> job $job"
done
