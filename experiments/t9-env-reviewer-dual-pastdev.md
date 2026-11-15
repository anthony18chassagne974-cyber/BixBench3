# t9-env-reviewer-dual-pastdev (Sherlock)

- **Task:** 9, `10_1186_s12915_024_01879_0_v1`, official v1.0.0 prompt; preinstalled task 9 environment
- **Compared with:** `t9-env-reviewer` (one format reviewer; mean 0.830 over 5 runs) and `t9-env-reviewer-pastdev`
  (one reviewer that sees the accepted tables): same settings and number of repeats
- **Modification:** two reviewers check every table, in parallel, and both must accept it.

## The two reviewers

| | Local reviewer | Consistency reviewer |
|---|---|---|
| Instructions | exactly the `t9-env-reviewer` ones | the same, plus a consistency check and a NOTES block |
| Sees | task prompt, this table's specification and computed summary, its earlier feedback, the agent's note | the same, plus its rolling memory of the run |
| Memory | none: a fresh call for each table | rolling past.dev memory, updated after every review |

Rolling memory of the consistency reviewer (one past.dev audience per run):
- **conventions**: after each verdict it lists at most 3 format conventions the table establishes or confirms
  (`NOTES:` block, formats only, never values or gene lists); all of them are shown in every later review, outside the
  ranking;
- **accepted tables**: the computed summary of every accepted table; past.dev ranks them against the submitted table and
  the three most similar are shown (never the table itself);
- **earlier reviews**: every consistency review (verdict and bullets); the three most relevant reviews of other tables
  are shown.

## Decision rule

- Accepted only if every reviewer that answered accepts. Feedback to the agent contains both answers, labelled
  "Local reviewer (this table only)" and "Consistency reviewer (run memory)".
- If one reviewer fails, the other decides (recorded as "only the ... reviewer answered"); if both fail, the table is
  accepted, as in `t9-env-reviewer`.
- Unchanged: two rejections at most per table (submission 3 accepted without review), the final submit check,
  `METHODS.md` not reviewed, the agent has no new tool. Its notice says two reviewers check every table.
- Cost: two reviewer calls per submission instead of one (each one model call without tools).

## Files

`artifact_review.py` (`MEMORY_REVIEWER_SYSTEM`, `_dual_review`, optional `related` / `record` / `note_review` hooks;
without them the original single reviewer runs), `review_memory.py` (`memory_reviewer_context`, `note_review`,
`record_accepted`, `review_memory_setup`), `run_memory.py` (past.dev client and storage), `tasks.py`,
`sherlock/run_job.sbatch` (passes `PAST_API_KEY`), `gcp_runner.py` (redacts `past_sk_...`).
`tests/unit/test_artifact_review.py`: the end-to-end mock-model test now provides two reviewer answers per submission.

## Records

`broker_logs/memory_cards.jsonl` (tables, reviews, conventions), `memory_packs.jsonl` (what each consistency review
received), `memory_stats.json` (`pastdev` true/false, fallbacks, errors); `review_log` in the sample store lists the
related tables and reviews of each submission; `work/artifact_review.md` holds both reviewers' answers.

## Measures

Score per artifact (does the 0.677 mode disappear?), rejections by reviewer and their reasons, conventions noted,
cost and time, against `t9-env-reviewer` and `t9-env-reviewer-pastdev`.
