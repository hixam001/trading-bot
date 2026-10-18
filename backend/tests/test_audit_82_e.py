"""
test_audit_82_e.py — Phase E tests for verifiable, reproducible mutation testing.

Verifies:
1. docs/17_MUTATION_LOG.md contains only references to real tests that actually exist
   in backend/tests/ (def test_...).
2. scripts/mutation_check.py declarative table is non-empty and every mutation's
   target file and old text exists.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_mutation_log_references_real_tests():
    """
    §82 Phase E:
    Parses docs/17_MUTATION_LOG.md, extracts every backticked `test_...` name,
    and asserts each one exists in backend/tests/ (def <name>).
    Stops fabricated test names from ever landing in documentation again.
    """
    doc_path = REPO_ROOT / "docs" / "17_MUTATION_LOG.md"
    assert doc_path.exists(), f"Mutation log not found: {doc_path}"

    content = doc_path.read_text(encoding="utf-8")

    # Extract all backticked test names matching `test_...`
    referenced_tests = set(re.findall(r"`(test_\w+)`", content))
    assert len(referenced_tests) > 0, "No tests found in docs/17_MUTATION_LOG.md"

    # Find all test definitions across backend/tests/
    tests_dir = REPO_ROOT / "backend" / "tests"
    defined_tests = set()

    for test_file in tests_dir.rglob("*.py"):
        file_text = test_file.read_text(encoding="utf-8")
        for match in re.finditer(r"^\s*(?:async\s+)?def\s+(test_\w+)\s*\(", file_text, re.MULTILINE):
            defined_tests.add(match.group(1))

    # Assert every referenced test exists
    missing = referenced_tests - defined_tests
    assert not missing, f"docs/17_MUTATION_LOG.md references non-existent tests: {sorted(missing)}"


def test_mutation_harness_declarations_valid():
    """
    §82 Phase E:
    Ensures scripts/mutation_check.py definitions all have exact matches in the codebase.
    """
    from scripts.mutation_check import MUTATIONS

    assert len(MUTATIONS) >= 15, f"Expected at least 15 mutations, got {len(MUTATIONS)}"

    for m in MUTATIONS:
        target = REPO_ROOT / m.file_path
        assert target.exists(), f"Mutation target file does not exist: {m.file_path}"
        text = target.read_text(encoding="utf-8")
        count = text.count(m.exact_old_text)
        assert count == 1, (
            f"Mutation {m.mutation_id} in {m.file_path}: expected 1 occurrence of exact text, found {count}"
        )
