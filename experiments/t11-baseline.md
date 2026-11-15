# t11-baseline (Sherlock)

- **Task:** 11, `10.1016_j.crtox.2021.01.003_v1` (1,4-dioxane mouse liver TempO-Seq)
- **Condition:** baseline, upstream Inspect harness (identical to `main`: no diff in `src/`, `configs/`, `uv.lock`)
- **Model:** openai/gpt-5.4-mini, effort max → xhigh; 24 h / 5,000 messages
- **Platform:** Stanford Sherlock, `sherlock/` runner from this branch; **6 CPU / 32 GB per run** (the GCP VM
  shape, n2-custom-6-32768), partition `normal`, AMD EPYC 7502 nodes only (`-C CPU_GEN:RME`)
- **Repeats:** 4 (same code; repeats only reduce noise)
- **Dashboard:** condition `baseline`, owner `sofia`, description "[repeat k/4, 6 CPU/32 GB, Sherlock]"
- **Results:** `gs://bixbench3-sofia-results/10.1016_j.crtox.2021.01.003_v1/openai-gpt-5-4-mini/<timestamp>/`

Harness diff vs upstream: none. This branch only records what was launched.

## History

- **v1 (2026-11-15 06:57 UTC, jobs 52391887/89/93/94): cancelled after 1 h, not valid.** 3 CPU / 16 GB on
  2016 Broadwell nodes; `apt-get` did not work in the sandbox (fakeroot), so agents could not install system
  libraries (e.g. `libuv1-dev` → R `fs` → `shiny` → PIANO); 3 of 4 runs hit the 16 GB limit.
- **v2 (relaunch):** with the fixes in `sherlock/runner` `dcc005e`: working apt, memory view = job limit,
  signals to host processes blocked by `bb3jail`, cancel handling, one CPU type, 6 CPU / 32 GB.
