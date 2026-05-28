# syntax=docker/dockerfile:1.7
#
# Python MCP service image.
#
# Build context is the repo root (see docker-compose.yml) so we can copy
# the pyproject + src tree directly.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        wget \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r mcp && useradd -r -g mcp -d /home/mcp -m -s /bin/bash mcp \
 && mkdir -p /srv/uploads \
 && chown -R mcp:mcp /srv/uploads

WORKDIR /app

# Copy only what we need for the install layer to maximize cache hits.
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip \
 && pip install .

USER mcp

EXPOSE 8080

# server.main() reads MCP_INTERNAL_PORT / RSERVE_* etc. from env.
CMD ["python", "-m", "microbiomedb_mcp.server"]
