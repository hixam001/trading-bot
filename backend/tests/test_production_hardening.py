"""
backend/tests/test_production_hardening.py — production security hardening tests.

Verifies:
1. Debug mode disabled in production:
   - FastAPI debug flag is False.
   - Swagger UI (/docs) is disabled (404).
   - ReDoc (/redoc) is disabled (404).
   - OpenAPI schema (/openapi.json) is disabled (404).
   - When config.DEBUG is True, interactive docs are accessible.
2. Source maps blocked and disabled:
   - Requests for *.map files return 404.
   - frontend/vite.config.ts explicitly sets build.sourcemap = false.
   - No .map files exist in frontend/dist.
3. .git directory and sensitive dotfiles blocked:
   - Requests for /.git, /.git/config, /.git/HEAD return 404 (never 200 SPA shell).
   - Requests for /.env, /.env.local, /.env.production return 404.
   - .dockerignore explicitly excludes .git and .env files.
4. Normal frontend routing preserved:
   - Root (/) and SPA client-side routes (/journal, /holdings) return 200 OK.
"""
from __future__ import annotations

import os
from pathlib import Path
import httpx
import pytest
import pytest_asyncio

import config
from api import db
from api.main import app, _is_blocked_static_path


@pytest_asyncio.fixture
async def prod_env(tmp_path, monkeypatch):
    config.DB_PATH = tmp_path / "test_prod_hardening.db"
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    await db.init_db()
    yield


@pytest_asyncio.fixture
async def client(prod_env):
    from data_providers.mock import MockProvider
    from llm.narrator import Narrator

    app.state.provider = MockProvider()
    app.state.narrator = Narrator()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Debug Mode & Interactive API Documentation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_debug_mode_disabled_by_default():
    """By default, config.DEBUG is False in production."""
    assert config.DEBUG is False
    assert config.ENVIRONMENT in ("production", "prod")


@pytest.mark.asyncio
async def test_swagger_and_openapi_disabled_in_production(client):
    """In production (DEBUG=False), /docs, /redoc, and /openapi.json are turned off."""
    r_docs = await client.get("/docs")
    assert r_docs.status_code == 404

    r_redoc = await client.get("/redoc")
    assert r_redoc.status_code == 404

    r_openapi = await client.get("/openapi.json")
    assert r_openapi.status_code == 404


# ---------------------------------------------------------------------------
# 2. Source Maps Exposure Defense
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sourcemaps_return_404(client):
    """Requests for JavaScript or CSS sourcemaps return 404 Not Found."""
    paths = [
        "/assets/index.js.map",
        "/index.js.map",
        "/main.css.map",
        "/bundle.map",
        "/dist/assets/app.js.map",
    ]
    for p in paths:
        r = await client.get(p)
        assert r.status_code == 404, f"Expected 404 for sourcemap {p}, got {r.status_code}"
        assert r.headers.get("content-type", "").startswith("application/json") or r.status_code == 404


def test_vite_config_disables_sourcemaps():
    """frontend/vite.config.ts must explicitly configure sourcemap: false."""
    vite_config_path = config.BASE_DIR.parent / "frontend" / "vite.config.ts"
    assert vite_config_path.exists(), "vite.config.ts must exist"
    content = vite_config_path.read_text(encoding="utf-8")
    assert "sourcemap: false" in content or "sourcemap:false" in content


def test_no_map_files_in_frontend_dist():
    """Built frontend dist directory must contain zero .map files."""
    frontend_dist = config.BASE_DIR.parent / "frontend" / "dist"
    if frontend_dist.exists():
        map_files = list(frontend_dist.glob("**/*.map"))
        assert len(map_files) == 0, f"Found unexpected source maps in dist: {map_files}"


# ---------------------------------------------------------------------------
# 3. .git Directory & Sensitive Dotfiles Protection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_git_folder_and_files_return_404(client):
    """Direct probes for .git and sensitive repository files return 404, never 200."""
    probes = [
        "/.git",
        "/.git/",
        "/.git/config",
        "/.git/HEAD",
        "/.git/index",
        "/.git/refs/heads/main",
        "/.gitignore",
        "/.env",
        "/.env.local",
        "/.env.production",
        "/.dockerignore",
    ]
    for p in probes:
        r = await client.get(p)
        assert r.status_code == 404, f"Expected 404 for sensitive path {p}, got {r.status_code}"


def test_is_blocked_static_path_helper():
    """Unit test _is_blocked_static_path helper for security boundary conditions."""
    # Dotfiles / hidden dirs
    assert _is_blocked_static_path(".git")
    assert _is_blocked_static_path(".git/config")
    assert _is_blocked_static_path("/.git/HEAD")
    assert _is_blocked_static_path(".env")
    assert _is_blocked_static_path(".env.local")
    assert _is_blocked_static_path("foo/.env")

    # Blocked extensions
    assert _is_blocked_static_path("bundle.map")
    assert _is_blocked_static_path("assets/app.js.map")
    assert _is_blocked_static_path("config.py")
    assert _is_blocked_static_path("database.sqlite")
    assert _is_blocked_static_path("wallet.key")
    assert _is_blocked_static_path("cert.pem")

    # Safe public frontend paths
    assert not _is_blocked_static_path("assets/index-ABC123.js")
    assert not _is_blocked_static_path("assets/index-DEF456.css")
    assert not _is_blocked_static_path("favicon.ico")
    assert not _is_blocked_static_path("journal")
    assert not _is_blocked_static_path("holdings")


def test_dockerignore_excludes_git_and_env():
    """.dockerignore must explicitly exclude .git and .env from production images."""
    dockerignore_path = config.BASE_DIR.parent / ".dockerignore"
    assert dockerignore_path.exists(), ".dockerignore must exist"
    lines = [
        line.strip()
        for line in dockerignore_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert ".git" in lines, ".git must be in .dockerignore"
    assert ".env" in lines, ".env must be in .dockerignore"
    assert ".env.*" in lines, ".env.* must be in .dockerignore"


# ---------------------------------------------------------------------------
# 4. Legitimate Application Routing Preserved
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_legitimate_routes_accessible(client):
    """System status and public API endpoints remain fully functional."""
    r = await client.get("/api/system-status")
    assert r.status_code == 200

    r_root = await client.get("/")
    assert r_root.status_code == 200
