FROM python:3.12-slim

# Profile contract levels this runtime executes (#75). miragend reads this
# label off the image it will spawn and refuses profiles the runtime cannot
# run. Kept in lockstep with miragen/profile_contract.py by
# tests/test_profile_contract.py::test_dockerfile_label_matches_source.
LABEL io.miragen.profile-contracts="1 2"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /agent

COPY . /build/
# Executor extras + Grok Build CLI are baked into the published image so agent
# containers can run self-harnessed backends without a custom Dockerfile.
# publish.yml builds this on main + v* tags → ghcr.io/.../miragen.
#
# kimi-code is intentionally NOT baked in here: pydantic-ai-slim's "openai"
# extra (needed for openai-responses:* model support) requires openai>=2.29,
# while kimi-code's kosong dependency hard-pins openai<2.15 -- the two can't
# coexist in one environment (see issue #48). No subscription flow exists
# for kimi-code yet (API-key only), so it's excluded from the default image
# for now; still installable standalone via `pip install miragen[kimi-code]`
# in a custom Dockerfile for anyone who needs it and doesn't need OpenAI
# model support in the same container.
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
    "/build[codex,claude-code]"

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
