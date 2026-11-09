"""
api/main.py — FastAPI app (H1–H5).

ENTIRELY READ-ONLY with respect to trading decisions and safety flags:
every endpoint reports state; none can open, close, or modify a trade, or
change PAPER_TRADING_ONLY. The only POST is knowledge-base ingestion, which
touches no trade state.

§52: the paper tick loop (backend/main.py) is retired with the paper book;
the LIVE decision cycle runs as its own process (run_live_cycle.py, started
by start.sh). This API serves the dashboard, the WebSocket feed, and the
read-only research surface only.
"""
from __future__ import annotations

import contextlib
import html
import logging
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles


import config

# live_execution is a sibling package inside backend/ (single deployable
# module), so the sanctioned function-local optional imports of live_execution
# (proof.py, disclosure.py, live_book.py) resolve natively. They stay
# try/except ImportError at every call site: a paper-only checkout still boots.
from api.routes import (
    feed,
    funnel,
    holdings,
    journal,
    knowledge_base,
    market_regime,
    promotion_gate,
    safety,
    stats,
    system_status,
)
from api.websocket import FeedBroadcaster, websocket_endpoint
from api.security_headers import apply_security_headers
from data_providers import build_provider
from llm.narrator import Narrator

log = logging.getLogger(__name__)

broadcaster = FeedBroadcaster()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    config.validate_api_credentials()
    from api import db
    await db.init_db()
    app.state.provider = build_provider()
    app.state.narrator = Narrator()
    broadcaster.start()

    # §79 Phase D: Heartbeat watcher task
    watcher_task = None
    try:
        import asyncio
        from alerts import heartbeat_watcher
        stale_sec = float(getattr(config, "HEARTBEAT_STALE_SECONDS", 300.0))
        watcher_task = asyncio.create_task(
            heartbeat_watcher(check_interval=60.0, stale_threshold=stale_sec)
        )
    except Exception as exc:
        log.warning("failed starting heartbeat watcher: %s", exc)

    yield
    if watcher_task is not None:
        watcher_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher_task
    await app.state.narrator.aclose()
    await broadcaster.stop()


app = FastAPI(
    title="trading-bot",
    version="1.0.0",
    lifespan=lifespan,
    debug=config.DEBUG,
    docs_url="/docs" if config.DEBUG else None,
    redoc_url="/redoc" if config.DEBUG else None,
    openapi_url="/openapi.json" if config.DEBUG else None,
)
from api.logging_config import setup_logging
from api.error_handler import install_secret_redactor, register_error_handlers

setup_logging()
install_secret_redactor()
register_error_handlers(app)

from api.rate_limiter import RateLimitMiddleware
app.add_middleware(RateLimitMiddleware)

cors_origins = [o for o in config.FRONTEND_ORIGINS if o != "*"]
if "*" in config.FRONTEND_ORIGINS:
    log.error("CORS misconfiguration: wildcard '*' origin forbidden when allow_credentials=True; stripping '*' (§77 E4)")

app.add_middleware(
    CORSMiddleware,
    # Split deployments (dashboard on Vercel, API elsewhere) may need more
    # than one origin: FRONTEND_ORIGIN accepts a comma-separated list.
    allow_origins=cors_origins,
    # §38 F6: only the verbs/headers the dashboard actually uses.
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Admin-Token", "X-Requested-With"],
    allow_credentials=True,
)


# ---------------------------------------------------------------------------
# S2 — CSRF defense for cookie-authenticated mutations.
#
# SameSite=Lax is not sufficient for split deployments (SameSite=None is
# required for a cross-origin dashboard) and older clients. A session cookie
# therefore authenticates a mutation only when the request ALSO proves it was
# issued by our JavaScript: a custom header that an HTML form cannot set.
# If Origin/Referer is present it must match the effective request origin or
# an explicitly allowed frontend origin; a mismatched one is rejected even
# with the header.
# ---------------------------------------------------------------------------
_CSRF_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_CSRF_EXEMPT_PATHS = frozenset({"/api/auth/login", "/api/auth/challenge"})
_CSRF_HEADER = "X-Requested-With"
_CSRF_HEADER_VALUE = "XMLHttpRequest"


