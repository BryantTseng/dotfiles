# Image for one Claude Code kanban agent run (`BRIDGE_AGENT_PROVIDER=claude`,
# `BRIDGE_CLAUDE_MODE=docker`). The bridge starts a fresh container per run with only
# the task directory (`/work` or worktree path) and that task's Claude state mounted.
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl git ripgrep jq unzip less procps \
      python3 python3-venv nodejs npm ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Optional corporate TLS CA bundle (gitignored: place PEM at agent/ca-bundle.pem for Netskope/Zscaler).
COPY Dockerfile ca-bundle.pe[m] /tmp/ca-ctx/
RUN if [ -s /tmp/ca-ctx/ca-bundle.pem ]; then \
      cp /tmp/ca-ctx/ca-bundle.pem /usr/local/share/ca-certificates/corp-ca.crt \
      && update-ca-certificates; \
    fi && rm -rf /tmp/ca-ctx
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

# GitHub CLI
ARG GH_VERSION=2.62.0
RUN arch=$(dpkg --print-architecture) \
 && curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz" \
    | tar -xz -C /tmp && mv /tmp/gh_*/bin/gh /usr/local/bin/gh && rm -rf /tmp/gh_*

# AWS CLI v2
RUN arch=$(uname -m) \
 && curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${arch}.zip" -o /tmp/aws.zip \
 && unzip -q /tmp/aws.zip -d /tmp && /tmp/aws/install && rm -rf /tmp/aws /tmp/aws.zip

# uv (Python tooling) and Azure CLI (setuptools<81 + python wrapper required)
COPY --from=ghcr.io/astral-sh/uv:0.8.4 /uv /uvx /usr/local/bin/
ENV UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin
RUN uv tool install --python 3.11 --with 'setuptools<81' 'azure-cli>=2.70' \
 && printf '#!/bin/sh\nexec /opt/uv-tools/azure-cli/bin/python -m azure.cli "$@"\n' > /usr/local/bin/az \
 && chmod +x /usr/local/bin/az

RUN useradd -m -u 1000 agent
USER agent
ENV PATH="/home/agent/.local/bin:${PATH}" \
    CLAUDE_CONFIG_DIR=/home/agent/.claude \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_AUTOUPDATER=1
RUN curl -fsSL https://claude.ai/install.sh | bash && claude --version
WORKDIR /work
