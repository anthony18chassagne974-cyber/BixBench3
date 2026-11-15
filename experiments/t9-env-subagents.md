# t9-env-subagents (Sherlock)

- **Task:** 9, `10_1186_s12915_024_01879_0_v1` (REST loss in T98G / HEK293, Tag-Seq), official v1.0.0 prompt (HF), unchanged
- **Model:** openai/gpt-5.4-mini, effort max → xhigh; 24 h / 5,000 messages
- **Platform:** Sherlock, 6 CPU / 32 GB (GCP VM shape), AMD EPYC 7502 nodes, partition `normal`
- **Repeats:** 5 (night batch 2026-11-15); compare the three t9 conditions with each other only
- **Condition:** t9-env + sub-agents: the `run_subagent` tool and the orchestration note appended after the unchanged task prompt (harness from `exp/t11-v4-dag-orchestrator`, see `experiments/t11-v4-dag-orchestrator.md` for where the sub-agent count is stored). Same task 9 image as t9-env (`BB3_SANDBOX_SIF`).
