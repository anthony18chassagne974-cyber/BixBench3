#!/usr/bin/env Rscript
# Prove that every task 11 step runs with the PREINSTALLED packages only.
# Run it with networking disabled, e.g.:
#   docker run --rm --network none bixbench3-sandbox:task11 \
#     micromamba run -n task11 Rscript /opt/task-env/verify_env.R --report /tmp/verify.json
#
# Each step runs on tiny synthetic data (no task data, no ground truth):
#   versions   R / QuasR / Rbowtie / DESeq2 / biomaRt / piano / readxl present (+ pins compared)
#   counting   QuasR qAlign (Rbowtie) + qCount of reads against probe sequences
#   deseq2     DESeq2 Wald test on simulated counts, BH-adjusted results
#   orthologs  biomaRt loads + prestaged Ensembl 100 ortholog table is present and well formed
#   gsea       piano runGSA on a ranked statistic with directional p-values
#   hypergeom  phyper over-representation + p.adjust BH
#   excel      readxl reads an .xlsx workbook
# Exit code 0 only if every step passes.

args <- commandArgs(trailingOnly = TRUE)
report_path <- if ("--report" %in% args) args[which(args == "--report") + 1] else ""
check_network <- "--network" %in% args
pins <- c(R = "4.0.2", QuasR = "1.26.0", DESeq2 = "1.28.1", biomaRt = "2.44.1", piano = "2.4.0")

results <- list()
step <- function(name, expr) {
  t0 <- Sys.time()
  res <- tryCatch({ detail <- force(expr); list(ok = TRUE, detail = detail) },
                  error = function(e) list(ok = FALSE, detail = conditionMessage(e)))
  res$seconds <- round(as.numeric(difftime(Sys.time(), t0, units = "secs")), 1)
  results[[name]] <<- res
  message(sprintf("[verify] %-10s %s  %s", name, if (res$ok) "PASS" else "FAIL", paste(res$detail, collapse = " ")))
}
quiet <- function(pkg) suppressPackageStartupMessages(library(pkg, character.only = TRUE))

step("versions", {
  have <- c(R = paste(R.version$major, R.version$minor, sep = "."),
            sapply(c("QuasR", "Rbowtie", "DESeq2", "biomaRt", "piano", "readxl"),
                   function(p) as.character(packageVersion(p))))
  dev <- names(pins)[have[names(pins)] != pins]
  sprintf("%s%s", paste(names(have), have, sep = "=", collapse = " "),
          if (length(dev)) paste0("  | differs from prompt pins: ", paste(dev, collapse = ",")) else "  | all prompt pins exact")
})

step("counting", {
  quiet("QuasR"); quiet("GenomicRanges")
  set.seed(1)
  wd <- tempfile("quasr_"); dir.create(wd)
  probes <- vapply(1:5, function(i) paste(sample(c("A", "C", "G", "T"), 50, TRUE), collapse = ""), "")
  names(probes) <- sprintf("Gene%d_%d", 1:5, 1000 + 1:5)
  fa <- file.path(wd, "probes.fa")
  writeLines(paste0(">", names(probes), "\n", probes), fa)
  truth <- c(40, 10, 0, 25, 5)
  reads <- unlist(mapply(function(p, n) rep(p, n), probes, truth))
  fq <- file.path(wd, "s1.fastq")
  writeLines(paste0("@r", seq_along(reads), "\n", reads, "\n+\n", strrep("I", 50)), fq)
  sf <- file.path(wd, "samples.txt")
  writeLines(c("FileName\tSampleName", paste0(fq, "\ts1")), sf)
  proj <- qAlign(sf, genome = fa, alignmentsDir = wd, clObj = NULL)
  query <- GRanges(names(probes), IRanges(1, nchar(probes)), strand = "+")
  names(query) <- names(probes)
  counts <- qCount(proj, query, orientation = "any")
  got <- as.integer(counts[names(probes), "s1"])
  stopifnot(identical(got, as.integer(truth)))
  sprintf("probe counts match simulated reads (%s)", paste(got, collapse = ","))
})

