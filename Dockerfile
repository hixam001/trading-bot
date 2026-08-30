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
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend/ /app/backend/
COPY --from=frontend-build /build/dist/ /app/frontend/dist/
COPY backend/docker-entrypoint.sh /app/docker-entrypoint.sh

# Non-root runtime; the operator mounts state/secrets readable by uid 10001.
RUN useradd --uid 10001 --create-home bot \
    && chmod +x /app/docker-entrypoint.sh \
    && chown -R bot:bot /app
USER bot

EXPOSE 8000
# Health check honours $PORT (platforms often inject their own).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,sys,urllib.request; \
url='http://127.0.0.1:%s/api/system-status' % os.getenv('PORT','8000'); \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]