# Clean-machine test image for the crews plugin (OSS-TST-3, OSS-POR-3). It holds only what a user
# needs before installing the plugin: Node and Claude Code, git, and uv. There is no Python
# beyond what uv fetches on demand, and no API key or credential of any kind.
FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

# Pinned Claude Code release.
RUN npm install -g @anthropic-ai/claude-code@2.1.287 && npm cache clean --force

# A non-root user with an empty home, like a fresh account.
RUN useradd --create-home --home-dir /opt/tester --shell /bin/bash tester
USER tester
ENV HOME=/opt/tester
ENV PATH=/opt/tester/.local/bin:${PATH}

# Pinned uv from the official installer (installs to ~/.local/bin, no shell profile edits).
RUN curl -LsSf https://astral.sh/uv/0.11.6/install.sh | env UV_NO_MODIFY_PATH=1 sh

WORKDIR /opt/tester
