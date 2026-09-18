# Build explicitly; Fleet requires the resulting immutable image ID.
# Override base image arguments with digests for a reproducible release build.
ARG NODE_BASE_IMAGE=node:22-bookworm-slim
ARG PYTHON_BASE_IMAGE=python:3.12-slim-bookworm
FROM ${NODE_BASE_IMAGE} AS node
FROM ${PYTHON_BASE_IMAGE} AS native-helper
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential meson ninja-build pkg-config libcap-dev curl ca-certificates xz-utils
WORKDIR /build
RUN curl --fail --location --output bubblewrap.tar.xz \
      https://github.com/containers/bubblewrap/releases/download/v0.12.0/bubblewrap-0.12.0.tar.xz \
 && echo '9760d007363e3abba7c747489910f9f82d9fca53ba3bd3282e396fa3c97a3314  bubblewrap.tar.xz' | sha256sum -c - \
 && tar -xf bubblewrap.tar.xz
COPY restricted-procfs.py /build/restricted-procfs.py
RUN python /build/restricted-procfs.py bubblewrap-0.12.0/bubblewrap.c \
 && meson setup build bubblewrap-0.12.0 -Dtests=false -Dman=disabled -Dselinux=disabled \
 && ninja -C build
FROM ${PYTHON_BASE_IMAGE}
COPY --from=native-helper /build/build/bwrap /usr/bin/bwrap
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates python3 libcap2 \
 && npm install -g @openai/codex@0.154.0 \
 && useradd -m -u 1000 agent
WORKDIR /work
# No source repository, Fleet state, or authentication is included in this image.
