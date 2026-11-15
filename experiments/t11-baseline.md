# t11-baseline (Sherlock)

- **Task:** 11, `10.1016_j.crtox.2021.01.003_v1` (1,4-dioxane mouse liver TempO-Seq)
- **Condition:** baseline, upstream Inspect harness (identical to `d0e0bbb` / `main`)
- **Model:** openai/gpt-5.4-mini, effort max → xhigh; 24 h / 5,000 messages
- **Platform:** Stanford Sherlock, `sherlock/` runner from this branch; 3 CPU / 16 GB per run, partition `normal`
- **Repeats:** 4 (same code; repeats only reduce noise)
- **Dashboard:** condition `baseline`, owner `sofia`, description "[repeat k/4, 3 CPU/16 GB, Sherlock]"
- **Results:** `gs://bixbench3-sofia-results/10.1016_j.crtox.2021.01.003_v1/openai-gpt-5-4-mini/<timestamp>/`

Harness diff vs upstream: none. This branch only records what was launched.
