"""
backend/tests/test_s4_xss_surface.py — S4: XSS and upload surface guards.

The dashboard renders attacker-controlled token names, descriptions, and
social text. React escapes text by default; this test fails if a future
change introduces a raw-HTML sink or a dangerous URL sink. The Playwright
suite (`frontend/e2e/xss.spec.ts`) proves the rendered result in a browser.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
BACKEND = REPO_ROOT / "backend"

_RAW_HTML_SINKS = re.compile(
    r"dangerouslySetInnerHTML|\binnerHTML\s*=|"
    r"\bouterHTML\s*=|\bdocument\.write\s*\(|"
    r"\beval\s*\(|\bnew\s+Function\s*\(")
_JAVASCRIPT_URL = re.compile(r"(?:href|src)\s*=\s*[{[][^}\n]*javascript:", re.IGNORECASE)


def _source_files(root: Path):
    for path in root.rglob("*"):
        if path.suffix in {".ts", ".tsx", ".js", ".jsx"} and path.is_file():
            yield path


def test_frontend_has_no_raw_html_or_eval_sinks():
    violations = []
    for path in _source_files(FRONTEND_SRC):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in _RAW_HTML_SINKS.finditer(text):
            line = text[:match.start()].count("\n") + 1
            violations.append(f"{path.relative_to(REPO_ROOT)}:{line} {match.group(0)}")
        for match in _JAVASCRIPT_URL.finditer(text):
            line = text[:match.start()].count("\n") + 1
            violations.append(
                f"{path.relative_to(REPO_ROOT)}:{line} javascript: URL")
    assert violations == [], "\n".join(violations)


def test_external_links_are_noopener_and_https():
    violations = []
    for path in _source_files(FRONTEND_SRC):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r"<a\b[^>]*target=\"_blank\"[^>]*>", text):
            tag = match.group(0)
            if "noopener" not in tag or "noreferrer" not in tag:
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}: target=_blank without "
                    "rel=noopener noreferrer")
        for match in re.finditer(r"href=\{`(https?://[^`]+)`\}", text):
            if not match.group(1).startswith("https://"):
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}: non-https external href")
    assert violations == [], "\n".join(violations)


def test_no_multipart_upload_endpoint_exists():
    """If an upload endpoint is ever added it needs a route-matrix entry,
    a size/type policy, and its own security review."""
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "UploadFile" in text or "multipart/form-data" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], (
        "multipart upload endpoint introduced without an S4 review: "
        + ", ".join(offenders))
