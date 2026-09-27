# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Version of the OpenWiki CLI baked into the image. Bump it and rebuild to
# upgrade OpenWiki without touching the service code:
#   docker compose build --build-arg OPENWIKI_VERSION=x.y.z
ARG OPENWIKI_VERSION=0.6.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_MAJOR=22

# git: cloning repositories; curl: container healthcheck; Node 22: OpenWiki.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg git \
    && curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/install-openwiki.sh /usr/local/bin/install-openwiki.sh
RUN chmod +x /usr/local/bin/install-openwiki.sh \
    && OPENWIKI_VERSION="${OPENWIKI_VERSION}" /usr/local/bin/install-openwiki.sh \
    && node --version

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

# Repositories mounted or cloned into the container may be owned by another
# user; git only honours safe.directory from system/global config.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app \
    && git config --system --add safe.directory '*'

USER appuser

ENV DATA_DIR=/data \
    OPENWIKI_CONFIG_DIR=/data/.openwiki-config \
    OPENWIKI_TELEMETRY_DISABLED=1

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
