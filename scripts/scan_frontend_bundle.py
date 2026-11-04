#!/usr/bin/env python3
"""
scripts/scan_frontend_bundle.py — fail CI if a built frontend asset contains
secret-shaped material or leaks source maps.

Vite inlines every `VITE_*` value into public JavaScript. That is safe only for
public origins; a backend-only secret that reaches the bundle is a full
compromise. This scanner is intentionally self-contained (stdlib only) so it can
run immediately after `npm run build` on a clean CI runner.

Usage:
    python scripts/scan_frontend_bundle.py [--dist frontend/dist]

Exit codes: 0 clean, 1 findings, 2 missing build directory.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent

# Exact backend-only names. Do not add public `VITE_*` flags here.
SECRET_ENV_NAMES = (
    "ADMIN_TOKEN",
    "WALLET_KEYPAIR",
    "WALLET_KEYPAIR_JSON",
    "WALLET_KEYPAIR_PATH",
    "BACKUP_ENCRYPTION_KEY",
    "SECRET_STORE_KEY",
    "DEEPSEEK_API_KEY",
    "GROQ_API_KEY",
    "SOCIAL_LLM_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "FIRECRAWL_API_KEY",
    "SCRAPINGBEE_API_KEY",
    "SCRAPINGDOG_API_KEY",
    "ZENROWS_API_KEY",
    "SCRAPEOPS_API_KEY",
    "CAPTCHA_SECRET_KEY",
    "BIRDEYE_API_KEY",
    "DEXSCREENER_API_KEY",
    "JUPITER_API_KEY",
    "HELIUS_API_KEY",
    "ALERT_TELEGRAM_BOT_TOKEN",
    "ALERT_NTFY_TOKEN",
    "FOMO_PRIVY_REFRESH_TOKEN",
    "SUPABASE_DB_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
)

STRUCTURAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pem-private-key", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    )),
    ("json-keypair", re.compile(r"\[\s*\d+(?:\s*,\s*\d+){40,}\s*\]")),
    ("base58-keypair", re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{80,90}\b")),
    ("bearer-token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.~+/]{16,}=*")),
    ("basic-auth-url", re.compile(r"https?://[^:\s/]+:[^@\s/]{6,}@")),
    ("openai-token", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("groq-token", re.compile(r"\bgsk_[A-Za-z0-9_-]{20,}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("telegram-token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("ntfy-token", re.compile(r"\btk_[A-Za-z0-9_-]{16,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("source-map-reference", re.compile(r"[#@]\s*sourceMappingURL=")),
)


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    kind: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}"


def _decode(raw: bytes) -> str | None:
    if b"\x00" in raw[:4096]:
        return None  # binary asset (font/image); no text secrets
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def scan_text(text: str, path: Path = Path("<memory>")) -> list[Finding]:
    """Return findings for one decoded asset."""
    findings: list[Finding] = []
    for kind, pattern in STRUCTURAL_PATTERNS:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            findings.append(Finding(path, line, kind))
    for name in SECRET_ENV_NAMES:
        for match in re.finditer(rf"\b{re.escape(name)}\b", text):
            line = text.count("\n", 0, match.start()) + 1
            findings.append(Finding(path, line, f"backend-secret-name:{name}"))
    return findings


def scan_directory(dist: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(dist.rglob("*")):
        if not path.is_file():
            continue
        text = _decode(path.read_bytes())
        if text is None:
            continue
        findings.extend(scan_text(text, path))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist",
        type=Path,
        default=REPO_ROOT / "frontend" / "dist",
        help="built frontend directory (default: frontend/dist)",
    )
    args = parser.parse_args(argv)

    if not args.dist.is_dir():
        print(f"ERROR: build directory not found: {args.dist}", file=sys.stderr)
        return 2

    findings = scan_directory(args.dist)
    if findings:
        print(
            f"FAIL: {len(findings)} secret-shaped finding(s) in {args.dist}",
            file=sys.stderr,
        )
        # Never print the matched value, only location and rule.
        for finding in findings[:200]:
            try:
                display = finding.path.relative_to(REPO_ROOT)
            except ValueError:
                display = finding.path
            print(f"  {display}:{finding.line}: {finding.kind}", file=sys.stderr)
        if len(findings) > 200:
            print(f"  ... {len(findings) - 200} more", file=sys.stderr)
        return 1

    print(f"OK: no secret-shaped material in {args.dist}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
