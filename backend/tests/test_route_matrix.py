"""
backend/tests/test_route_matrix.py — S2: security docs cannot drift from app.

Every registered HTTP route, WebSocket, and mount must have a row in
docs/18_SECURITY.md. Adding an endpoint without documenting its auth posture
fails here, before it can ship.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import Mount, Route

from api.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOC = REPO_ROOT / "docs" / "18_SECURITY.md"
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "WS", "MOUNT"}


def _walk(routes):
    for route in routes:
        if isinstance(route, APIRoute):
            for method in route.methods or ():
                if method.upper() != "HEAD":
                    yield method.upper(), route.path
        elif isinstance(route, APIWebSocketRoute):
            yield "WS", route.path
        elif isinstance(route, Mount):
            yield "MOUNT", route.path
        elif isinstance(route, Route):
            for method in (route.methods or ()):
                if method.upper() != "HEAD":
                    yield method.upper(), route.path
        elif hasattr(route, "original_router"):
            yield from _walk(route.original_router.routes)
        elif hasattr(route, "routes"):
            yield from _walk(route.routes)


def _documented_routes() -> set[tuple[str, str]]:
    rows: set[tuple[str, str]] = set()
    for line in DOC.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip().strip("`") for c in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        method, path = cells[0].upper(), cells[1]
        if method in METHODS and path.startswith("/"):
            rows.add((method, path))
    return rows


def test_security_doc_exists() -> None:
    assert DOC.is_file(), "docs/18_SECURITY.md is required"


def test_every_registered_route_is_in_the_matrix() -> None:
    registered = set(_walk(app.routes))
    documented = _documented_routes()
    missing = sorted(registered - documented)
    assert missing == [], (
        "routes missing from docs/18_SECURITY.md matrix: "
        + ", ".join(f"{m} {p}" for m, p in missing)
    )


def test_matrix_has_no_phantom_critical_routes() -> None:
    """A documented path that no longer exists is drift too (catchalls aside)."""
    registered = {p for _, p in _walk(app.routes)}
    documented = {p for _, p in _documented_routes()}
    # /assets is mounted only when a frontend build exists; its absence on a
    # buildless CI checkout is not route drift.
    phantom = sorted(documented - registered - {"/assets"})
    assert phantom == [], f"documented paths no longer registered: {phantom}"


@pytest.mark.parametrize(
    "required",
    [
        "/api/admin/reset",
        "/api/knowledge-base/ingest",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/live/portfolio",
        "/ws/feed",
    ],
)
def test_high_risk_routes_have_matrix_rows(required: str) -> None:
    assert any(path == required for _, path in _documented_routes())
