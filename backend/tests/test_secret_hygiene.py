"""
backend/tests/test_secret_hygiene.py — S1 repo/build secret-hygiene proofs.

Two independent gates:
1. Runtime material (`.env`, keypairs, encrypted archives, logs, local DBs,
   backups/state) must be git-ignored and must never be tracked.
2. The built frontend must not contain backend-only env names, secret-shaped
   strings, or source-map references. The same scanner runs in CI right after
   `npm run build`.
"""
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from scripts.scan_frontend_bundle import scan_directory, scan_text

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        "frontend/.env.local",
        "secrets/wallet-keypair.json",
        "wallet-keypair.json",
        "state/executions.json",
        "backups/state-20260101T000000Z.tar.gz.enc",
        "logs/app.log",
        "backend/trading_bot.db",
        "backend/live_execution/state/live.json",
        "some-archive.enc",
        "private-key.pem",
    ],
)
def test_runtime_material_is_git_ignored(path: str) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "-q", path],
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"{path} is not covered by .gitignore"


def test_no_runtime_material_is_tracked() -> None:
    tracked = _git("ls-files", "-z").split("\x00")
    forbidden_names = {
        ".env",
        "wallet-keypair.json",
        "id.json",
        "blocklist_state.json",
        ".fomo_privy.json",
        ".supabase_fp.txt",
    }
    forbidden_suffixes = (
        ".db",
        ".db-wal",
        ".db-shm",
        ".enc",
        ".pem",
        ".key",
        ".keypair",
        ".p12",
        ".pfx",
    )
    offenders = []
    for rel in tracked:
        if not rel:
            continue
        name = Path(rel).name
        if name in forbidden_names:
            offenders.append(rel)
        elif name.startswith(".env.") and name != ".env.example":
            offenders.append(rel)
        elif name.endswith(forbidden_suffixes):
            offenders.append(rel)
        elif rel.startswith(("logs/", "secrets/", "backups/", "state/", ".run/")):
            offenders.append(rel)
    assert offenders == [], f"runtime/secret material is tracked: {offenders}"


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("json-keypair", "[" + ",".join(str(i % 256) for i in range(64)) + "]"),
        ("base58-keypair", "4" * 88),
        (
            "pem-private-key",
            "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----",
        ),
        ("bearer-token", "Bearer " + "A" * 24),
        ("basic-auth-url", "https://user:sup3rsecret@example.com"),
        ("openai-token", "sk-" + "a" * 24),
        ("groq-token", "gsk_" + "b" * 24),
        ("github-token", "ghp_" + "c" * 24),
        ("telegram-token", "1234567890:" + "d" * 35),
        ("ntfy-token", "tk_" + "e" * 24),
        ("aws-access-key", "AKIA" + "F" * 16),
        ("source-map-reference", "//# sourceMappingURL=index.js.map"),
        ("backend-secret-name:ADMIN_TOKEN", "const x = 'ADMIN_TOKEN'"),
        (
            "backend-secret-name:BACKUP_ENCRYPTION_KEY",
            "BACKUP_ENCRYPTION_KEY",
        ),
    ],
)
def test_frontend_scanner_detects_secret_shapes(kind: str, payload: str) -> None:
    findings = scan_text(payload)
    assert any(f.kind == kind for f in findings), findings


def test_frontend_scanner_passes_a_clean_bundle(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<div id="root"></div><script src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    (dist / "assets" / "app.js").write_text(
        'const api = import.meta.env.VITE_API_BASE_URL || "";',
        encoding="utf-8",
    )
    assert scan_directory(dist) == []


def test_built_frontend_contains_no_secrets_when_present() -> None:
    dist = REPO_ROOT / "frontend" / "dist"
    if not dist.is_dir():
        pytest.skip("frontend/dist not built; CI runs the scanner after npm run build")
    findings = scan_directory(dist)
    assert findings == [], "\n".join(str(f) for f in findings)
