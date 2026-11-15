# t11-v4-dag-orchestrator (Sherlock)

- **Task:** 11, `10.1016_j.crtox.2021.01.003_v1`; compare with `exp/t11-baseline` v2 (same platform, 6 CPU / 32 GB, AMD EPYC 7502)
- **Single modification:** the agent can start sub-agents and is asked to work as a dependency-ordered orchestrator.
  - `src/bixbench3/inspect_harness/tools/subagent.py`: `run_subagent(role, brief)` starts a fresh `react` agent
    (empty context, own bash session, python, text editor, web access requests; same model and effort).
    Depth ≤ 2 (orchestrator → artifact builder → brick worker), ≤ 200 sub-agents per sample,
    message limit 800 (depth 1) / 300 (depth 2) per sub-agent. A failing sub-agent returns a note, never ends the run.
  - `src/bixbench3/inspect_harness/condition_note.py`: orchestration note appended **after** the original task prompt
    (which is unchanged and stated to take precedence); wired in `tasks.py` setup like v3a.
  - `solvers.py`: `run_subagent(depth=1)` added to the main agent's tools.
- **No change** to data, outputs, grading, judge, run script or upload.

## Where to find the number of sub-agents

| Where | What |
|---|---|
| `gs://bixbench3-sofia-results/<paper>/<model>/<ts>/broker_logs/subagents_summary.json` | final totals: `total_subagents`, `finished`, `failed`, `by_role`, `by_depth` |
| `.../broker_logs/subagents.jsonl` | one line per sub-agent start/end (role, depth, status, times) |
| Inspect log (`inspect_logs/*.eval`), sample store key `bixbench3_subagents` | same list, inside the eval log |
| Live dashboard, run page | per-agent table (one row per `subagent-<n>-<role>`), "active agents" chart; `n_subagents` field in `latest.json` / `history.jsonl` |

Tested locally with Inspect's mock model: nested sub-agents (depth 2), reports returned to the caller,
depth-2 workers have no `run_subagent`, message-limit failure returned as text, counters written in all three
places; repo unit tests 157 passed.
