# t9-env-subagents-pastdev (Sherlock)

- **Task:** 9, `10_1186_s12915_024_01879_0_v1`, official v1.0.0 prompt (HF), unchanged
- **Compared with:** `t9-env-subagents` (Sofia, 5 repeats): same model, effort, limits, task 9 preinstalled environment
  (`BB3_TASK_ENV=auto`), orchestration note and `run_subagent` tool, same Sherlock profile (6 CPU / 32 GB, `normal`)
- **Single modification:** a shared memory for the run, held in past.dev (`src/bixbench3/inspect_harness/run_memory.py`)

## Why

In `t9-env-subagents-b-r2` (score 0.677, $8.88, 3 h 55, 17 sub-agents) each sub-agent starts with an empty context
and re-reads what earlier agents already established: the task environment manifest was inspected by 17 of 17
agents (65 calls), the sample/GSM mapping by 10 (25 calls), the GTF by 8, upstream artifact files by almost all
(101 calls). Two DE artifacts (F7, C10) were rebuilt because a shared convention (gene symbols from the GTF) did not
reach their briefs.

## What the memory does

| Mechanism | Model calls | What |
|---|---|---|
| Capture of inspections | none | every read-only inspection of `/data`, `/opt/task-env` or `/workspace/work/pipeline` by any agent is stored as an `observation` card (exact command and output, up to 1,500 characters) |
| File registry | none | after each sub-agent, the files in its OUTPUT folder are stored as `file` cards (size, line count, first line) |
| Lessons | none extra | each sub-agent ends its report with `LEARNED: [...]` (convention, how_to, pitfall, fact); the harness stores them and replaces the block by a one-line note in the report the orchestrator sees |
| Conventions | one tool call each | the orchestrator records shared conventions with `record_convention` |
| Context pack ("librarian") | none | before each sub-agent, past.dev ranks the run's cards against the brief (`POST /recall`, about 2,000 tokens); all conventions are always added, outside the ranking |
| On demand | one tool call | `recall(query)` for every agent |

Scope: one past.dev audience per run, read by that run's reader identity only; nothing crosses runs. Only the
host-side harness calls past.dev; `PAST_API_KEY` stays out of the sandbox and is redacted from uploaded results.
If past.dev is unavailable the run continues with a local keyword ranking, recorded as `ranking_fallbacks`.

Other changes needed to run it: `sherlock/run_job.sbatch` passes `PAST_API_KEY` from `~/.bixbench3_secrets`;
`gcp_runner.remote_run_script` redacts `past_sk_...` keys.

## Where to find the memory data

`broker_logs/` of each run: `memory_cards.jsonl` (every card, created or updated, with author), `memory_packs.jsonl`
(which cards each sub-agent received), `memory_events.jsonl` (setup, errors), `memory_stats.json` (cards by kind and
author, `inspected_by` per path, packs, recall calls, past.dev errors, fallbacks).

## Measures

Against `t9-env-subagents`: score per artifact, cost, wall time, model calls, sub-agents; distinct agents inspecting
the same path (`inspected_by`); artifacts rebuilt because of an inconsistency.
