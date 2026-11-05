"""
backend/tests/test_s4_sql_allowlist.py — S4: interpolated SQL identifiers.

Only two query families build SQL by interpolation: the reset/wipe table
names and the shared feed/trade column lists. Both must be module constants
whose contents are validated, never request input.
"""
from __future__ import annotations

import re

import pytest

from api import db, db_pg

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _column_names(block: str) -> list[str]:
    names: list[str] = []
    for raw in block.splitlines():
        line = raw.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        # "ts::text AS ts" -> take the explicit alias, else the column name.
        alias = re.search(r"\bAS\s+([a-z_][a-z0-9_]*)$", line, re.IGNORECASE)
        if alias:
            names.append(alias.group(1))
            continue
        head = line.split("::", 1)[0].strip()
        if _IDENT.match(head):
            names.append(head)
    return names


def test_feed_and_trade_column_constants_are_plain_identifiers():
    for name, block in (("_FEED_COLS", db_pg._FEED_COLS),
                        ("_TRADE_COLS", db_pg._TRADE_COLS)):
        columns = _column_names(block)
        assert columns, f"{name} parsed empty"
        for column in columns:
            assert _IDENT.match(column), f"{name} contains non-identifier: {column!r}"
        assert ";" not in block, f"{name} contains a statement separator"
        assert "--" not in block, f"{name} contains a SQL comment"
        assert '"' not in block, f"{name} contains a quoted identifier"


def test_reset_table_allowlist_is_the_complete_constant():
    expected = {
        "feed_events", "market_regime", "decision_commits", "events",
        "memories", "theses", "daily_stats", "llm_call_usage", "trades",
    }
    assert db._ALLOWED_RESET_TABLES == frozenset(expected)
    assert db_pg._ALLOWED_RESET_TABLES == frozenset(expected)


@pytest.mark.parametrize("module", [db, db_pg])
def test_reset_table_guard_rejects_injection(module):
    guard = module._assert_allowed_reset_table
    for table in ("feed_events", "trades"):
        guard(table)

    for hostile in (
        "users",
        "feed_events; DROP TABLE trades",
        "feed_events--",
        "feed_events ",
        "",
        "portfolio_state",
    ):
        with pytest.raises(ValueError):
            guard(hostile)
