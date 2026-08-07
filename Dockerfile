ARG BASE_IMAGE=uhub.service.ucloud.cn/techwu/node:25.9-bookworm-slim
FROM ${BASE_IMAGE}

ARG RUNTIME_USER=node
ARG RUNTIME_GROUP=node
ARG DEBIAN_MIRROR=http://mirrors.aliyun.com/debian
ARG DEBIAN_SECURITY_MIRROR=http://mirrors.aliyun.com/debian-security
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG CODEX_CLI_VERSION=0.146.0
ARG OPENMONTAGE_IMAGE_REVISION=unknown

LABEL org.opencontainers.image.source="https://github.com/iTechwu/openmontage.dofe.ai" \
      org.opencontainers.image.revision=${OPENMONTAGE_IMAGE_REVISION}
USER root

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    OPENMONTAGE_PROJECTS_DIR=/data/projects \
    OPENMONTAGE_MCP_HOST=0.0.0.0 \
    OPENMONTAGE_MCP_PORT=8765 \
    XDG_CACHE_HOME=/data/cache \
    HYPERFRAMES_NO_TELEMETRY=1 \
    ONNXRUNTIME_NODE_INSTALL_CUDA=skip \
    HYPERFRAMES_BROWSER_PATH=/app/remotion-composer/node_modules/.remotion/chrome-headless-shell/linux64/chrome-headless-shell-linux64/chrome-headless-shell

RUN sed -i \
        -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl ffmpeg python3 python3-pip python3-venv tini unzip \
        libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libcups2 libnspr4 libnss3 \
        libxcomposite1 libxdamage1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-reference.txt setup.py ./
RUN python3 -m venv /opt/venv \
    && pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" --upgrade pip setuptools wheel \
    && pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements-reference.txt

COPY remotion-composer/package.json remotion-composer/package-lock.json ./remotion-composer/
RUN cd remotion-composer \
    && npm config set registry "${NPM_REGISTRY}" \
    && npm ci --no-audit --no-fund \
    && npm install --global --no-audit --no-fund "@openai/codex@${CODEX_CLI_VERSION}" \
    && codex --version | grep -F "codex-cli ${CODEX_CLI_VERSION}" \
    && npm run runtime:browser \
    && mkdir -p "/home/${RUNTIME_USER}/.cache" "/home/${RUNTIME_USER}/.hyperframes" \
    && HOME="/home/${RUNTIME_USER}" npm exec -- hyperframes telemetry disable \
    && HOME="/home/${RUNTIME_USER}" npm run runtime:hyperframes-browser \
    && HOME="/home/${RUNTIME_USER}" npm run runtime:hyperframes >/dev/null \
    && chown -R ${RUNTIME_USER}:${RUNTIME_GROUP} "/home/${RUNTIME_USER}/.cache" "/home/${RUNTIME_USER}/.hyperframes"

COPY --chown=${RUNTIME_USER}:${RUNTIME_GROUP} . .
RUN pip install --no-cache-dir --no-deps -e . \
    && mkdir -p /data/projects /data/music_library /data/cache/remotion-webpack \
    && rm -rf /app/remotion-composer/node_modules/.cache \
    && ln -s /data/cache/remotion-webpack /app/remotion-composer/node_modules/.cache \
    && chown -R ${RUNTIME_USER}:${RUNTIME_GROUP} /data \
    && chmod -R a-w /app

USER ${RUNTIME_USER}
VOLUME ["/data/projects", "/data/music_library"]
EXPOSE 8765

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl --fail --silent http://127.0.0.1:8765/healthz >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "openmontage"]
CMD ["mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8765"]