def _normalise_origin(value: str) -> str:
    raw = (value or "").strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _effective_request_origin(request: Request) -> str:
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
        or ""
    ).lower()
    return f"{proto}://{host}".rstrip("/")


def _csrf_origin_allowed(origin_or_referer: str, request: Request) -> bool:
    candidate = _normalise_origin(origin_or_referer)
    if not candidate:
        return False
    if candidate == _effective_request_origin(request):
        return True
    allowed = {_normalise_origin(o) for o in getattr(config, "FRONTEND_ORIGINS", [])}
    return candidate in allowed


def _record_csrf_block(request: Request, reason: str) -> None:
    try:
        from api.rate_limiter import get_client_ip
        from api.security_monitor import SecurityEventType, record_security_event
        record_security_event(
            SecurityEventType.CSRF_BLOCKED,
            client_ip=get_client_ip(request),
            path=request.url.path,
            method=request.method,
            detail=reason,
            severity="WARNING",
        )
    except Exception:
        pass


@app.middleware("http")
async def csrf_protect_middleware(request: Request, call_next):
    """Reject cross-site cookie-authenticated mutations (§92 S2)."""
    method = request.method.upper()
    path = request.url.path
    if (method in _CSRF_UNSAFE_METHODS
            and path.startswith("/api/")
            and path not in _CSRF_EXEMPT_PATHS
            and request.cookies.get(config.SESSION_COOKIE_NAME, "")):
        supplied = request.headers.get(_CSRF_HEADER, "").strip()
        if supplied.lower() != _CSRF_HEADER_VALUE.lower():
            _record_csrf_block(request, "cookie-auth mutation without X-Requested-With")
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF verification failed: missing X-Requested-With"},
            )
        origin = request.headers.get("origin", "")
        referer = request.headers.get("referer", "")
        if origin and not _csrf_origin_allowed(origin, request):
            _record_csrf_block(request, "cookie-auth mutation from disallowed Origin")
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF verification failed: origin not allowed"},
            )
        if referer and not _csrf_origin_allowed(referer, request):
            _record_csrf_block(request, "cookie-auth mutation from disallowed Referer")
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF verification failed: referer not allowed"},
            )
    return await call_next(request)