step("deseq2", {
  quiet("DESeq2")
  dds <- makeExampleDESeqDataSet(n = 500, m = 6)
  dds <- DESeq(dds, test = "Wald", quiet = TRUE)
  res <- results(dds, alpha = 0.10, pAdjustMethod = "BH")
  stopifnot(all(c("baseMean", "log2FoldChange", "lfcSE", "stat", "pvalue", "padj") %in% colnames(res)))
  sprintf("%d genes, %d with padj < 0.10", nrow(res), sum(res$padj < 0.10, na.rm = TRUE))
})

step("orthologs", {
  quiet("biomaRt")
  tab_path <- "/opt/task-env/reference/ensembl100_mmusculus_hsapiens_orthologs.tsv"
  stopifnot(file.exists(tab_path))
  tab <- data.table::fread(tab_path, sep = "\t", na.strings = "")
  need <- c("mouse_gene_stable_id", "mouse_gene_name", "human_gene_stable_id", "human_gene_name", "orthology_type")
  stopifnot(all(need %in% names(tab)), nrow(tab) > 10000,
            all(grepl("^ENSMUSG", tab$mouse_gene_stable_id)), all(grepl("^ENSG", tab$human_gene_stable_id)))
  probe <- tab[mouse_gene_name == "Trp53"]
  msg <- sprintf("Ensembl 100 table: %d rows, %d mouse genes; Trp53 -> %s",
                 nrow(tab), data.table::uniqueN(tab$mouse_gene_stable_id), paste(unique(probe$human_gene_name), collapse = ","))
  if (check_network) {
    # documents that the live endpoint the prompt names is gone (expected: 301 to the archives help page)
    h <- tryCatch(curlGetHeaders("https://apr2020.archive.ensembl.org/biomart/martservice", redirect = FALSE),
                  error = function(e) conditionMessage(e))
    msg <- paste0(msg, " | live archive: ", trimws(h[1]))
  }
  msg
})

step("gsea", {
  quiet("piano")
  set.seed(2)
  genes <- sprintf("G%03d", 1:300)
  stat <- setNames(rnorm(300), genes)
  stat[1:25] <- stat[1:25] + 3
  gsc <- loadGSC(data.frame(gene = c(genes[1:25], genes[100:140], genes[200:230]),
                            set = rep(c("SET_UP", "SET_NULL1", "SET_NULL2"), c(25, 41, 31))))
  gsa <- runGSA(geneLevelStats = stat, gsc = gsc, nPerm = 200, gsSizeLim = c(5, 500), verbose = FALSE)
  tab <- GSAsummaryTable(gsa)
  stopifnot("p (dist.dir.up)" %in% colnames(tab))
  sprintf("runGSA ok, SET_UP p(up) = %.3g", tab[tab$Name == "SET_UP", "p (dist.dir.up)"])
})

step("hypergeom", {
  universe <- 1000; set_size <- 50; selected <- 80; overlap <- 15
  p <- phyper(overlap - 1, set_size, universe - set_size, selected, lower.tail = FALSE)
  padj <- p.adjust(c(p, 0.2, 0.5), method = "BH")
  sprintf("phyper p = %.2e, BH ok (%d values)", p, length(padj))
})

step("excel", {
  quiet("readxl")
  x <- read_excel(readxl_example("datasets.xlsx"), sheet = 1)
  sprintf("read %d x %d sheet", nrow(x), ncol(x))
})

ok <- all(vapply(results, `[[`, logical(1), "ok"))
if (nzchar(report_path)) {
  jsonlite::write_json(list(passed = ok, network_checked = check_network, steps = results),
                       report_path, auto_unbox = TRUE, pretty = TRUE)
  message("[verify] report: ", report_path)
}
message(if (ok) "[verify] ALL STEPS PASS with preinstalled packages" else "[verify] SOME STEPS FAILED")
quit(status = if (ok) 0 else 1)
