# Layer on an operator-selected Debian-based Codex/Python runtime.
# Headless shell avoids Chromium's desktop singleton socket inside proxy-only sandboxing.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends chromium-headless-shell \
    && rm -rf /var/lib/apt/lists/*
USER 1000:1000
