# Build explicitly; Fleet requires the resulting immutable image ID.
# Override base image arguments with digests for a reproducible release build.
ARG NODE_BASE_IMAGE=node:22-bookworm-slim
ARG PYTHON_BASE_IMAGE=python:3.12-slim-bookworm
FROM ${NODE_BASE_IMAGE} AS node
FROM ${PYTHON_BASE_IMAGE}
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && npm install -g @openai/codex@0.144.4 \
 && useradd -m -u 1000 agent
WORKDIR /work
# No source repository, Fleet state, or authentication is included in this image.
