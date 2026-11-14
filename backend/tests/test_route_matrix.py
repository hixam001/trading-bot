"""
backend/tests/test_route_matrix.py — S2: security docs cannot drift from app.

Every registered HTTP route, WebSocket, and mount must have a row in
docs/10_SECURITY.md. Adding an endpoint without documenting its auth posture
fails here, before it can ship.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import Mount, Route

from api.main import app
from api import auth, db
from api.rate_limiter import limiter
import config

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOC = REPO_ROOT / "docs" / "10_SECURITY.md"
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


def _documented_auth() -> dict[tuple[str, str], str]:
    rows: dict[tuple[str, str], str] = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip().strip("`") for c in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        method, path = cells[0].upper(), cells[1]
        if method in METHODS and path.startswith("/"):
            assert (method, path) not in rows, f"duplicate matrix row: {method} {path}"
            rows[(method, path)] = cells[2]
    return rows


def _documented_routes() -> set[tuple[str, str]]:
    return set(_documented_auth())


def test_security_doc_exists() -> None:
    assert DOC.is_file(), "docs/10_SECURITY.md is required"


def test_every_registered_route_is_in_the_matrix() -> None:
    registered = set(_walk(app.routes))
    documented = _documented_routes()
    missing = sorted(registered - documented)
    assert missing == [], (
        "routes missing from docs/10_SECURITY.md matrix: "
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


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,posture", [
    (method, path, posture)
    for (method, path), posture in _documented_auth().items()
])
async def test_matrix_auth_matches_the_real_app(method, path, posture, tmp_path,
                                               monkeypatch):
    """Probe from an internet peer, without cookies, credentials or bypasses."""
    assert posture in {"Public", "Operator", "Loopback/Admin", "Cookie+CSRF", "Concealed"}
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "matrix.db")
    monkeypatch.setattr(config, "LIVE_BOOK_PUBLIC", False)
    monkeypatch.setattr(config, "ALLOW_SIGNUP", False)
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    monkeypatch.setattr(config, "CAPTCHA_ENABLED", False)
    await db.init_db()
    monkeypatch.setattr(app.state, "narrator", SimpleNamespace(
        _main_llm=SimpleNamespace(health=AsyncMock(return_value=True))), raising=False)
    from data_providers.mock import MockProvider
    monkeypatch.setattr(app.state, "provider", MockProvider(), raising=False)
    limiter.clear()
    auth._FAILED_ATTEMPTS.clear()
    auth._GLOBAL_FAILED_ATTEMPTS.clear()
    concrete = (path.replace("{mint}", "So11111111111111111111111111111111111111112")
                .replace("{full_path:path}", "matrix-probe"))
    if method == "WS":
        # Exercise the actual websocket handler, without opening a socket or
        # starting the live broadcaster. A public route accepts the handshake.
        messages = iter([{"type": "websocket.connect"},
                         {"type": "websocket.disconnect", "code": 1000}])
        sent = []

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        await app({"type": "websocket", "asgi": {"version": "3.0"},
                   "scheme": "wss", "path": concrete, "root_path": "",
                   "query_string": b"", "headers": [(b"host", b"bot.example")],
                   "client": ("198.51.100.20", 54321),
                   "server": ("bot.example", 443), "subprotocols": []}, receive, send)
        assert posture == "Public"
        assert any(message["type"] == "websocket.accept" for message in sent)
        return

    transport = httpx.ASGITransport(app=app, client=("198.51.100.20", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="https://bot.example") as client:
        kwargs = {}
        if method == "POST":
            # Valid ingest schema reaches its auth barrier. Public login gets
            # an empty body: schema rejection is separate from auth posture.
            kwargs["json"] = {"documents": []} if path.endswith("/ingest") else {}
        response = await client.request("GET" if method == "MOUNT" else method,
                                        concrete + ("/" if method == "MOUNT" else ""),
                                        **kwargs)
    label = f"{method} {path} ({posture}): {response.status_code} {response.text[:200]}"
    if (method, path) == ("POST", "/api/auth/logout"):
        assert posture == "Cookie+CSRF"
        assert response.status_code == 200, label  # documented idempotent no-cookie exemption
        assert response.json() == {"status": "logged_out"}
    elif posture == "Public":
        assert response.status_code not in {401, 403}, label
        assert response.status_code < 500, label
    elif posture == "Concealed":
        assert response.status_code == 404, label
    else:
        assert response.status_code in {401, 403, 404, 429}, label
