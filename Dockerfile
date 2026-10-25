# Public sources only; no internal image layers or owner workspace content.
FROM node:24.22.0-bookworm-slim@sha256:91acba91b842bec0bf7ea6f5e09c644c7ccad72c4996ed9616d861d1c7cd60d4 AS node
FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="Pi chat runtime" \
      org.opencontainers.image.source="https://github.com/ssube/pi-chat" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/opt/pi-chat \
    PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME=/state/home \
    PI_CODING_AGENT_DIR=/state/pi \
    AGENT_BRIDGE_PROFILE=/config/profile.json \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    PLAYWRIGHT_BROWSERS_PATH=/state/playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git libstdc++6 tini tmux && \
    rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY runtime/package.json runtime/package-lock.json /opt/pi-runtime/
RUN npm ci --prefix /opt/pi-runtime --cache /tmp/npm-cache --omit=dev --ignore-scripts --no-audit --no-fund && \
    rm -rf /tmp/npm-cache && \
    ln -s /opt/pi-runtime/node_modules/.bin/pi /usr/local/bin/pi && \
    pi --version | grep -Fx '0.99.1'
COPY runtime/requirements.lock /opt/pi-chat/runtime/requirements.lock
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --require-hashes -r /opt/pi-chat/runtime/requirements.lock

COPY extensions/package.json extensions/package-lock.json /opt/pi-chat/extensions/
RUN npm ci --prefix /opt/pi-chat/extensions --cache /tmp/npm-cache --omit=dev --ignore-scripts --no-audit --no-fund && \
    rm -rf /tmp/npm-cache
COPY bridge /opt/pi-chat/bridge
COPY tools /opt/pi-chat/tools
COPY skills /opt/pi-chat/skills
COPY extensions /opt/pi-chat/extensions
COPY runtime /opt/pi-chat/runtime
COPY LICENSE /opt/pi-chat/LICENSE

RUN groupadd --gid 1000 agent && \
    useradd --uid 1000 --gid 1000 --home-dir /state/home --no-create-home agent && \
    install -d -o agent -g agent -m 0700 /state /state/home /state/pi /state/bridge && \
    install -d -o agent -g agent -m 0755 /workspace && \
    python -c 'import aiohttp, discord, dotenv, playwright, supervisor; print("runtime imports OK")'

USER 1000:1000
WORKDIR /workspace
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/opt/venv/bin/python", "/opt/pi-chat/runtime/healthcheck.py"]
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/venv/bin/python", "/opt/pi-chat/runtime/entrypoint.py"]
CMD ["/opt/venv/bin/supervisord", "-c", "/opt/pi-chat/runtime/supervisord.conf"]
