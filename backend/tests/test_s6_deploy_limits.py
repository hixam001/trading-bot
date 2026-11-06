"""
backend/tests/test_s6_deploy_limits.py — §92 S6 server process contracts.

The rate limiter, auth lockout, PoW nonce set, and session revocation cache are
per-process in-memory structures. Running uvicorn with more than one worker
silently multiplies every quota and splits state between workers, so every
launch site must pin one worker explicitly. The same launch sites also carry
bounded connection/header/keep-alive/graceful-shutdown flags so a slow or
oversized client cannot consume unbounded server resources.
"""
from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

import config
from api.routes import feed as feed_routes
from api.routes import funnel as funnel_routes

ROOT = Path(config.BASE_DIR).parent
LAUNCH_SITES = [
    ROOT / "backend" / "docker-entrypoint.sh",
    ROOT / "start.sh",
    ROOT / "scripts" / "prepush_check.sh",
]

REQUIRED_FLAGS = [
    ("--workers", "1"),
    ("--limit-concurrency", "200"),
    ("--timeout-keep-alive", "5"),
    ("--h11-max-incomplete-event-size", "65536"),
    ("--timeout-graceful-shutdown", "20"),
]


def _uvicorn_commands(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    # Join shell line continuations so a multi-line command is one string.
    text = text.replace("\\\n", " ")
    commands = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line.strip())
        if "uvicorn api.main:app" not in line:
            continue
        if line.startswith("#"):
            continue
        commands.append(line)
    return commands


def test_every_uvicorn_launch_site_pins_one_worker_and_bounds():
    commands = []
    for site in LAUNCH_SITES:
        assert site.exists(), f"missing launch site {site}"
        commands.extend(_uvicorn_commands(site))

    # 2 in the container entrypoint (API role + single-container fallback),
    # 1 in start.sh, 1 in the prepush mock backend.
    assert len(commands) >= 4, commands

    for command in commands:
        for flag, value in REQUIRED_FLAGS:
            assert f"{flag} {value}" in command, (
                f"{flag} {value} missing from: {command}")

    # Never allow an explicit multi-worker or auto-worker launch.
    joined = "\n".join(commands)
    assert not re.search(r"--workers\s+(?:0|[2-9]|\d{2,})\b", joined)
    assert "--workers auto" not in joined


def test_api_process_has_no_docker_compose_worker_override():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    # The API service must not override the entrypoint with a multi-worker
    # command; the entrypoint is the single source of launch flags.
    assert "gunicorn" not in compose.lower()
    assert not re.search(r"UVICORN_WORKERS\s*[:=]\s*['\"]?[2-9]", compose)
    assert not re.search(r"WEB_CONCURRENCY\s*[:=]\s*['\"]?[2-9]", compose)


def test_entrypoint_documents_single_worker_reason():
    text = (ROOT / "backend" / "docker-entrypoint.sh").read_text(
        encoding="utf-8")
    assert "single worker" in text.lower() or "one worker" in text.lower()


# Every public endpoint that turns a caller-supplied page size into a database
# scan must reject values above its declared ceiling *before* the handler runs.
# The request that trips validation never touches the database, so this test
# stays hermetic while still proving the ceiling is enforced at the edge.
BOUNDED_QUERIES = [
    ("/api/feed", "limit", 500),
    ("/api/feed", "offset", 100_000),
    ("/api/mint/So11111111111111111111111111111111111111112/history",
     "limit", 500),
    ("/api/mint/So11111111111111111111111111111111111111112/history",
     "offset", 100_000),
    ("/api/events.json", "limit", 500),
    ("/api/funnel", "limit", 10_000),
    ("/api/funnel/snapshots", "limit", 1_000),
]


@pytest.mark.parametrize("path,param,maximum", BOUNDED_QUERIES)
async def test_public_db_scan_queries_reject_values_above_declared_max(
        path, param, maximum):
    app = FastAPI()
    app.include_router(feed_routes.router)
    app.include_router(funnel_routes.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://127.0.0.1") as client:
        response = await client.get(path, params={param: maximum + 1})
    assert response.status_code == 422, (
        f"{path}?{param}={maximum + 1} was not bounded (status "
        f"{response.status_code})")
