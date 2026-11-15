# Task 11 environment layer on top of the BixBench3 sandbox image.
#
#   docker build --build-arg BASE=bixbench3-sandbox:1.0.0 \
#                --build-arg EXACT_PINS=1 \
#                -t bixbench3-sandbox:1.0.0-task11env -f env/task11/Dockerfile .
#   docker run --rm --network none bixbench3-sandbox:1.0.0-task11env \
#                micromamba run -n task11 Rscript /opt/task-env/verify_env.R
#
# The analyst agent then finds R 4.0.2 + the task's packages in the conda env `task11`.
ARG BASE=bixbench3-sandbox:1.0.0
FROM ${BASE}

ARG EXACT_PINS=1
USER root
COPY env/task11/environment.yml env/task11/exact_pins.R env/task11/verify_env.R env/task11/fetch_ensembl100_orthologs.py /opt/task-env/

RUN micromamba create -y -f /opt/task-env/environment.yml \
    && micromamba clean -a -y

# Ensembl 100 mouse->human orthologs from the static release-100 BioMart dump
# (the live Ensembl 100 BioMart archive is retired: 301 -> /help/articles/archives).
RUN python3 /opt/task-env/fetch_ensembl100_orthologs.py --out /opt/task-env/reference

# Exact prompt pins (DESeq2 1.28.1, biomaRt 2.44.1, QuasR 1.26.0 attempt). Network needed at build time only.
RUN if [ "$EXACT_PINS" = "1" ]; then \
        micromamba run -n task11 Rscript /opt/task-env/exact_pins.R --manifest /opt/task-env/MANIFEST.json; \
    else \
        micromamba run -n task11 Rscript -e 'jsonlite::write_json(list(task="11", exact_pins="skipped", installed=as.list(sapply(c("QuasR","DESeq2","biomaRt","piano"), function(p) as.character(packageVersion(p))))), "/opt/task-env/MANIFEST.json", auto_unbox=TRUE, pretty=TRUE)'; \
    fi \
    && micromamba env export -n task11 --explicit > /opt/task-env/task11.lock.txt \
    && micromamba run -n task11 Rscript /opt/task-env/verify_env.R --report /opt/task-env/verify_build.json

WORKDIR /workspace
