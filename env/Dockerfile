# Task environment layer on top of the BixBench3 sandbox image (Docker / GCP).
#   docker build --build-arg BASE=bixbench3-sandbox:1.0.0 --build-arg TASK=task9 \
#                -t bixbench3-sandbox:1.0.0-task9env -f env/Dockerfile .
#   docker run --rm --network none bixbench3-sandbox:1.0.0-task9env bash /opt/task-env/verify_env.sh
# (task11: verify with `micromamba run -n task11 Rscript /opt/task-env/verify_env.R`)
ARG BASE=bixbench3-sandbox:1.0.0
FROM ${BASE}
ARG TASK
ARG EXACT_PINS=1
USER root
COPY env/${TASK}/ /opt/task-env/
COPY env/link_path.sh /opt/task-env/link_path.sh
RUN test -n "$TASK" && EXACT_PINS=$EXACT_PINS bash /opt/task-env/build.sh && bash /opt/task-env/link_path.sh
# plain `Rscript`, `hisat2`, `featureCounts`... resolve to the pinned task env (python3 stays the system one)
ENV PATH=/opt/task-env/bin:$PATH
WORKDIR /workspace
