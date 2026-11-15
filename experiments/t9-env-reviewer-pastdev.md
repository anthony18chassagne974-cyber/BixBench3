# t9-env-reviewer-pastdev (Sherlock)

- **Task:** 9, `10_1186_s12915_024_01879_0_v1`, official v1.0.0 prompt; preinstalled task 9 environment
- **Compared with:** `t9-env-reviewer` (single research agent + format reviewer, best task 9 condition so far:
  mean 0.830 over 5 runs, scores 0.932 / 0.677 / 0.932 / 0.677 / 0.934): same settings and number of repeats
- **Single modification (variant B):** the format reviewer remembers the tables it already accepted in this run,
  through past.dev, and checks cross-artifact format consistency.

## Why

The reviewer sees one table at a time, so it cannot see a format inconsistency between two tables (gene identifiers
with a version suffix in one DE table and without in another, different symbol sources, different label spellings).

## What changes

- After a table is accepted (including the third, unreviewed submission), its computed format summary (shape, header,
  head/tail rows, row-key checks, sampled identifiers; up to 1,900 characters) is stored as an `artifact` card in a
  past.dev audience scoped to this run. Accepting the same artifact again replaces its card. Rejected tables are not stored.
- When a table is submitted, past.dev ranks the accepted tables against its specification and summary
  (`POST /recall`); the reviewer receives the three most similar (never the artifact itself), up to 2,500 characters
  each, in a section "Artifacts of this run you already accepted (most similar first; for format consistency only)".
- Reviewer instructions: one added bullet, still format only. The same kind of column must use the same format as in
  those artifacts; never require identical values, genes or row counts.
- Agent notice: one added bullet telling the agent the reviewer checks consistency with already accepted tables.
- Nothing else changes: the research agent has no new tool and no memory; the reviewer is still one model call
  without tools; two rejections at most; reviewer or memory errors never block an artifact.

## Files

`src/bixbench3/inspect_harness/review_memory.py` (new), `run_memory.py` (past.dev client and storage, same file as
`exp/t9-env-subagents-pastdev`), `artifact_review.py` (optional `related` / `record` hooks, prompt additions),
`tasks.py` (`review_memory_setup()` after `review_setup()`), `sherlock/run_job.sbatch` (passes `PAST_API_KEY`),
`gcp_runner.py` (redacts `past_sk_...`).

## Memory scope and records

One past.dev audience per run: every run starts empty and cannot read another run. Only the host-side harness calls
past.dev; the key never enters the sandbox. Without `PAST_API_KEY` (or if past.dev fails) a local keyword ranking is
used and recorded. `broker_logs/`: `memory_cards.jsonl` (accepted-table cards), `memory_packs.jsonl` (which tables each
review received), `memory_stats.json` (`pastdev` true/false, fallbacks, errors). The sample store `review_log` records
the related artifacts of each review.

## Measures

Score per artifact (does the 0.677 mode disappear?), number of rejections and their reasons, consistency rejections
(feedback citing an accepted artifact), cost and time, against `t9-env-reviewer`.
