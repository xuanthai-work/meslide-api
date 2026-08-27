# syntax=docker/dockerfile:1.7

FROM python:3.11-slim-trixie AS fastapi-builder

WORKDIR /app/servers/fastapi

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN python -m venv --without-pip /opt/venv \
    && pip install --no-cache-dir uv

COPY servers/fastapi/pyproject.toml servers/fastapi/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
    && uv pip install --python /opt/venv/bin/python -r /tmp/requirements.txt

COPY servers/fastapi /app/servers/fastapi
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python --no-deps .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python \
    "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
ENV HF_HOME=/root/.cache/huggingface \
    GSLIDE_FASTEMBED_ICON_CACHE_DIR=/root/.cache/gslide/fastembed-icons
RUN /opt/venv/bin/python scripts/warm_fastembed_cache.py


FROM node:20-bookworm-slim AS assets-builder

WORKDIR /app

ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/*

COPY package.json /app/

RUN mkdir -p /app/document-extraction-liteparse \
    && cd /app/document-extraction-liteparse \
    && npm init -y \
    && npm install @llamaindex/liteparse@1.5.2 --omit=dev

COPY scripts/liteparse_runner.mjs /app/document-extraction-liteparse/liteparse_runner.mjs
COPY scripts/sync-presentation-export.cjs /app/scripts/sync-presentation-export.cjs
RUN rm -rf /app/presentation-export \
    && EXPORT_RUNTIME_ARCH="${TARGETARCH}" node /app/scripts/sync-presentation-export.cjs --force \
    && find /app/presentation-export/py -maxdepth 1 -type f -name "convert-linux-*" -exec chmod +x {} \; \
    && cd /app/presentation-export \
    && npm init -y \
    && npm install "sharp@^0.34.5" --include=optional --omit=dev --no-fund --no-audit --no-package-lock


FROM python:3.11-slim-trixie

WORKDIR /app

ARG INSTALL_TESSERACT=true
ARG TARGETARCH
ARG CHROMIUM_VERSION=149.0.7827.196-1~deb13u1
ARG CHROMIUM_SNAPSHOT=20260625T180000Z

ENV APP_DATA_DIRECTORY=/app_data \
    TEMP_DIRECTORY=/tmp/gslide \
    EXPORT_PACKAGE_ROOT=/app/presentation-export \
    EXPORT_RUNTIME_DIR=/app/presentation-export \
    BUILT_PYTHON_MODULE_PATH=/app/presentation-export/py/convert-linux-current \
    PRESENTON_APP_ROOT=/app \
    GSLIDE_APP_ROOT=/app \
    HF_HOME=/root/.cache/huggingface \
    PRESENTON_FASTEMBED_ICON_CACHE_DIR=/root/.cache/gslide/fastembed-icons \
    GSLIDE_FASTEMBED_ICON_CACHE_DIR=/root/.cache/gslide/fastembed-icons \
    PATH="/opt/venv/bin:${PATH}" \
    NODE_ENV=production \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

RUN set -eux; \
    printf 'Acquire::Check-Valid-Until "false";\n' > /etc/apt/apt.conf.d/99snapshot; \
    printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/%s trixie-security main\n' "$CHROMIUM_SNAPSHOT" > /etc/apt/sources.list.d/chromium-snapshot.list; \
    packages="ca-certificates curl fontconfig imagemagick zstd \
    fonts-liberation fonts-noto-core fonts-noto-extra fonts-noto-mono fonts-noto-ui-core fonts-noto-ui-extra \
    fonts-noto-cjk fonts-noto-cjk-extra fonts-noto-color-emoji xdg-utils \
    libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 \
    libcairo2 libcups2t64 libdbus-1-3 libdrm2 libexpat1 libgbm1 \
    libglib2.0-0t64 libgtk-3-0t64 libnspr4 libnss3 libpango-1.0-0 \
    libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
    libxkbcommon0 libxrandr2 libxshmfence1 libxss1 libxtst6"; \
    if [ "$INSTALL_TESSERACT" = "true" ]; then packages="$packages tesseract-ocr tesseract-ocr-eng"; fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends --allow-downgrades \
    $packages \
    chromium="${CHROMIUM_VERSION}" \
    chromium-common="${CHROMIUM_VERSION}" \
    chromium-driver="${CHROMIUM_VERSION}"; \
    apt-mark hold chromium chromium-common chromium-driver; \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -; \
    apt-get install -y --no-install-recommends nodejs; \
    rm -rf /var/lib/apt/lists/*

RUN find /usr/share/fonts -type f ! -iname 'Noto*' -delete \
    && find /usr/share/fonts -type d -empty -delete \
    && fc-cache -fsv

RUN mkdir -p /app/scripts /app/servers/fastapi
RUN mkdir -p /app_data/exports /app_data/images /app_data/uploads /app_data/fonts /app_data/templates /app_data/pptx-to-html /app_data/pptx-to-json \
    && chmod -R a+rX /app_data

COPY --from=fastapi-builder /opt/venv /opt/venv
COPY --from=fastapi-builder /app/servers/fastapi /app/servers/fastapi
COPY --from=fastapi-builder /root/.cache/huggingface /root/.cache/huggingface
COPY --from=fastapi-builder /root/.cache/gslide/fastembed-icons /root/.cache/gslide/fastembed-icons
COPY templates /app/templates

COPY --from=assets-builder /app/package.json /app/package.json
COPY --from=assets-builder /app/document-extraction-liteparse /app/document-extraction-liteparse
COPY --from=assets-builder /app/presentation-export /app/presentation-export
COPY --from=assets-builder /app/scripts/sync-presentation-export.cjs /app/scripts/sync-presentation-export.cjs

RUN set -eux; \
    if [ -z "${TARGETARCH:-}" ]; then TARGETARCH="$(dpkg --print-architecture)"; fi; \
    case "$TARGETARCH" in \
    amd64) export_arch="x64" ;; \
    arm64) export_arch="arm64" ;; \
    *) echo "Unsupported TARGETARCH: $TARGETARCH" && exit 1 ;; \
    esac; \
    test -f "/app/presentation-export/py/convert-linux-${export_arch}"; \
    ln -sf "/app/presentation-export/py/convert-linux-${export_arch}" /app/presentation-export/py/convert-linux-current; \
    chmod +x "/app/presentation-export/py/convert-linux-${export_arch}"; \
    ls -lah /app/presentation-export/py

COPY LICENSE NOTICE ./
COPY scripts/start-api.js scripts/gslide-terminal-banner.mjs scripts/gslide-ascii.txt scripts/user-config-env.cjs /app/scripts/

EXPOSE 8000
CMD ["node", "/app/scripts/start-api.js"]
