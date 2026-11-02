# ============================================================================
# trading-bot — single deployable module (engine + dashboard).
#
# Stage 1 builds the React dashboard; stage 2 is the Python engine. The built
# SPA lands at /app/frontend/dist so backend/api/main.py's existing
# single-origin serving keeps working (unset VITE_API_BASE_URL in the build
# for same-origin mode — the default here).
#
# SECRETS: the image contains NONE. Everything arrives at runtime via env
# (--env-file / platform secrets) or a mounted keypair file
# (WALLET_KEYPAIR_PATH). See docker-entrypoint.sh + docs/11_DEPLOYMENT.md.
# ============================================================================
FROM node:20-slim AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY frontend/ ./
RUN npm run build

FROM python:3.14-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY backend/requirements.lock /app/backend/requirements.lock
RUN pip install --no-cache-dir --no-deps -r /app/backend/requirements.lock

# §47 — Scrapling stealth-browser layer (crowd-feed free transport). Browser
# + its system deps install as ROOT, before USER bot, or Chromium cannot
# write its profile. ~+1.5 GB image. On linux/arm64: if patchright's patched
# build is missing, fall back to `playwright install chromium` (official
# arm64 build).
#
# §87 F3: CI sets SKIP_BROWSER=1 so the QEMU linux/arm64 cross-build validates
# the Python app layer without spending the job cap on the ~1.5 GB Chromium
# tree. The operator's native ARM build (docker compose up --build) still
# installs it. The browser is a fallback transport, not on the order path.
ARG SKIP_BROWSER=0
RUN if [ "$SKIP_BROWSER" = "1" ]; then \
      echo "SKIP_BROWSER=1 — skipping patchright/chromium (CI ARM build check)"; \
    else \
      python -m patchright install --with-deps chromium \
      || python -m patchright install chromium; \
    fi

COPY backend/ /app/backend/
COPY scripts/ /app/scripts/
COPY --from=frontend-build /build/dist/ /app/frontend/dist/
COPY backend/docker-entrypoint.sh /app/docker-entrypoint.sh

# Non-root runtime; pre-create runtime directories with secure permissions (§70, §74).
# The operator mounts state/secrets readable by uid 10001.
RUN useradd --uid 10001 --create-home bot \
    && mkdir -p /app/logs /app/data /app/backups /app/backend/live_execution/state /app/backend/knowledge_base/ingested \
    && chmod +x /app/docker-entrypoint.sh \
    && chmod 750 /app/logs \
    && chmod 700 /app/backups /app/data /app/backend/live_execution/state \
    && (chmod -R 755 /ms-playwright 2>/dev/null || true) \
    && chown -R bot:bot /app
USER bot

EXPOSE 8000
# §87 B: the runner handles SIGTERM by finishing an in-flight cycle/order and
# then logging "graceful shutdown complete". Docker's default stop timeout is
# 10s; compose raises it to 150s for the live service. The signal is explicit
# so a future base-image change cannot silently switch to SIGQUIT/SIGKILL.
STOPSIGNAL SIGTERM
# Health check honours $PORT and $API_PORT (platforms often inject their own).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,sys,urllib.request; \
port = os.getenv('PORT') or os.getenv('API_PORT') or '8000'; \
url = f'http://127.0.0.1:{port}/api/health'; \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]