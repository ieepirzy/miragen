FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /agent

COPY . /build/
# Executor extras + Grok Build CLI are baked into the published image so agent
# containers can run self-harnessed backends without a custom Dockerfile.
# publish.yml builds this on main + v* tags → ghcr.io/.../miragen.
#
# Split into separate RUN layers (issue #45) so a failure in any one step
# (apt/curl, Grok CLI install, either pip install, or user setup) is
# attributable to that specific step instead of one opaque mega-layer.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN set -o pipefail \
    && curl -fsSL https://x.ai/cli/install.sh -o /tmp/grok-install.sh \
    && GROK_BIN_DIR=/usr/local/bin bash /tmp/grok-install.sh \
    && rm -f /tmp/grok-install.sh \
    && command -v grok

RUN pip install --no-cache-dir \
    /build/packages/grok-build-client

RUN pip install --no-cache-dir \
    "/build[codex,claude-code,kimi-code]"

RUN adduser --disabled-password --gecos "" agentuser \
    && chown agentuser /agent

USER agentuser

# Workspace (agent.yaml + tools.py) is mounted at runtime — nothing baked in.
# Set AGENT_PROFILE to a path relative to /agent, e.g. agent.yaml (default).
ENV AGENT_PROFILE=agent.yaml \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health').raise_for_status()"

CMD ["miragen", "run"]
