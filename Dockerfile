FROM --platform=$BUILDPLATFORM node:24-bookworm-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c AS node-assets
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts

FROM node:24-bookworm-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c AS node-runtime
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts

FROM python:3.11-slim-bookworm@sha256:94a8fee35252d06ab75856f15f12c7bae67b10e8a6da93c7fded82b2610926cd AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src \
    SPK_STATE_DIR=/state SPK_KEY_DIR=/keys SPK_MEDIA_ROOT=/media \
    SPK_LOCAL_ROOT=/music SPK_BIND=0.0.0.0 HOME=/home/spk
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates ffmpeg libasound2 libssl3 libgomp1 curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 spk && useradd --uid 1000 --gid 1000 --no-create-home spk \
    && install -d -o 1000 -g 1000 /state /keys /keys/media /keys/voice /keys/resolver /keys/mcp \
       /media /music /models /accounts /home/spk /resolver
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.txt
COPY src/ ./src/
COPY config/spk_config.py ./config/spk_config.py
COPY --from=node-assets /build/node_modules/lucide /app/node_modules/lucide
COPY --from=node-assets /build/node_modules/@fontsource /app/node_modules/@fontsource
USER 1000:1000
ENTRYPOINT ["python", "-m", "spk_platform.cli"]
CMD ["serve", "control"]

FROM base AS voice
USER root
COPY requirements-voice.txt ./
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements-voice.txt
USER 1000:1000
CMD ["serve", "voice"]

FROM voice AS runtime
USER root
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /build/node_modules /app/node_modules
COPY docker/mpv /app/bin/mpv
RUN chmod 0755 /app/bin/mpv
ENV SPK_RESOLVER_DATA=/resolver SPK_MPV_BIN=/app/bin
USER 1000:1000
CMD ["serve", "control"]