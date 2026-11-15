# Agent notes

## Cloud run outputs

When launching `bixbench3 run`, always pass:

```text
--results-bucket gs://phdval-runner-workspaces/launch-test
```

Campaigns for Opus 5, GPT-5.6 Sol, and Kimi K3 used this prefix. Without it, agent artifacts, broker logs, and extra run files are deleted with the VM. `complete` still copies `grade.json` / `judge.json` locally, but the full bundle is lost.

Project for these evals: `edsci-evals`.

## This fork's experiments

For user-owned experiments, use the user's explicitly selected GCP project and private result bucket; the historical edsci project/bucket above are not access grants or defaults for this fork. See ADAPTED_RUNNER.md. Keep separate run records under iterations/<version>/.
