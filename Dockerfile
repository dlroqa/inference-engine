# syntax=docker/dockerfile:1

# Multi-stage build for the Inference Engine (Block 9).
#   1. node   — build the operator dashboard SPA into engine/static
#   2. build  — build a wheel that bundles the SPA
#   3. runtime — slim, non-root image that installs the wheel
#
# CPU-only by default. The llama.cpp backend is optional (it needs an AVX2 host);
# build with --build-arg INSTALL_LLAMA=true to include it. See docs/deployment.md.

# --- 1. Dashboard build -----------------------------------------------------
FROM node:22-slim AS dashboard
WORKDIR /app/dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
# vite.config builds into ../engine/static
COPY engine/ /app/engine/
RUN npm run build

# --- 2. Wheel build ---------------------------------------------------------
FROM python:3.12-slim AS build
ENV PIP_RETRIES=5 PIP_DEFAULT_TIMEOUT=60
WORKDIR /src
RUN pip install --no-cache-dir --upgrade pip build
COPY . .
# Bring in the SPA produced by the dashboard stage so it is packaged in the wheel.
COPY --from=dashboard /app/engine/static/ /src/engine/static/
RUN python -m build --wheel --outdir /dist

# --- 3. Runtime -------------------------------------------------------------
FROM python:3.12-slim AS runtime
ARG INSTALL_LLAMA=false
ARG IE_BUILD_SHA=""
ARG IE_BUILD_DATE=""
ENV PIP_RETRIES=5 PIP_DEFAULT_TIMEOUT=60 \
    PYTHONUNBUFFERED=1 \
    IE_HOST=0.0.0.0 \
    IE_ALLOW_NETWORK_BIND=true \
    IE_DATA_DIR=/data \
    IE_BUILD_SHA=${IE_BUILD_SHA} \
    IE_BUILD_DATE=${IE_BUILD_DATE}

# Create a non-root user and a data volume it owns.
RUN useradd --create-home --uid 10001 engine \
    && mkdir -p /data && chown engine:engine /data

COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl \
    && if [ "$INSTALL_LLAMA" = "true" ]; then \
         pip install --no-cache-dir "llama-cpp-python>=0.2.90" \
           --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu ; \
       fi \
    && rm -f /tmp/*.whl

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
