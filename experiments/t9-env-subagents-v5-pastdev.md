# t9-env-subagents-v5-pastdev (Sherlock)

- **Task:** 9, `10_1186_s12915_024_01879_0_v1`, official v1.0.0 prompt; preinstalled task 9 environment
- **Compared with:** `t9-env-subagents-v5` (Sofia): run both conditions with the same settings and repeats
- **Base:** `exp/t9-env-subagents-v5` (parallel levels, medium-effort sub-agents, blocking waits, no default planner)
- **Single modification:** the shared run memory through past.dev, identical to `exp/t9-env-subagents-pastdev`
  (`src/bixbench3/inspect_harness/run_memory.py`, unchanged). See `experiments/t9-env-subagents-pastdev.md` for what it
  captures, how the context pack is built and where the memory files are.

## Wiring in v5

- `tools/subagent.py`: in `_run_one` (used by both `run_subagent` and `run_subagents`), the context pack is built from the
  brief and appended to it before the sub-agent starts; after it ends, its LEARNED items and produced files are stored and
  the LEARNED block is replaced by a one-line note in the report. Sub-agent shell, python and text editor are wrapped to
  capture read-only inspections; sub-agents get the `recall` tool. `SUBAGENT_MEMORY_RULES` is appended to the v5
  sub-agent instructions.
- `solvers.py`: the orchestrator's shell, python and text editor are wrapped the same way; it gets `record_convention` and
  `recall`.
- `tasks.py`: `run_memory_setup()` runs after `condition_note_setup()` (memory note after the orchestration note).
- `sherlock/run_job.sbatch` passes `PAST_API_KEY` from `~/.bixbench3_secrets`; `gcp_runner` redacts `past_sk_...` keys.

## Parallel levels

Sub-agents of one level start together, so their packs hold what earlier levels and the orchestrator established, not what
their siblings find meanwhile. Siblings' cards are written as they go and can be found with `recall` during the run.
Concurrent writes from parallel sub-agents are covered by a unit test (6 agents, no lost card).

## Memory scope

One past.dev audience per run (from the sample id and a microsecond timestamp): every run, including Inspect retries,
starts with an empty memory and cannot read another run's cards. Cards stay stored in past.dev after the run; a full copy
is uploaded with the run in `broker_logs/memory_cards.jsonl`.
