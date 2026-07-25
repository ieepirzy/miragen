FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /agent

COPY . /build/
# Executor extras (codex / claude-code / kimi-code) are baked into the published
# image so agent containers can run self-harnessed backends without a custom
# Dockerfile. publish.yml builds this on main + v* tags → ghcr.io/.../miragen.
RUN pip install --no-cache-dir "/build[codex,claude-code,kimi-code]" \
    && adduser --disabled-password --gecos "" agentuser \
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
