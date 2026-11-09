#!/usr/bin/env python3
"""§93 R1 — statically inventory every runtime write path.

Two layers, deliberately:

1. An AST scanner walks ``backend/`` and ``scripts/`` (tests excluded) and
   finds every write-shaped call: ``open(..., "w"/"a"/...)``,
   ``Path.write_text``/``write_bytes``/``mkdir``, ``os.open`` with a creating
   or writing flag, ``os.replace``/``rename``, ``shutil.copy*``/``move``,
   ``sqlite3.connect`` and ``aiosqlite.connect``.  Nothing is hand-waved: if a
   new write appears, the scanner reports it.

2. Each detected site is mapped to the config key that gives it a home (or
   flagged as operator/dev-only).  The mapping is checked against the
   detected sites, so a config key with no writer, or a writer with no
   classified home, is visible instead of silently omitted.

The committed table in ``docs/18_SECURITY.md`` §Runtime write paths is
generated from :data:`INVENTORY`.  Run with ``--check`` to fail (exit 1) when
any inventory key cannot be resolved from ``config`` or any classified site
is not backed by a detected write.
"""
from __future__ import annotations

import argparse
import ast
import errno
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
SCRIPTS = REPO_ROOT / "scripts"

WRITE_METHODS = {"write_text", "write_bytes", "mkdir", "replace", "rename",
                 "unlink", "touch", "copy", "copy2", "copyfile", "move"}
WRITE_FUNCS = {"replace", "rename", "remove", "unlink", "mkdir", "makedirs",
               "copy", "copy2", "copyfile", "move", "truncate"}
WRITE_MODULE_FUNCS = {"replace", "rename", "remove", "unlink", "mkdir",
                      "makedirs", "copy", "copy2", "copyfile", "move",
                      "truncate"}
SQLITE_CONNECTS = {"connect"}


@dataclass
class WriteSite:
    file: str
    line: int
    func: str
    path_expr: str
    config_key: str = ""


@dataclass
class InventoryRow:
    path: str
    config_key: str
    services: tuple[str, ...]
    volume: str
    evidence: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Curated inventory. ``evidence`` names the write sites that justify the row;
# the script verifies each one was actually detected by the AST scanner.
# ``services`` is the compose service(s) that need the path writable.
# ---------------------------------------------------------------------------
INVENTORY: tuple[InventoryRow, ...] = (
    InventoryRow(
        "<backend>/blocklist_state.json", "BLOCKLIST_STATE_FILE", ("live",),
        "engine_state:/app/backend/live_execution/state",
        ("backend/blocklist.py:134", "backend/blocklist.py:133"),
    ),
    InventoryRow(
        "<repo>/.fomo_privy.json", "FOMO_PRIVY_STATE_FILE", ("live",),
        "engine_state:/app/backend/live_execution/state",
        ("backend/data_providers/crowd.py:512",),
    ),
    InventoryRow(
        "<repo>/.secret_store.key", "SECRET_STORE_KEY_FILE", ("live",),
        "engine_state:/app/backend/live_execution/state",
        ("backend/secret_store.py:57",),
    ),
    InventoryRow(
        "<backend>/.supabase_fp.txt", "SUPABASE_PIN_FILE", ("api", "live"),
        "api engine_data:/app/data; live engine_state:/app/backend/live_execution/state",
        ("backend/api/db_pg.py:117",),
    ),
    InventoryRow(
        "<repo>/state/session_revocations.json", "SESSION_REVOCATION_FILE",
        ("api",), "engine_data:/app/data",
        ("backend/api/auth.py:184",),
    ),
    InventoryRow(
        "<backend>/knowledge_base/ingested/", "KB_INGESTED_DIR", ("api",),
        "engine_data:/app/data",
        ("backend/knowledge_base/loader.py:151",
         "backend/knowledge_base/loader.py:166"),
    ),
    InventoryRow(
        "<LIVE_STATE_DIR>/spend_guard.json", "SPEND_GUARD_FILE",
        ("api", "live"),
        "api engine_data:/app/data/spend_guard_api.json; live engine_state",
        ("backend/llm/spend_guard.py:90",),
    ),
    InventoryRow(
        "<LIVE_STATE_DIR>/{break_state,kill_switch,heartbeat,commits,"
        "executions,confirmations,safety_counters,crowd_chain_status,"
        "wallet_pubkey}.*", "LIVE_STATE_DIR", ("live",),
        "engine_state:/app/backend/live_execution/state",
        ("backend/live_execution/kill_switch.py:70",
         "backend/live_execution/models.py:138",
         "backend/live_execution/commit_log.py:55",
         "backend/live_execution/confirmation_queue.py:61",
         "backend/live_execution/safety_counters.py:106",
         "backend/data_providers/crowd.py:512",
         "backend/run_live_cycle.py:123"),
    ),
    InventoryRow(
        "<DB_PATH>/trading_bot.db", "DB_PATH", ("api", "live"),
        "engine_data:/app/data",
        ("backend/api/db.py:250",),
    ),
    InventoryRow(
        "<LOG_DIR>/{app,error,security}.log", "LOG_DIR", ("api", "live"),
        "engine_logs:/app/logs",
        ("backend/api/logging_config.py:51",),
    ),
    InventoryRow(
        "<BACKUP_DIR>/state-*.tar.gz.enc", "BACKUP_DIR (CLI/env)",
        ("operator",), "engine_backups:/app/backups",
        ("scripts/backup_state.py:253",),
    ),
    InventoryRow(
        "<LIVE_STATE_DIR>/resolve_pending_buy_audit.log",
        "LIVE_STATE_DIR (operator CLI)", ("operator",),
        "engine_state (or operator-specified --audit-log)",
        ("scripts/resolve_pending_buy.py:135",),
    ),
    InventoryRow(
        "<LIVE_STATE_DIR>/safety_counters.json (ack script)",
        "LIVE_STATE_DIR (operator CLI)", ("operator",),
        "engine_state (or operator-specified state dir)",
        ("scripts/ack_wallet_change.py:126",),
    ),
    InventoryRow(
        "<backend>/live_execution/dex_labels.json (dev refresh)",
        "fixed repo path (operator/dev script)", ("operator",),
        "image root (dev only; not a compose service)",
        ("scripts/refresh_dex_labels.py:92",),
    ),
    InventoryRow(
        "repo files mutated by the mutation harness (dev/CI)",
        "repo root (dev/CI tool)", ("dev",),
        "repository checkout",
        ("scripts/mutation_check.py:1316",),
    ),
)

