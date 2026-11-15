# Shared process memory policy (experiment v2a, past.dev)

This policy governs what the BixBench3 harness may store in, and read from, the team's
past.dev project. The implementation is `src/bixbench3/inspect_harness/pastdev_memory.py`.

## Purpose

Let a run benefit from *process* knowledge gathered by earlier runs: how the environment
behaves, which installations fail and how they were fixed, which commands work. It is not
a store of benchmark answers.

## Who can access it

| Actor | Access |
| --- | --- |
| Harness on the run VM host | Reads before the analyst starts; writes when the analyst stops. Uses `PAST_API_KEY` from the environment. |
| Analyst agent in the sandbox | No access to past.dev and no key. It only sees `work/process_memory.md` and writes `work/process_notes.md`. |
| Team members | Project key holders; may inspect and delete items through the past.dev app or API. |

The key is a past.dev *project* key (`past_sk_...`). It is passed to the VM with the provider
keys (`BIXBENCH3_EXTRA_ENV_KEYS=PAST_API_KEY`), is redacted from uploaded results, is never
printed, and is never mounted into the sandbox container.

## Memory schema

Every item is stored with these fields (the text in `content`, the rest as metadata):

`project`, `version`, `run_id`, `agent_role`, `memory_type`, `title`, `content`, `source`,
`leakage_level`, `created_at`.

- `project` is always `bixbench3`; `leakage_level` is always `process_only`.
- `memory_type` is one of: `setup_failure_fix`, `package_version_note`, `command_worked`,
  `command_failed`, `fallback_decision`, `process_pitfall`, `validated_helper_script`,
  `validator_finding`, `run_summary`.
- The item id is a hash of project, type, title and content, so identical notes are stored once.

## What may be stored

Validated helper scripts, setup failures and fixes, package and version notes, process-level
task pitfalls, run summaries, validator findings, accepted or rejected fallbacks, and commands
that worked or failed.

## What is never stored

- API keys, tokens, passwords or any other secret.
- Ground truth, reference outputs, or anything derived from the grader: scores, expected row
  counts, expected values, grader or judge files.
- Result values from the analysis itself: p-values, fold changes, counts of significant genes
  or enriched pathways.

## How this is enforced

1. The agent is told to write process notes only, under fixed headings.
2. Before writing, the harness drops any item that matches a secret pattern or mentions grading
   or reference material, and removes individual lines that state result values.
3. Before showing recalled memory to the agent, the harness keeps only items whose metadata
   carries `project = bixbench3` and `leakage_level = process_only`, and re-applies the same
   secret and grading filter.
4. Items are capped at 4,000 characters and 20 items per run; the file shown to the agent is
   capped at 12,000 characters.
5. The harness records in the run's Inspect log (`store.pastdev_memory`) which items were read,
   written and rejected.

The filter is pattern-based. It reduces the risk of leakage but does not prove its absence:
review the stored items after each run and delete anything that should not be there.

## Failure behaviour

If past.dev is unreachable or the key is missing, the run continues without memory and the
failure is recorded. Memory can never block or fail a run.

## Deletion

Delete one item with `DELETE /api/v1/data-points/{sourceId}`. To reset the memory between
experimental conditions, delete all items listed in the runs' `written` records.
