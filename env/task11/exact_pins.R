#!/usr/bin/env Rscript
# Upgrade the conda env to the EXACT versions pinned by the task 11 prompt.
# Needs network to bioconductor.org at IMAGE BUILD time only (never during a run).
#
#   micromamba run -n task11 Rscript env/task11/exact_pins.R [--manifest /opt/task-env/MANIFEST.json]
#
# Every step is attempted independently; failures are recorded, not fatal, so the
# image still builds with the closest versions and the deviation is documented.

args <- commandArgs(trailingOnly = TRUE)
manifest_path <- if ("--manifest" %in% args) args[which(args == "--manifest") + 1] else "/opt/task-env/MANIFEST.json"
options(repos = c(CRAN = "https://cloud.r-project.org"), timeout = 600, Ncpus = parallel::detectCores())

log <- list()
attempt <- function(label, expr) {
  res <- tryCatch({ force(expr); "ok" }, error = function(e) paste("failed:", conditionMessage(e)))
  message(sprintf("[exact_pins] %-40s %s", label, res))
  log[[label]] <<- res
}

# Bioconductor 3.11 is the release paired with R 4.0 that ships DESeq2 1.28.1 and biomaRt 2.44.1
attempt("BiocManager version 3.11", BiocManager::install(version = "3.11", ask = FALSE, update = FALSE))
attempt("DESeq2 1.28.1 (Bioc 3.11)", BiocManager::install("DESeq2", ask = FALSE, update = FALSE, force = TRUE))
attempt("biomaRt 2.44.1 (Bioc 3.11)", BiocManager::install("biomaRt", ask = FALSE, update = FALSE, force = TRUE))

# QuasR 1.26.0 belongs to Bioconductor 3.10 (R 3.6). Try a source build against
# Rbowtie 1.26.x under R 4.0.2; keep 1.28.0 if it does not build or load.
quasr_url <- "https://bioconductor.org/packages/3.10/bioc/src/contrib/QuasR_1.26.0.tar.gz"
rbowtie_url <- "https://bioconductor.org/packages/3.10/bioc/src/contrib/Rbowtie_1.26.0.tar.gz"
lib_backup <- tempfile("quasr_backup_")
dir.create(lib_backup)
attempt("QuasR 1.26.0 from Bioc 3.10 source", {
  install.packages(rbowtie_url, repos = NULL, type = "source")
  install.packages(quasr_url, repos = NULL, type = "source")
  stopifnot(as.character(packageVersion("QuasR")) == "1.26.0")
  suppressPackageStartupMessages(library(QuasR))
})
if (!identical(log[["QuasR 1.26.0 from Bioc 3.10 source"]], "ok")) {
  attempt("QuasR fallback to 1.28.0 + Rbowtie 1.28", BiocManager::install(c("Rbowtie", "QuasR"), ask = FALSE, update = FALSE, force = TRUE))
}

pins <- c(R = "4.0.2", QuasR = "1.26.0", DESeq2 = "1.28.1", biomaRt = "2.44.1", piano = "2.4.0")
installed <- c(R = paste(R.version$major, R.version$minor, sep = "."),
               sapply(setdiff(names(pins), "R"), function(p) tryCatch(as.character(packageVersion(p)), error = function(e) NA_character_)))
deviations <- names(pins)[is.na(installed[names(pins)]) | installed[names(pins)] != pins]
out <- list(
  task = "11", built_at = format(Sys.time(), tz = "UTC", usetz = TRUE),
  pins = as.list(pins), installed = as.list(installed[names(pins)]),
  deviations = lapply(deviations, function(p) list(package = p, requested = pins[[p]], installed = installed[[p]])),
  exact_pins_log = log
)
dir.create(dirname(manifest_path), recursive = TRUE, showWarnings = FALSE)
jsonlite::write_json(out, manifest_path, auto_unbox = TRUE, pretty = TRUE)
message("[exact_pins] manifest written to ", manifest_path)
if (length(deviations)) message("[exact_pins] DEVIATIONS: ", paste(deviations, collapse = ", "))