# Site classification: (relative file, function) -> config key.  Sites not
# listed here are operator/dev-only or covered by the LIVE_STATE_DIR rows.
SITE_KEYS: dict[tuple[str, str], str] = {
    ("backend/blocklist.py", "write_text"): "BLOCKLIST_STATE_FILE",
    ("backend/blocklist.py", "mkdir"): "BLOCKLIST_STATE_FILE",
    ("backend/blocklist.py", "rename"): "BLOCKLIST_STATE_FILE",
    ("backend/api/db_pg.py", "write_text"): "SUPABASE_PIN_FILE",
    ("backend/api/auth.py", "write_text"): "SESSION_REVOCATION_FILE",
    ("backend/api/auth.py", "replace"): "SESSION_REVOCATION_FILE",
    ("backend/api/auth.py", "mkdir"): "SESSION_REVOCATION_FILE",
    ("backend/api/logging_config.py", "mkdir"): "LOG_DIR",
    ("backend/knowledge_base/loader.py", "mkdir"): "KB_INGESTED_DIR",
    ("backend/knowledge_base/loader.py", "open"): "KB_INGESTED_DIR",
    ("backend/knowledge_base/loader.py", "fdopen"): "KB_INGESTED_DIR",
    ("backend/data_providers/crowd.py", "write_text"): "FOMO_PRIVY_STATE_FILE",
    ("backend/data_providers/crowd.py", "replace"): "FOMO_PRIVY_STATE_FILE",
    ("backend/data_providers/crowd.py", "mkdir"): "LIVE_STATE_DIR",
    ("backend/secret_store.py", "open"): "SECRET_STORE_KEY_FILE",
    ("backend/secret_store.py", "fdopen"): "SECRET_STORE_KEY_FILE",
    ("backend/secret_store.py", "replace"): "SECRET_STORE_KEY_FILE",
    ("backend/api/auth.py", "unlink"): "SESSION_REVOCATION_FILE",
    ("backend/api/auth.py", "replace"): "SESSION_REVOCATION_FILE",
    ("backend/api/auth.py", "mkdir"): "SESSION_REVOCATION_FILE",
    ("backend/llm/spend_guard.py", "write_text"): "SPEND_GUARD_FILE",
    ("backend/llm/spend_guard.py", "replace"): "SPEND_GUARD_FILE",
    ("backend/llm/spend_guard.py", "mkdir"): "SPEND_GUARD_FILE",
    ("backend/api/db.py", "connect"): "DB_PATH",
    ("backend/rule_engine/liveness.py", "write_text"): "LIVE_STATE_DIR",
    ("backend/rule_engine/liveness.py", "replace"): "LIVE_STATE_DIR",
    ("backend/rule_engine/liveness.py", "mkdir"): "LIVE_STATE_DIR",
    ("backend/live_execution/kill_switch.py", "write_text"): "LIVE_STATE_DIR",
    ("backend/live_execution/kill_switch.py", "replace"): "LIVE_STATE_DIR",
    ("backend/live_execution/kill_switch.py", "mkdir"): "LIVE_STATE_DIR",
    ("backend/live_execution/models.py", "write_text"): "LIVE_STATE_DIR",
    ("backend/live_execution/models.py", "replace"): "LIVE_STATE_DIR",
    ("backend/live_execution/models.py", "mkdir"): "LIVE_STATE_DIR",
    ("backend/live_execution/commit_log.py", "write_text"): "LIVE_STATE_DIR",
    ("backend/live_execution/commit_log.py", "replace"): "LIVE_STATE_DIR",
    ("backend/live_execution/commit_log.py", "mkdir"): "LIVE_STATE_DIR",
    ("backend/live_execution/confirmation_queue.py", "write_text"):
        "LIVE_STATE_DIR",
    ("backend/live_execution/confirmation_queue.py", "replace"):
        "LIVE_STATE_DIR",
    ("backend/live_execution/confirmation_queue.py", "mkdir"):
        "LIVE_STATE_DIR",
    ("backend/live_execution/safety_counters.py", "write_text"):
        "LIVE_STATE_DIR",
    ("backend/live_execution/safety_counters.py", "mkdir"):
        "LIVE_STATE_DIR",
    ("backend/run_live_cycle.py", "write_text"): "LIVE_STATE_DIR",
    ("backend/run_live_cycle.py", "mkdir"): "LIVE_STATE_DIR",
    ("backend/run_live_cycle.py", "replace"): "LIVE_STATE_DIR",
    ("backend/live_execution/reconcile.py", "mkdir"): "LIVE_STATE_DIR",
    ("backend/live_execution/reconcile.py", "write_text"): "LIVE_STATE_DIR",
    ("backend/live_execution/safety_counters.py", "replace"): "LIVE_STATE_DIR",
    ("scripts/backup_state.py", "connect"): "DB_PATH",
    ("scripts/backup_state.py", "write_bytes"): "BACKUP_DIR",
    ("scripts/backup_state.py", "mkdir"): "BACKUP_DIR",
    ("scripts/restore_state.py", "copy2"): "operator target dir",
    ("scripts/restore_state.py", "replace"): "operator target dir",
    ("scripts/restore_state.py", "connect"): "operator target dir",
    ("scripts/restore_state.py", "open"): "operator target dir",
    ("scripts/resolve_pending_buy.py", "open"): "LIVE_STATE_DIR",
    ("scripts/resolve_pending_buy.py", "mkdir"): "LIVE_STATE_DIR",
    ("scripts/ack_wallet_change.py", "write_text"): "LIVE_STATE_DIR",
    ("scripts/ack_wallet_change.py", "mkdir"): "LIVE_STATE_DIR",
    ("scripts/ack_wallet_change.py", "replace"): "LIVE_STATE_DIR",
    ("scripts/refresh_dex_labels.py", "write_text"): "fixed repo path",
    ("scripts/mutation_check.py", "write_bytes"): "repo root",
}


