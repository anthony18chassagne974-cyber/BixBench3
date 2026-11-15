# t9-env (Sherlock)

- **Task:** 9, `10_1186_s12915_024_01879_0_v1` (REST loss in T98G / HEK293, Tag-Seq), official v1.0.0 prompt (HF), unchanged
- **Model:** openai/gpt-5.4-mini, effort max → xhigh; 24 h / 5,000 messages
- **Platform:** Sherlock, 6 CPU / 32 GB (GCP VM shape), AMD EPYC 7502 nodes, partition `normal`
- **Repeats:** 5 (night batch 2026-11-15); compare the three t9 conditions with each other only
- **Condition:** t9-baseline + preinstalled task 9 environment (`env/task9`: HISAT2 2.2.1, Subread 2.0.1, DESeq2 1.30.1, clusterProfiler 3.18.1, org.Hs.eg.db 3.12.0, GO.db 3.12.1, tag-based_RNAseq; first on PATH). Run with `BB3_SANDBOX_SIF=$SCRATCH/bixbench3/images/bixbench3-sandbox_1.0.0-task9env.sif` (sha256 recorded in each run's `sherlock/sandbox_image.json`). Harness and prompt unchanged.