class RequestBodyLimitMiddleware:
    """§92 S4: bound every request body before parsing or disk writes.

    Pure ASGI (not BaseHTTPMiddleware) so a chunked body is rejected with a
    real 413 response instead of an exception raised inside FastAPI's body
    parser. The body is buffered up to the cap and replayed to the app, so
    routes see the same stream they would have received.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        cap = int(getattr(config, "MAX_REQUEST_BODY_BYTES", 16 * 1024 * 1024))
        if cap <= 0:
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers") or []}
        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > cap:
                    return await _send_body_too_large(scope, receive, send)
            except ValueError:
                response = JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length"},
                )
                return await response(scope, receive, send)

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                break
            if message.get("type") != "http.request":
                continue
            body = message.get("body", b"")
            total += len(body)
            if total > cap:
                return await _send_body_too_large(scope, receive, send)
            chunks.append(body)
            if not message.get("more_body", False):
                break

        replayed = False

        async def _replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request",
                        "body": b"".join(chunks),
                        "more_body": False}
            return await receive()

        await self.app(scope, _replay, send)


async def _send_body_too_large(scope, receive, send) -> None:
    response = JSONResponse(
        status_code=413,
        content={"detail": "Request body too large"},
    )
    await response(scope, receive, send)


app.add_middleware(RequestBodyLimitMiddleware)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# Synthetic hostnames used by httpx/Starlette test transports. They are only
# loopback while a pytest process is running; otherwise a forged Host header
# would disable the HTTPS redirect.
_TEST_HOSTS = frozenset({"testclient", "test", "testserver", "app"})


def _is_loopback_host(host: str) -> bool:
    if not host:
        return True
    if host in _LOOPBACK_HOSTS:
        return True
    return host in _TEST_HOSTS and bool(getattr(config, "TESTING", False))


@app.middleware("http")
async def force_https_middleware(request, call_next):
    """Force HTTPS redirect on non-loopback connections and reverse proxies."""
    if getattr(config, "FORCE_HTTPS", True):
        forwarded_proto = (request.headers.get("x-forwarded-proto") or "").lower()
        forwarded_ssl = (request.headers.get("x-forwarded-ssl") or "").lower()
        forwarded_hdr = (request.headers.get("forwarded") or "").lower()
        is_insecure_forwarded = (
            forwarded_proto == "http"
            or forwarded_ssl == "off"
            or "proto=http" in forwarded_hdr
        )

        client_host = getattr(getattr(request, "client", None), "host", "") or ""
        request_host = getattr(request.url, "hostname", "") or ""

        # Allow local loopback and, under pytest only, synthetic test hosts.
        is_loopback = (
            _is_loopback_host(client_host)
            and _is_loopback_host(request_host)
            and not is_insecure_forwarded
        )

        if is_insecure_forwarded or (request.url.scheme == "http" and not is_loopback):
            # A7 (repo audit): 308, not 301 — a 301 redirect is re-issued
            # as GET by many clients (dropping a POST body) and is cached
            # aggressively; 308 preserves the method on both counts.
            forwarded_host = (
                request.headers.get("x-forwarded-host")
                or request.headers.get("host")
                or request.url.netloc
            )
            # Remove internal backend ports (80, 8000, 8080) if redirecting to public HTTPS
            if is_insecure_forwarded or request.headers.get("x-forwarded-host"):
                if ":" in forwarded_host:
                    h, p = forwarded_host.rsplit(":", 1)
                    if p in ("80", "8000", "8080"):
                        forwarded_host = h

            redirect_url = request.url.replace(scheme="https", netloc=forwarded_host)
            return RedirectResponse(url=str(redirect_url), status_code=308)

    return await call_next(request)


@app.middleware("http")
async def security_headers(request, call_next):
    """Baseline security headers on every response (F5 & security checklist).

    Defense-in-depth: nosniff stops MIME-confusion, DENY blocks click-
    jacking framing, no-referrer keeps local URLs out of third-party logs,
    upgrade-insecure-requests automatically upgrades mixed content, and
    no-store keeps book/wallet data out of any cache.
    """
    response = await call_next(request)
    return apply_security_headers(request, response)


for module in (feed, funnel, holdings, journal, stats, market_regime,
               promotion_gate, knowledge_base, system_status, safety):
    app.include_router(module.router)

from api.routes.proof import router as proof_router  # noqa: E402
app.include_router(proof_router)

from api.routes.disclosure import router as disclosure_router  # noqa: E402
app.include_router(disclosure_router)

from api.routes.admin import router as admin_router  # noqa: E402
app.include_router(admin_router)

from api.routes.auth import router as auth_router  # noqa: E402
app.include_router(auth_router)

from api.routes.live_book import router as live_book_router  # noqa: E402
app.include_router(live_book_router)


@app.get("/api/health")
async def health_check():
    """Lightweight health check endpoint for container orchestrators (§77 B2)."""
    return {"status": "ok"}


@app.websocket("/ws/feed")
async def ws_feed(ws: WebSocket):
    await websocket_endpoint(ws, broadcaster)


# ---------------------------------------------------------------------------
# Optional: serve the built React dashboard from this same origin/port, so a
# single process (and a single click) exposes the whole application.
# ---------------------------------------------------------------------------
FRONTEND_DIST = config.BASE_DIR.parent / "frontend" / "dist"


def _safe_dist_file(full_path: str) -> Path | None:
    """Resolve `full_path` to a file INSIDE FRONTEND_DIST, or None.

    Path-containment guard (§55 authz audit): a raw `//etc/passwd` request
    reaches this route with a LEADING-SLASH path param, and pathlib
    absolute-joins discard the dist base (`Path('/dist') / '/etc/passwd'`
    -> '/etc/passwd') — an arbitrary file read, confirmed by raw-ASGI probe.
    resolve() collapses `..`, symlinks AND absolute re-joins onto one
    comparable path; is_relative_to() then enforces containment. Everything
    else (empty, traversal, escape) gets the SPA shell.
    """
    if not full_path or ".." in full_path or len(full_path) > 512 or "\x00" in full_path:
        return None
    try:
        root = FRONTEND_DIST.resolve()
        candidate = (FRONTEND_DIST / full_path).resolve()
    except (OSError, ValueError):
        return None
    if candidate.is_file() and candidate.is_relative_to(root):
        return candidate
    return None


@app.get("/")
async def root():
    # When the built frontend exists it is served by the SPA catch-all below;
    # this JSON root only shows when no frontend build is present.
    if not FRONTEND_DIST.exists():
        return {
            "service": "trading-bot",
            # §52: the paper book is retired; the live cycle is a separate
            # process. This banner only shows on a checkout with no built
            # frontend.
            "paper_trading_only": False,
            "note": "Read-only research API. No endpoint can open, close, or "
                    "modify a trade.",
        }
    return FileResponse(FRONTEND_DIST / "index.html")


if FRONTEND_DIST.exists():
    assets_dir = FRONTEND_DIST / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

# §57: the /api/* loud-404 guarantee is INDEPENDENT of the frontend build.
# Before, this catch-all was registered only when a built SPA existed — so a
# buildless checkout (fresh clone, split deploy with the dashboard on Vercel)
# silently lost the JSON-404 contract and unmatched /api/* paths returned
# Starlette's bare "Not Found" 404 with no JSON detail. A wrong API URL must
# fail identically whether or not a frontend is present. (Both decorators:
# "/api" alone and "/api/..." — the bare-prefix request was 404'd before too.)
@app.get("/api", include_in_schema=False)
@app.get("/api/{full_path:path}", include_in_schema=False)
async def unknown_api_path(full_path: str = ""):
    """An unmatched /api/* path is a wrong URL (or a removed endpoint) —
    it must fail loudly as JSON 404, never fall through to the SPA shell (a
    200 HTML body silently breaks API clients and monitors)."""
    safe_path = html.escape(
        full_path.replace("\r", "").replace("\n", "").replace("\x00", "")[:200]
    )
    return JSONResponse(
        status_code=404,
        content={"detail": f"Unknown API path: /api/{safe_path}"},
    )

_BLOCKED_STATIC_EXTENSIONS = frozenset({
    ".map", ".git", ".env", ".py", ".db", ".key", ".pem", ".cert",
    ".bak", ".log", ".yml", ".yaml", ".toml", ".sh", ".ini", ".conf",
    ".sql", ".sqlite",
})

_DISABLED_DEBUG_PATHS = frozenset({"docs", "redoc", "openapi.json"})


def _is_blocked_static_path(path: str) -> bool:
    """Check if a path requests dotfiles, sourcemaps, or sensitive system files."""
    cleaned = path.strip().replace("\\", "/").lower()
    # When debug is off, suppress interactive documentation routes from falling through to SPA
    if not getattr(config, "DEBUG", False):
        if cleaned in _DISABLED_DEBUG_PATHS or cleaned.startswith("docs/") or cleaned.startswith("redoc/"):
            return True

    # Check for dotfiles/directories (.git, .env, etc.) anywhere in the path
    segments = [seg for seg in cleaned.split("/") if seg]
    for seg in segments:
        if seg.startswith("."):
            return True
    # Check for blocked extensions (sourcemaps, secrets, server files)
    p = Path(cleaned)
    if p.suffix in _BLOCKED_STATIC_EXTENSIONS:
        return True
    return False


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str, request: Request):
    # Explicit 404 for dotfiles (.git, .env) and sourcemaps (.map) — always active (§77 D1)
    if _is_blocked_static_path(full_path):
        try:
            from api.rate_limiter import get_client_ip
            from api.security_monitor import SecurityEventType, record_security_event
            client_ip = get_client_ip(request)
            record_security_event(
                SecurityEventType.BLOCKED_PATH_PROBE,
                client_ip=client_ip,
                path=f"/{full_path}",
                method="GET",
                detail=f"Blocked static path probe: {html.escape(full_path[:100])}",
                severity="WARNING",
            )
        except Exception:
            pass
        return JSONResponse(
            status_code=404,
            content={"detail": "Not found"},
        )

    if FRONTEND_DIST.exists():
        # §55: containment via _safe_dist_file — never a raw path join.
        candidate = _safe_dist_file(full_path)
        if candidate is not None:
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")

    return JSONResponse(
        status_code=404,
        content={"detail": "Not found"},
    )
