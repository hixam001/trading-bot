"""
DB surface parity guard (review §2.1 / §8.4).

db.py (SQLite) and db_pg.py (Postgres) are dialect twins merged at import
time via ``globals().update(...)``. If a public function is missing from
db_pg.py, the SQLite implementation silently stays active in the Postgres
runtime — a silent dialect break. This test fails loudly on any drift.
"""

import ast
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PY = os.path.join(HERE, "..", "api", "db.py")
DB_PG_PY = os.path.join(HERE, "..", "api", "db_pg.py")


def _public_functions(path: str) -> set:
    tree = ast.parse(open(path).read())
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }


def test_db_pg_exposes_every_public_function_of_db() -> None:
    sqlite_surface = _public_functions(DB_PY)
    pg_surface = _public_functions(DB_PG_PY)
    missing = sqlite_surface - pg_surface
    assert not missing, (
        "db_pg.py is missing public function(s) present in db.py: "
        f"{sorted(missing)}. Because the two modules are merged via "
        "globals().update(), these would silently run the SQLite "
        "implementation against Postgres."
    )


def test_db_pg_additions_are_explicit_helpers() -> None:
    """Postgres-only additions must stay an explicit, documented allow-list."""
    allowed = {"close_pool"}
    pg_only = _public_functions(DB_PG_PY) - _public_functions(DB_PY)
    unexpected = pg_only - allowed
    assert not unexpected, (
        f"db_pg.py defines public function(s) absent from db.py: "
        f"{sorted(unexpected)}. Add them to db.py (for parity) or to the "
        "explicit allow-list in this test with a justification comment."
    )
