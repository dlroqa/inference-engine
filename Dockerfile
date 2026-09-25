# syntax=docker/dockerfile:1

# Prebuilt CPU image for the Inference Engine (Block 9, release pipeline).
#   1. dashboard — build the operator dashboard SPA into engine/static
#   2. build     — build a wheel that bundles the SPA, and verify it does
#   3. runtime   — slim, non-root image with the llama.cpp CPU backend installed
#
# The llama.cpp CPU backend is always installed (hash-locked in
# requirements/release-cpu.txt), so this image is the supported deployment
# artifact: linux/amd64 on an AVX2 host. No GPU libraries. Models are never
# baked in; they live on the /data volume. See docs/deployment.md.
#
# Base images are pinned by digest for reproducible releases; bump them
# deliberately (docs/releasing.md).

# --- 1. Dashboard build -----------------------------------------------------
FROM node:22-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS dashboard
WORKDIR /app/dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
# vite.config builds into ../engine/static
COPY engine/ /app/engine/
RUN npm run build

# --- 2. Wheel build ---------------------------------------------------------
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS build
ENV PIP_RETRIES=5 PIP_DEFAULT_TIMEOUT=60
WORKDIR /src
# Wheel-build tooling (pip, build, setuptools, wheel + deps) is hash-locked like
# the runtime deps, so a rebuild of the same source uses the same build tools.
COPY requirements/release-build.txt /tmp/release-build.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/release-build.txt
COPY . .
# Bring in the SPA produced by the dashboard stage so it is packaged in the wheel.
COPY --from=dashboard /app/engine/static/ /src/engine/static/
# --no-isolation: build with the locked setuptools/wheel installed above instead
# of letting PEP 517 isolation resolve pyproject's floating `setuptools>=68` from
# PyPI. This stage is discarded; only the wheel reaches the runtime image.
RUN python -m build --no-isolation --wheel --outdir /dist \
    && python scripts/release.py verify-wheel /dist/*.whl \
         --expect-version "$(python scripts/release.py check-version)"

# --- 3. Runtime -------------------------------------------------------------
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS runtime
ARG IE_VERSION=""
ARG IE_BUILD_SHA=""
ARG IE_BUILD_DATE=""
ARG IE_SOURCE_URL="https://github.com/dlroqa/inference-engine"
LABEL org.opencontainers.image.title="inference-engine" \
      org.opencontainers.image.description="Self-hosted inference engine — CPU (llama.cpp) image" \
      org.opencontainers.image.source="${IE_SOURCE_URL}" \
      org.opencontainers.image.revision="${IE_BUILD_SHA}" \
      org.opencontainers.image.version="${IE_VERSION}" \
      org.opencontainers.image.created="${IE_BUILD_DATE}" \
      org.opencontainers.image.licenses="LicenseRef-Proprietary"
ENV PIP_RETRIES=5 PIP_DEFAULT_TIMEOUT=60 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    IE_HOST=0.0.0.0 \
    IE_ALLOW_NETWORK_BIND=true \
    IE_DATA_DIR=/data \
    IE_BUILD_SHA=${IE_BUILD_SHA} \
    IE_BUILD_DATE=${IE_BUILD_DATE}

# Create a non-root user and a data volume it owns.
RUN useradd --create-home --uid 10001 engine \
    && mkdir -p /data && chown engine:engine /data

# Runtime deps (incl. the llama-cpp-python CPU wheel) come only from the
# hash-locked set; the engine wheel itself is then installed without resolving.
COPY requirements/release-cpu.txt /tmp/release-cpu.txt
COPY --from=build /dist/*.whl /tmp/
RUN pip install --require-hashes --only-binary=:all: -r /tmp/release-cpu.txt \
    && pip install --no-deps /tmp/*.whl \
    && pip check \
    && python -c "import importlib.resources as r, llama_cpp; \
assert r.files('engine').joinpath('static/index.html').is_file(), 'dashboard missing'" \
    && rm -f /tmp/*.whl /tmp/release-cpu.txt

USER engine
WORKDIR /home/engine
VOLUME ["/data"]
EXPOSE 8000

# Liveness for orchestrators; the app also exposes /readyz for routing decisions.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"

# Bind is 0.0.0.0 inside the container; auth is required by default on a non-loopback
# bind (put TLS/authn at your reverse proxy — see docs/deployment.md).
CMD ["inference-engine", "serve"]
