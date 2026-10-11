"""
backend/tests/test_docs_security.py — Documentation security tests (§77 A1).

Ensures documentation never instructs operators to inject admin tokens in reverse
proxy headers (which exposes admin credentials to internet visitors) or to bake
admin tokens into frontend environment variables.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_docs_do_not_contain_header_up_admin_token():
    """Verify no doc suggests Caddy header_up X-Admin-Token."""
    pattern = re.compile(r"header_up\s+X-Admin-Token", re.IGNORECASE)
    doc_files = list((REPO_ROOT / "docs").rglob("*.md"))
    doc_files.append(REPO_ROOT / "README.md")

    violations = []
    for doc in doc_files:
        if not doc.exists():
            continue
        content = doc.read_text(encoding="utf-8")
        if pattern.search(content):
            violations.append(str(doc.relative_to(REPO_ROOT)))

    assert not violations, f"Documentation contains dangerous 'header_up X-Admin-Token' in: {violations}"


def test_docs_do_not_contain_vite_admin_token_value():
    """Verify no doc suggests setting a value for VITE_ADMIN_TOKEN."""
    pattern = re.compile(r"VITE_ADMIN_TOKEN\s*=\s*[^\s#]+", re.IGNORECASE)
    doc_files = list((REPO_ROOT / "docs").rglob("*.md"))
    doc_files.append(REPO_ROOT / "README.md")

    violations = []
    for doc in doc_files:
        if not doc.exists():
            continue
        content = doc.read_text(encoding="utf-8")
        matches = pattern.findall(content)
        # Empty assignment (e.g. VITE_ADMIN_TOKEN=) or comment is fine, non-empty value is a violation
        non_empty = [m for m in matches if m.split("=")[1].strip()]
        if non_empty:
            violations.append((str(doc.relative_to(REPO_ROOT)), non_empty))

    assert not violations, f"Documentation contains VITE_ADMIN_TOKEN with a value in: {violations}"
