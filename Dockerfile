ARG BASE_IMAGE=uhub.service.ucloud.cn/techwu/node:25.9-bookworm-slim
FROM ${BASE_IMAGE}

ARG RUNTIME_USER=node
ARG RUNTIME_GROUP=node
ARG DEBIAN_MIRROR=http://mirrors.aliyun.com/debian
ARG DEBIAN_SECURITY_MIRROR=http://mirrors.aliyun.com/debian-security
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG NPM_REGISTRY=https://registry.npmmirror.com
USER root

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    OPENMONTAGE_PROJECTS_DIR=/data/projects \
    OPENMONTAGE_MCP_HOST=0.0.0.0 \
    OPENMONTAGE_MCP_PORT=8765 \
    XDG_CACHE_HOME=/data/cache

RUN sed -i \
        -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl ffmpeg python3 python3-pip python3-venv tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-reference.txt setup.py ./
RUN python3 -m venv /opt/venv \
    && pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" --upgrade pip setuptools wheel \
    && pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements-reference.txt

COPY remotion-composer/package.json remotion-composer/package-lock.json ./remotion-composer/
RUN cd remotion-composer \
    && npm config set registry "${NPM_REGISTRY}" \
    && npm ci --no-audit --no-fund

COPY --chown=${RUNTIME_USER}:${RUNTIME_GROUP} . .
RUN pip install --no-cache-dir --no-deps -e . \
    && mkdir -p /data/projects /data/music_library /data/cache \
    && chown -R ${RUNTIME_USER}:${RUNTIME_GROUP} /data

USER ${RUNTIME_USER}
VOLUME ["/data/projects", "/data/music_library"]
EXPOSE 8765

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl --fail --silent http://127.0.0.1:8765/healthz >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "openmontage"]
CMD ["mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8765"]
