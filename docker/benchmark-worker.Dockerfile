# Product-owned adapter runtime; the task's /app is an alias for Fleet's /work.
# Supply an immutable image produced from isolated-worker.Dockerfile.
ARG ISOLATED_WORKER_IMAGE
FROM ${ISOLATED_WORKER_IMAGE}
RUN test ! -e /app && ln -s /work /app