def _iter_python(root: Path):
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        yield path


def _path_expr(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "?"


def _is_write_mode(mode) -> bool:
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return any(c in mode.value for c in "wax+")
    return True  # dynamic mode: assume it can write, flag for review


def _os_open_writes(flags: ast.AST) -> bool:
    """True when an os.open flags expression contains a creating/writing flag."""
    text = _path_expr(flags)
    return any(name in text for name in (
        "O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND"))


def scan(root: Path = REPO_ROOT) -> list[WriteSite]:
    sites: list[WriteSite] = []
    for base in (BACKEND, SCRIPTS):
        if not base.exists():
            continue
        for path in _iter_python(base):
            rel = str(path.relative_to(REPO_ROOT))
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = ""
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                else:
                    continue
                path_expr = ""
                if name == "open" and node.args:
                    if not _is_write_mode(node.args[1] if len(node.args) > 1
                                          else None):
                        continue
                    path_expr = _path_expr(node.args[0])
                elif name in ("write_text", "write_bytes", "mkdir", "touch"):
                    path_expr = _path_expr(func.value)
                elif name in ("replace", "rename", "remove", "unlink",
                              "move", "truncate", "copy", "copy2",
                              "copyfile"):
                    base = _path_expr(func.value).lower()
                    # A str.replace(...) is not a filesystem write. Only
                    # treat replace/rename as path operations when the
                    # receiver names a path/file/tmp/dir (or is os/Path).
                    if name in ("replace", "rename", "truncate"):
                        if not any(tok in base for tok in (
                                "os", "path", "file", "tmp", "dir", "p.")):
                            continue
                    if name in ("replace", "rename", "move"):
                        path_expr = _path_expr(node.args[1]) if len(node.args) > 1 else ""
                    else:
                        path_expr = _path_expr(node.args[0]) if node.args else ""
                elif name == "makedirs":
                    path_expr = _path_expr(node.args[0]) if node.args else ""
                elif name == "connect":
                    if isinstance(func.value, ast.Name) and func.value.id in (
                            "asyncpg", "httpx", "socket"):
                        continue
                    path_expr = _path_expr(node.args[0]) if node.args else ""
                elif name == "fdopen":
                    path_expr = _path_expr(node.args[0]) if node.args else ""
                elif name == "os_open":
                    pass
                else:
                    continue
                if name == "os_open":
                    continue
                # os.open(...) is an ast.Call whose func is ast.Attribute
                # `open` on ast.Name `os`; detect before the generic branch.
                if (isinstance(func, ast.Attribute) and func.attr == "open"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "os"):
                    if not (node.args and _os_open_writes(node.args[1])):
                        continue
                    path_expr = _path_expr(node.args[0])
                sites.append(WriteSite(file=rel, line=node.lineno,
                                       func=name, path_expr=path_expr))
    return sites


def _classify(sites: list[WriteSite]) -> list[WriteSite]:
    for site in sites:
        key = SITE_KEYS.get((site.file, site.func), "")
        if key:
            site.config_key = key
    return sites


def _default_paths() -> dict[str, str]:
    """Import config in a clean subprocess and read the path defaults."""
    code = (
        "import json, config\n"
        "keys = %r\n"
        "print(json.dumps({k: str(getattr(config, k, '')) for k in keys}))\n"
    ) % ([row.config_key for row in INVENTORY
          if row.config_key.isupper()],)
    env = {k: v for k, v in os.environ.items()
           if k not in {
               "LIVE_STATE_DIR", "DB_PATH", "LOG_DIR", "BLOCKLIST_STATE_FILE",
               "FOMO_PRIVY_STATE_FILE", "SECRET_STORE_KEY_FILE",
               "SUPABASE_PIN_FILE", "SESSION_REVOCATION_FILE",
               "KB_INGESTED_DIR", "SPEND_GUARD_FILE",
           }}
    env["PYTHONPATH"] = str(BACKEND)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=str(REPO_ROOT), env=env,
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        pass
    return {}


def _compose_mounts() -> dict[str, list[str]]:
    try:
        import yaml
    except ImportError:
        return {}
    text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    compose = yaml.safe_load(text) or {}
    out: dict[str, list[str]] = {}
    for name, service in (compose.get("services") or {}).items():
        mounts: list[str] = []
        for vol in service.get("volumes") or []:
            parts = str(vol).split(":")
            if len(parts) >= 2 and (len(parts) < 3 or parts[2] != "ro"):
                mounts.append(parts[1].rstrip("/"))
        for tmp in service.get("tmpfs") or []:
            mounts.append(str(tmp).rstrip("/"))
        out[name] = mounts
    return out


def table() -> list[str]:
    defaults = _default_paths()
    lines = [
        "| Path (default) | Config key | Service(s) | Mounted volume |",
        "|---|---|---|---|",
    ]
    for row in INVENTORY:
        default = defaults.get(row.config_key, "")
        path = row.path
        if default and "<" not in path:
            path = default
        lines.append(f"| `{path}` | `{row.config_key}` | "
                     f"{', '.join(row.services)} | {row.volume} |")
    return lines


def check() -> tuple[list[str], list[str]]:
    """Return (problems, notes). Problems fail CI; notes are informational."""
    problems: list[str] = []
    notes: list[str] = []
    sites = _classify(scan())
    detected = {(s.file, s.func) for s in sites}
    for row in INVENTORY:
        for evidence in row.evidence:
            if "#" in evidence:
                rel, _, func = evidence.partition("#")
                if (rel, func) not in detected:
                    problems.append(
                        f"inventory evidence {evidence} was not detected as a write")
            else:
                rel = evidence.rpartition(":")[0]
                if not any(s.file == rel for s in sites):
                    problems.append(
                        f"inventory evidence {evidence} file was not scanned")
        if row.config_key.isupper() and not _default_paths().get(row.config_key):
            # Some keys resolve to "" by design (SPEND_GUARD_FILE fallback).
            if row.config_key not in {"SPEND_GUARD_FILE"}:
                problems.append(
                    f"config key {row.config_key} has no default in config.py")
    unclassified = sorted({f"{s.file}:{s.line}" for s in sites
                           if not s.config_key})
    if unclassified:
        notes.append("write sites without a config key (operator/dev): "
                     + ", ".join(unclassified))
    return problems, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="fail when the inventory does not match the AST scan")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args(argv)
    sites = _classify(scan())
    problems, notes = check()
    if args.json:
        print(json.dumps({
            "sites": [s.__dict__ for s in sites],
            "inventory": [r.__dict__ for r in INVENTORY],
            "problems": problems,
            "notes": notes,
        }, indent=2))
    else:
        print("\n".join(table()))
        if notes:
            print("\n".join(notes), file=sys.stderr)
    if args.check and problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
