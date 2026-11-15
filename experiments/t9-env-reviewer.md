# t9-env-reviewer

- **Task:** 9, `10_1186_s12915_024_01879_0_v1` (REST loss in T98G / HEK293, Tag-Seq), official v1.0.0 prompt (HF); the harness appends an "Artifact Review" section
- **Model:** openai/gpt-5.4-mini, effort max → xhigh; 24 h / 5,000 messages (the reviewer uses the same model and counts toward the same run)
- **Base:** `exp/t9-env` (preinstalled task 9 environment, same SIF and platform); compare with t9-baseline, t9-env and t9-env-subagents only
- **Condition:** single research agent + format reviewer on every required artifact (`src/bixbench3/inspect_harness/artifact_review.py`).
  - The agent writes each table under `./work` and calls `submit_artifact(artifact_id, path, note)`.
  - The reviewer is one model call with no tools. It reads only the ReAct system prompt, the task prompt, the spec of the submitted artifact and a computed format summary of the file (shape, header, delimiter, first 5 / last 3 rows, row-key uniqueness and emptiness, issues such as ragged rows or CRLF, and a seeded random sample of ceil(sqrt(n)/10) row identifiers (min 5) and columns (min 3) with example values). It judges format only and has no reference answers.
  - Accept → the harness copies the file to its required path under `./outputs`. Reject → written feedback to the agent, who fixes the file or argues in `note`. Two rejections at most per artifact: submission 3 is accepted without review. A reviewer error accepts the file.
  - `METHODS.md` is not reviewed and is written directly to `./outputs`.
  - The sandbox runs as root, so direct writes of tables to `./outputs` cannot be blocked. The first final `submit()` lists artifacts that are missing, never submitted or changed after acceptance; the next `submit()` is accepted.
  - Reviews: `work/artifact_review.md` and `review_log` in the sample store.
