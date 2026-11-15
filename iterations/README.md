# Experiment iterations

| Iteration | Task | CPU / RAM | Official score | Agent time | Estimated analyst API cost |
|---|---:|---|---:|---:|---:|
| [First baseline](baseline-task11/) | 11 | 8 vCPU / 32 GB | 0.589201 | 103 min 45 s | $3.023 |
| [6-vCPU baseline](baseline-task11-6vcpu/) | 11 | 6 vCPU / 32 GB | 0.678978 | 107 min 05 s | $2.843 |

Costs exclude judges, compute, storage and setup, and are not verified invoices.
These are single stochastic agent runs with different trajectories, not a causal
experiment establishing CPU's effect on scientific score. Both use the full task,
GPT-5.4 mini xhigh and the original scientific harness/prompt. The 6-vCPU run also
has live monitoring and overlapped with a separate task 9 run.

Each iteration includes its own Inspect log, export, results and provenance.
See [the shared setup](../TEAM_SETUP.md) for launching new experiments.
