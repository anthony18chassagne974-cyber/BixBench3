# Task 9 preinstalled environment

Task 9: REST loss in T98G / HEK293 cells, Tag-Seq (28 single-end FASTQ), 14 artifacts.

## Can it run without internet? Yes, with this environment

- **Data**: FASTQ, metadata, GRCh38 primary assembly FASTA and GENCODE v38 GTF are all
  staged in `./data`. Gene symbols can come from the GTF (`gene_name`), offline.
- **Only method URL in the task's network allowlist**: `https://github.com/z0on/tag-based_RNAseq`
  (`tagseq_clipper.pl`). `build.sh` clones it at image build time, records the commit in
  `MANIFEST.json` (`TAGSEQ_REF=<commit>` to pin a specific one).
- **STRING v12**: interpretation only, no artifact depends on it.

## Prompt pins vs. binaries (bioconda, checked 2026-11-15)

| Prompt | Binary | Env |
|---|---|---|
| HISAT2 v2.2.1 | `hisat2=2.2.1` | `task9-tools` |
| featureCounts v2.0.1 | `subread=2.0.1` | `task9-tools` |
| DESeq2 v1.30.1 | `bioconductor-deseq2=1.30.1` (R 4.0) | `task9` |
| clusterProfiler v3.18.1 | `bioconductor-clusterprofiler=3.18.1` (R 4.0) | `task9` |
| FastQC, cutadapt | latest | `task9-tools` |

All pins are mutually consistent (Bioconductor 3.12 / R 4.0) — unlike task 11.

## The one real uncertainty: GO annotation version

The prompt pins clusterProfiler 3.18.1 but **not** `org.Hs.eg.db` / `GO.db`. GO
over-representation depends on them twice: gene→GO associations, and clusterProfiler's
default background (all annotated genes). We use the same Bioconductor 3.12 release
(`org.Hs.eg.db` 3.12.0, `GO.db` 3.12.1); the paper (2024) may have used newer annotations,
so GO tables can differ whatever the harness. The embedded GO source dates are written
to `MANIFEST.json` (`go_source_dates`) — report them in METHODS.md.

## Files

| File | Role |
|---|---|
| `environment.yml` | R 4.0 env `task9`: DESeq2, clusterProfiler, org.Hs.eg.db, GO.db, rtracklayer |
| `tools.yml` | CLI env `task9-tools`: HISAT2, Subread, samtools, cutadapt, FastQC, perl |
| `build.sh` | create both envs, clone tag-based_RNAseq, lock files, verification + MANIFEST |
| `verify_env.sh` | synthetic genome/GTF/reads: FastQC, cutadapt (prompt options), HISAT2 `--rna-strandedness F`, featureCounts `-s 1` (and `-s 2` ≈ 0), DESeq2, rtracklayer, enrichGO (BP, BH) offline |
| `path.env` | PATH entries for `/opt/task-env/bin` |

Not included on purpose: a prebuilt HISAT2 GRCh38 index (~1 h, ~8 GB RAM to build from
the staged FASTA). Pre-building it would save run time but is a separate, declared change.

Build / run: see [`env/README.md`](../README.md).
