"""
backend/tests/test_s4_migrations_rls.py — S4: PostgREST/Supabase exposure.

Every table created by the SQL migrations must have Row-Level Security
enabled, and the anonymous/authenticated PostgREST roles must have no table
or sequence privileges at all. The bot itself connects with the privileged
role, so these revocations do not affect trading.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MIGRATIONS = REPO_ROOT / "migrations" / "supabase"


def _sql_files() -> list[Path]:
    return sorted(MIGRATIONS.glob("*.sql"))


def _created_tables() -> dict[str, str]:
    tables: dict[str, str] = {}
    for path in _sql_files():
        for match in re.finditer(
                r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                r"([a-zA-Z_][a-zA-Z0-9_]*)",
                path.read_text(encoding="utf-8"), re.IGNORECASE):
            tables.setdefault(match.group(1).lower(), path.name)
    return tables


def _rls_declared_tables() -> set[str]:
    """Table names listed in a migration that enables RLS.

    The migrations use static ALTER statements and DO-block arrays. A table
    only counts when its name is in a file that also turns RLS on.
    """
    declared: set[str] = set()
    for path in _sql_files():
        text = path.read_text(encoding="utf-8")
        if "ENABLE ROW LEVEL SECURITY" not in text.upper():
            continue
        for block in re.finditer(r"ARRAY\s*\[(.*?)\]", text, re.DOTALL):
            declared.update(
                x.lower() for x in re.findall(r"'([a-zA-Z_][a-zA-Z0-9_]*)'",
                                              block.group(1)))
        for match in re.finditer(
                r"ALTER\s+TABLE\s+([a-zA-Z_][a-zA-Z0-9_]*)"
                r"\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY",
                text, re.IGNORECASE):
            declared.add(match.group(1).lower())
    return declared


def test_every_migration_table_has_rls_enabled():
    created = _created_tables()
    assert created, "no CREATE TABLE statements found in migrations"
    declared = _rls_declared_tables()
    missing = sorted(set(created) - declared)
    assert missing == [], (
        "tables created in migrations without ENABLE ROW LEVEL SECURITY: "
        + ", ".join(f"{t} ({created[t]})" for t in missing)
    )


def test_anon_and_authenticated_roles_are_revoked():
    text = "\n".join(p.read_text(encoding="utf-8") for p in _sql_files())
    normalised = re.sub(r"\s+", " ", text).lower()

    required = [
        "revoke all on all tables in schema public from anon, authenticated",
        "revoke all on all sequences in schema public from anon, authenticated",
        "alter default privileges in schema public "
        "revoke all on tables from anon, authenticated",
    ]
    for statement in required:
        assert statement in normalised, (
            f"missing PostgREST lock-down statement: {statement!r}"
        )


def test_newest_migration_is_the_postgrest_lockdown():
    newest = _sql_files()[-1]
    assert newest.name == "007_revoke_anon_authenticated.sql", (
        "new PostgREST exposure requires a new numbered migration; "
        f"newest is {newest.name}"
    )


def test_no_permissive_true_policy_reaches_postgrest_roles():
    for path in _sql_files():
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(
                r"CREATE\s+POLICY.*?;", text,
                re.IGNORECASE | re.DOTALL):
            policy = match.group(0)
            if re.search(r"\bTO\s+(anon|authenticated|public)\b", policy,
                         re.IGNORECASE):
                assert not re.search(r"USING\s*\(\s*true\s*\)", policy,
                                     re.IGNORECASE), (
                    f"{path.name}: allow-all policy reaches a public role: "
                    + policy.strip()
                )
