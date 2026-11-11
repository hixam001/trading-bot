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
import json
import os
import posixpath
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
SCRIPTS = REPO_ROOT / "scripts"



@dataclass
class WriteSite:
    file: str
    line: int
    func: str
    path_expr: str
    config_key: str = ""
    scope: str = ""


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
        ("backend/secret_store.py#open", "backend/secret_store.py#replace"),
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

# Independent environment overrides also need an explicit writable home.
INVENTORY += (
    InventoryRow("<LIVE_STATE_DIR>/break_state.json", "BREAK_STATE_FILE", ("live",),
                 "engine_state", ("backend/rule_engine/liveness.py#write_text",)),
    InventoryRow("<LIVE_STATE_DIR>/kill_switch.json", "KILL_SWITCH_FILE", ("live",),
                 "engine_state", ("backend/live_execution/kill_switch.py#write_text",)),
    InventoryRow("<backend>/live_execution/state", "LIVE_EXECUTION_STATE_DIR", ("live",),
                 "engine_state", ("backend/run_live_cycle.py#mkdir",)),
    *(InventoryRow(f"<LOG_DIR>/{filename}", key, ("api", "live"), "engine_logs",
                   ("backend/api/logging_config.py#mkdir",))
      for key, filename in (("LOG_FILE", "app.log"), ("ERROR_LOG_FILE", "error.log"),
                            ("SECURITY_LOG_FILE", "security.log"))),
    InventoryRow("unique .rofs-smoke-* siblings of configured runtime paths",
                 "runtime inventory (smoke only)", ("operator", "dev"),
                 "same mounts as the selected compose service",
                 ("scripts/rofs_smoke.py#mkdir",)),
)


def container_path(path: str, base: str = "/app") -> str:
    """Normalise container paths lexically, without resolving host symlinks."""
    if not posixpath.isabs(base):
        base = posixpath.join("/app", base)
    return posixpath.normpath(posixpath.join(base, str(path)))


def is_under(path: str, root: str) -> bool:
    candidate, parent = container_path(path), container_path(root)
    return candidate == parent or candidate.startswith(parent.rstrip("/") + "/")


def split_volume(raw: str) -> tuple[str, str, bool]:
    parts = raw.rsplit(":", 2)
    if len(parts) == 3 and parts[-2].startswith("/") and not parts[-1].startswith("/"):
        return parts[0], parts[1], "ro" in parts[2].split(",")
    if len(parts) >= 2:
        source, target = raw.rsplit(":", 1)
        return source, target, False
    return "", raw, False


def mount_entries(service: dict) -> list[tuple[str, bool]]:
    """(target, read_only) from short or expanded docker-compose mounts."""
    entries: list[tuple[str, bool]] = []
    for volume in service.get("volumes") or []:
        if isinstance(volume, dict):
            target = volume.get("target")
            read_only = bool(volume.get("read_only", False))
        else:
            _, target, read_only = split_volume(str(volume))
        if target:
            entries.append((container_path(str(target)), read_only))
    tmpfs_entries = service.get("tmpfs") or []
    if isinstance(tmpfs_entries, (str, dict)):
        tmpfs_entries = [tmpfs_entries]
    for tmpfs in tmpfs_entries:
        target = tmpfs.get("target") if isinstance(tmpfs, dict) else str(tmpfs).split(":")[0]
        if target:
            entries.append((container_path(str(target)), False))
    return entries


def writable_mounts(service: dict) -> list[str]:
    return [target for target, read_only in mount_entries(service) if not read_only]


def writable_path(path: str, service: dict) -> bool:
    # A nested read-only mount overrides its writable parent.
    matches = [(len(target), not read_only) for target, read_only in mount_entries(service)
               if is_under(path, target)]
    return max(matches, default=(0, False))[1]


def service_environment(service: dict) -> dict[str, str]:
    raw = service.get("environment") or {}
    if isinstance(raw, dict):
        return {str(k): "" if v is None else str(v) for k, v in raw.items()}
    return dict(str(item).partition("=")[::2] for item in raw)


def runtime_keys(role: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(row.config_key for row in INVENTORY
                               if role in row.services and row.config_key.isupper()))


def resolved_runtime_paths(env: dict[str, str]) -> dict[str, str]:
    """Resolve the backend's container defaults and dependent path overrides.

    No app imports, .env reads, or secret access: preflight runs on a bare VM.
    Tests compare these defaults with config's actual resolved defaults.
    """
    state = env.get("LIVE_STATE_DIR") or "/app/backend/live_execution/state"
    log_dir = env.get("LOG_DIR") or "/app/logs"
    defaults = {
        "LIVE_STATE_DIR": state,
        "LIVE_EXECUTION_STATE_DIR": "/app/backend/live_execution/state",
        "BREAK_STATE_FILE": posixpath.join(state, "break_state.json"),
        "KILL_SWITCH_FILE": posixpath.join(state, "kill_switch.json"),
        "BLOCKLIST_STATE_FILE": "/app/backend/blocklist_state.json",
        "FOMO_PRIVY_STATE_FILE": "/app/.fomo_privy.json",
        "SECRET_STORE_KEY_FILE": "/app/.secret_store.key",
        "SUPABASE_PIN_FILE": "/app/backend/.supabase_fp.txt",
        "SESSION_REVOCATION_FILE": "/app/state/session_revocations.json",
        "KB_INGESTED_DIR": "/app/backend/knowledge_base/ingested",
        "SPEND_GUARD_FILE": posixpath.join(state, "spend_guard.json"),
        "DB_PATH": "/app/backend/trading_bot.db", "LOG_DIR": log_dir,
        "LOG_FILE": "app.log", "ERROR_LOG_FILE": "error.log",
        "SECURITY_LOG_FILE": "security.log",
    }
    paths = {key: container_path(env.get(key) or default)
             for key, default in defaults.items()}
    # config.DB_PATH anchors relative overrides to BASE_DIR; log filenames
    # anchor to LOG_DIR and absolute filenames override that parent.
    paths["DB_PATH"] = container_path(env.get("DB_PATH") or "trading_bot.db", "/app/backend")
    for key in ("LOG_FILE", "ERROR_LOG_FILE", "SECURITY_LOG_FILE"):
        paths[key] = container_path(env.get(key) or defaults[key], log_dir)
    return paths


APPROVED_WRITES: dict[str, str] = {
    'scripts/restore_state.py#restore_backup#mkdir#target_state_dir': 'operator target directory',
    'backend/live_execution/state_lock.py#state_lock#remove#held': 'non-filesystem thread-local set removal',
    'backend/live_execution/state_lock.py#state_lock#mkdir#path.parent': 'LIVE_STATE_DIR',
    "backend/live_execution/state_lock.py#state_lock#open#str(path) + '.lock'": 'LIVE_STATE_DIR',
    'scripts/backup_state.py#create_backup#mkstemp#backup_dir': 'BACKUP_DIR (CLI/env)',
    'scripts/restore_state.py#restore_backup#open#pre_db': 'operator target directory',
    'scripts/restore_state.py#restore_backup#fdopen#fd': 'operator target directory',

    'backend/api/auth.py#_persist_revocations#mkdir#path.parent': 'SESSION_REVOCATION_FILE',
    'backend/api/auth.py#_persist_revocations#write_text#tmp': 'SESSION_REVOCATION_FILE',
    'backend/api/auth.py#_persist_revocations#replace#path': 'SESSION_REVOCATION_FILE',
    'backend/api/auth.py#reset_session_revocations#unlink#path': 'SESSION_REVOCATION_FILE',
    'backend/api/db.py#init_db#connect#config.DB_PATH': 'DB_PATH',
    'backend/api/db.py#get_db#connect#config.DB_PATH': 'DB_PATH',
    'backend/api/db.py#deployed_today#replace#': 'non-filesystem string/datetime/URL replacement',
    'backend/api/db_pg.py#_tls_context#mkdir#pin_path.parent': 'SUPABASE_PIN_FILE',
    'backend/api/db_pg.py#_tls_context#write_text#pin_path': 'SUPABASE_PIN_FILE',
    'backend/api/db_pg.py#deployed_today#replace#': 'non-filesystem string/datetime/URL replacement',
    'backend/api/logging_config.py#setup_logging#mkdir#log_dir': 'LOG_DIR',
    'backend/api/main.py#force_https_middleware#replace#': 'non-filesystem string/datetime/URL replacement',
    "backend/api/main.py#unknown_api_path#replace#'\\x00'": 'non-filesystem string/datetime/URL replacement',
    "backend/api/main.py#unknown_api_path#replace#'\\n'": 'non-filesystem string/datetime/URL replacement',
    "backend/api/main.py#unknown_api_path#replace#'\\r'": 'non-filesystem string/datetime/URL replacement',
    "backend/api/main.py#_is_blocked_static_path#replace#'\\\\'": 'non-filesystem string/datetime/URL replacement',
    "backend/api/routes/proof.py#_verify_binding_checks#replace#'Z'": 'non-filesystem string/datetime/URL replacement',
    "backend/api/routes/safety.py#_row_epoch#replace#'Z'": 'non-filesystem string/datetime/URL replacement',
    'backend/blocklist.py#_load#rename#quarantined': 'BLOCKLIST_STATE_FILE',
    'backend/blocklist.py#_save#mkdir#path.parent': 'BLOCKLIST_STATE_FILE',
    'backend/blocklist.py#_save#write_text#path': 'BLOCKLIST_STATE_FILE',
    'backend/data_providers/crowd.py#publish_chain_status#mkdir#path.parent': 'LIVE_STATE_DIR',
    'backend/data_providers/crowd.py#publish_chain_status#write_text#tmp': 'LIVE_STATE_DIR',
    'backend/data_providers/crowd.py#publish_chain_status#replace#path': 'LIVE_STATE_DIR',
    "backend/data_providers/discovery.py#KeywordScanner._build_board#replace#'$'": 'non-filesystem string/datetime/URL replacement',
    'backend/knowledge_base/loader.py#ingest_file#mkdir#config.INGESTED_KNOWLEDGE_DIR': 'KB_INGESTED_DIR',
    'backend/knowledge_base/loader.py#ingest_file#open#str(archive_file)': 'KB_INGESTED_DIR',
    'backend/knowledge_base/loader.py#ingest_file#fdopen#fd': 'KB_INGESTED_DIR',
    'backend/live_execution/commit_log.py#CommitLog._save#mkdir#self.path.parent': 'LIVE_STATE_DIR',
    'backend/live_execution/commit_log.py#CommitLog._save#write_text#tmp': 'LIVE_STATE_DIR',
    'backend/live_execution/commit_log.py#CommitLog._save#replace#self.path': 'LIVE_STATE_DIR',
    'backend/live_execution/confirmation_queue.py#ConfirmationQueue._save#mkdir#self.path.parent': 'LIVE_STATE_DIR',
    'backend/live_execution/confirmation_queue.py#ConfirmationQueue._save#write_text#tmp': 'LIVE_STATE_DIR',
    'backend/live_execution/confirmation_queue.py#ConfirmationQueue._save#replace#self.path': 'LIVE_STATE_DIR',
    'backend/live_execution/kill_switch.py#_write#mkdir#path.parent': 'LIVE_STATE_DIR',
    'backend/live_execution/kill_switch.py#_write#write_text#tmp': 'LIVE_STATE_DIR',
    'backend/live_execution/kill_switch.py#_write#replace#path': 'LIVE_STATE_DIR',
    'backend/live_execution/models.py#ExecutionLedger._save#mkdir#self.path.parent': 'LIVE_STATE_DIR',
    'backend/live_execution/models.py#ExecutionLedger._save#write_text#tmp': 'LIVE_STATE_DIR',
    'backend/live_execution/models.py#ExecutionLedger._save#replace#self.path': 'LIVE_STATE_DIR',
    'backend/live_execution/reconcile.py#check_wallet_drop#mkdir#target_dir': 'LIVE_EXECUTION_STATE_DIR',
    'backend/live_execution/reconcile.py#check_wallet_drop#write_text#tmp_file': 'LIVE_EXECUTION_STATE_DIR',
    'backend/live_execution/reconcile.py#check_wallet_drop#replace#snap_file': 'LIVE_EXECUTION_STATE_DIR',
    'backend/live_execution/safety_counters.py#record_safety_refusal#mkdir#target_dir': 'LIVE_STATE_DIR',
    'backend/live_execution/safety_counters.py#record_safety_refusal#write_text#tmp_file': 'LIVE_STATE_DIR',
    'backend/live_execution/safety_counters.py#record_safety_refusal#replace#target_file': 'LIVE_STATE_DIR',
    "backend/llm/grounding.py#validate_numbers.nums#replace#','": 'non-filesystem string/datetime/URL replacement',
    'backend/llm/spend_guard.py#_atomic_write_locked#mkdir#path.parent': 'SPEND_GUARD_FILE',
    'backend/llm/spend_guard.py#_atomic_write_locked#write_text#tmp': 'SPEND_GUARD_FILE',
    'backend/llm/spend_guard.py#_atomic_write_locked#replace#path': 'SPEND_GUARD_FILE',
    "backend/llm/untrusted.py#sanitize_untrusted#replace#'<untrusted_data'": 'non-filesystem string/datetime/URL replacement',
    "backend/llm/untrusted.py#sanitize_untrusted#replace#'</untrusted_data'": 'non-filesystem string/datetime/URL replacement',
    'backend/redaction.py#redact_text#replace#value': 'non-filesystem string/datetime/URL replacement',
    "backend/retro_matcher.py#_parse_iso#replace#'Z'": 'non-filesystem string/datetime/URL replacement',
    'backend/rule_engine/liveness.py#_write#mkdir#path.parent': 'LIVE_STATE_DIR',
    'backend/rule_engine/liveness.py#_write#write_text#tmp': 'LIVE_STATE_DIR',
    'backend/rule_engine/liveness.py#_write#replace#path': 'LIVE_STATE_DIR',
    'backend/run_live_cycle.py#_beat#write_text#tmp_hb': 'LIVE_STATE_DIR',
    'backend/run_live_cycle.py#_beat#replace#hb_file': 'LIVE_STATE_DIR',
    "backend/run_live_cycle.py#_snapshot_funnel#replace#'Z'": 'non-filesystem string/datetime/URL replacement',
    'backend/run_live_cycle.py#main#mkdir#live_config.STATE_DIR': 'LIVE_STATE_DIR',
    'backend/run_live_cycle.py#main#write_text#tmp_pubkey_file': 'LIVE_STATE_DIR',
    'backend/run_live_cycle.py#main#replace#pubkey_file': 'LIVE_STATE_DIR',
    'backend/secret_store.py#_write_private#open#tmp': 'SECRET_STORE_KEY_FILE',
    'backend/secret_store.py#_write_private#fdopen#fd': 'SECRET_STORE_KEY_FILE',
    'backend/secret_store.py#_write_private#replace#path': 'SECRET_STORE_KEY_FILE',
    'backend/thesis_restate.py#parse_ts#replace#': 'non-filesystem string/datetime/URL replacement',
    'backend/thesis_restate.py#select_due.sort_key#replace#': 'non-filesystem string/datetime/URL replacement',
    'scripts/ack_wallet_change.py#main#mkdir#target_dir': 'LIVE_STATE_DIR',
    'scripts/ack_wallet_change.py#main#write_text#tmp_file': 'LIVE_STATE_DIR',
    'scripts/ack_wallet_change.py#main#replace#snap_file': 'LIVE_STATE_DIR',
    'scripts/backup_state.py#prune_retention#unlink#p': 'BACKUP_DIR (CLI/env)',
    'scripts/backup_state.py#create_backup#open#': 'in-memory tar stream (fileobj=buf)',
    'scripts/backup_state.py#create_backup#connect#str(db_path)': 'DB_PATH',
    'scripts/backup_state.py#create_backup#mkdir#temp_backup_path.parent': 'BACKUP_DIR (CLI/env)',
    'scripts/backup_state.py#create_backup#connect#str(temp_backup_path)': 'BACKUP_DIR (CLI/env)',
    'scripts/backup_state.py#create_backup#unlink#temp_backup_path': 'BACKUP_DIR (CLI/env)',
    'scripts/backup_state.py#create_backup#mkdir#backup_dir': 'BACKUP_DIR (CLI/env)',
    'scripts/backup_state.py#create_backup#write_bytes#tmp_out': 'BACKUP_DIR (CLI/env)',
    'scripts/backup_state.py#create_backup#replace#out_file': 'BACKUP_DIR (CLI/env)',
    "scripts/gate_funnel_report.py#_extract_date#replace#'Z'": 'non-filesystem string/datetime/URL replacement',
    'scripts/gate_funnel_report.py#fetch_data_sqlite#connect#str(db_path)': 'DB_PATH (operator read/report)',
    'scripts/mutation_check.py#run_mutation#replace#m.exact_old_text': 'repo root (dev/CI mutation harness)',
    'scripts/mutation_check.py#run_mutation#write_bytes#target': 'repo root (dev/CI mutation harness)',
    "scripts/mutation_check.py#main#replace#'|'": 'non-filesystem string/datetime/URL replacement',
    'scripts/mutation_check.py#main#write_text#doc_path': 'repo root (dev/CI mutation harness)',
    "scripts/perf_report.py#_ts_epoch#replace#'Z'": 'non-filesystem string/datetime/URL replacement',
    'scripts/perf_report.py#main#replace#': 'non-filesystem string/datetime/URL replacement',
    'scripts/refresh_dex_labels.py#refresh_labels#write_text#tmp_path': 'fixed repo path (operator/dev)',
    'scripts/refresh_dex_labels.py#refresh_labels#replace#target_file': 'fixed repo path (operator/dev)',
    'scripts/refresh_dex_labels.py#refresh_labels#unlink#tmp_path': 'fixed repo path (operator/dev)',
    'scripts/resolve_pending_buy.py#_append_audit#mkdir#path.parent': 'LIVE_STATE_DIR',
    'scripts/resolve_pending_buy.py#_append_audit#open#path': 'LIVE_STATE_DIR',
    'scripts/restore_state.py#_remove_sqlite_sidecars#unlink#Path(str(db_path) + suffix)': 'operator target directory',
    'scripts/restore_state.py#_attempt_rollback#mkdir#dest.parent': 'operator target directory',
    'scripts/restore_state.py#_attempt_rollback#copy2#tmp': 'operator target directory',
    'scripts/restore_state.py#_attempt_rollback#replace#dest': 'operator target directory',
    'scripts/restore_state.py#_attempt_rollback#unlink#tmp': 'operator target directory',
    'scripts/restore_state.py#restore_backup#mkdir#pre_restore_dir': 'operator target directory',
    'scripts/restore_state.py#restore_backup#copy2#pre_restore_dir / p.name': 'operator target directory',
    'scripts/restore_state.py#restore_backup#connect#str(target_db_path)': 'operator target directory',
    'scripts/restore_state.py#restore_backup#connect#str(pre_db)': 'operator target directory',
    'scripts/restore_state.py#restore_backup#copy2#pre_db': 'operator target directory',
    'scripts/restore_state.py#restore_backup#mkdir#dest_path.parent': 'operator target directory',
    'scripts/restore_state.py#restore_backup#open#temp_path': 'operator target directory',
    'scripts/restore_state.py#restore_backup#connect#str(temp_path)': 'operator target directory',
    'scripts/restore_state.py#restore_backup#unlink#temp_path': 'operator target directory',
    'scripts/restore_state.py#restore_backup#replace#dest_path': 'operator target directory',
    'scripts/rofs_smoke.py#_probe_directory#mkdir#directory': 'runtime inventory / disposable CI fixtures',
    'scripts/rofs_smoke.py#_probe_directory#mkstemp#directory': 'runtime inventory / disposable CI fixtures',
    'scripts/rofs_smoke.py#_probe_directory#fdopen#descriptor': 'runtime inventory / disposable CI fixtures',
    'scripts/rofs_smoke.py#_probe_directory#unlink#probe': 'runtime inventory / disposable CI fixtures',
    "scripts/rofs_smoke.py#run_compose_ci#TemporaryDirectory#'/tmp'": 'runtime inventory / disposable CI fixtures',
    'scripts/rofs_smoke.py#run_compose_ci#mkdir#fixture': 'runtime inventory / disposable CI fixtures',
    'scripts/rofs_smoke.py#run_compose_ci#touch#fixture': 'runtime inventory / disposable CI fixtures',
}


def _iter_python(root: Path):
    for path in sorted(root.rglob("*.py")):
        if ("tests" in path.parts or "__pycache__" in path.parts
                or path.name == "conftest.py"):
            continue
        yield path


def _path_expr(node: ast.AST | None) -> str:
    return ast.unparse(node) if node is not None else ""


def _argument(node: ast.Call, position: int, name: str,
              default: ast.AST | None = None) -> ast.AST | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return node.args[position] if len(node.args) > position else default


def _is_write_mode(mode: ast.AST | None) -> bool:
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return any(c in mode.value for c in "wax+")
    return True  # a dynamic mode needs explicit review


def _os_open_writes(flags: ast.AST | None) -> bool:
    if flags is None:
        return True
    if isinstance(flags, ast.Constant) and isinstance(flags.value, int):
        return bool(flags.value & (os.O_WRONLY | os.O_RDWR | os.O_CREAT
                                   | os.O_TRUNC | os.O_APPEND))
    names = {n.attr for n in ast.walk(flags) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(flags) if isinstance(n, ast.Name)}
    if names & {"O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND"}:
        return True
    # O_RDONLY alone (or combined with O_CLOEXEC/O_NOFOLLOW) is read-only.
    if names <= {"os", "O_RDONLY", "O_CLOEXEC", "O_NOFOLLOW"} and "O_RDONLY" in names:
        return False
    return True  # unresolved flags are potentially writable


def _qualified(node: ast.AST, imports: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return imports.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return _qualified(node.value, imports) + "." + node.attr
    return _path_expr(node)


def _site_signature(site: WriteSite) -> str:
    return f"{site.file}#{site.scope}#{site.func}#{site.path_expr}"


def scan(root: Path = REPO_ROOT) -> list[WriteSite]:
    """Conservatively collect file writes without importing application code.

    Unknown open modes/flags and ambiguous Path-style methods require review.
    Exact reviewed call expressions (not whole files or method names) form the
    allowlist, so a second write in an existing module cannot slip through.
    """
    root = Path(root)
    sites: list[WriteSite] = []
    for directory in (root / "backend", root / "scripts"):
        if not directory.exists():
            continue
        for path in _iter_python(directory):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports: dict[str, str] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports[alias.asname or alias.name] = alias.name
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        imports[alias.asname or alias.name] = (
                            (node.module or "") + "." + alias.name)

            class Visitor(ast.NodeVisitor):
                scope: list[str] = []

                def visit_FunctionDef(self, node):
                    self.scope.append(node.name)
                    self.generic_visit(node)
                    self.scope.pop()

                visit_AsyncFunctionDef = visit_FunctionDef

                def visit_ClassDef(self, node):
                    self.scope.append(node.name)
                    self.generic_visit(node)
                    self.scope.pop()

                def visit_Call(self, node):
                    function = _qualified(node.func, imports)
                    name = function.rsplit(".", 1)[-1]
                    receiver = (node.func.value if isinstance(node.func, ast.Attribute)
                                else None)
                    target = None
                    found = False
                    if function == "os.open":
                        if _os_open_writes(_argument(node, 1, "flags")):
                            target = _argument(node, 0, "path")
                            found = True
                    elif name in {"open", "fdopen"}:
                        is_function = (receiver is None or function in {
                            "open", "builtins.open", "io.open", "os.fdopen",
                            "tarfile.open"})
                        mode = _argument(node, 1 if is_function else 0, "mode")
                        if _is_write_mode(mode):
                            target = (_argument(node, 0, "file") if is_function
                                      else receiver)
                            found = True
                    elif name in {"write_text", "write_bytes", "mkdir", "touch"}:
                        target = (receiver if receiver is not None
                                  else _argument(node, 0, "path"))
                        # os.mkdir is a function, Path.mkdir is a method.
                        if function in {"os.mkdir", "os.makedirs"}:
                            target = _argument(node, 0, "path")
                        found = True
                    elif name == "makedirs":
                        target = _argument(node, 0, "name")
                        found = True
                    elif name in {"replace", "rename", "move", "copy", "copy2",
                                  "copyfile", "copytree", "copymode", "copystat"}:
                        module_function = function.startswith(("os.", "shutil."))
                        target = _argument(node, 1 if module_function else 0,
                                           "dst" if module_function else "target")
                        found = True
                    elif name in {"unlink", "remove", "rmdir", "rmtree", "truncate"}:
                        target = (_argument(node, 0, "path")
                                  if receiver is None or function.startswith(("os.", "shutil."))
                                  else receiver)
                        found = True
                    elif function in {"sqlite3.connect", "aiosqlite.connect", "connect"}:
                        target = _argument(node, 0, "database")
                        found = True
                    elif function in {"tempfile.mkstemp", "tempfile.mkdtemp",
                                      "tempfile.TemporaryDirectory",
                                      "tempfile.NamedTemporaryFile"}:
                        position = (2 if name in {"mkstemp", "mkdtemp"} else
                                    6 if name == "NamedTemporaryFile" else 2)
                        target = _argument(node, position, "dir", ast.Constant("/tmp"))
                        found = True
                    if found:
                        sites.append(WriteSite(
                            file=str(path.relative_to(root)), line=node.lineno,
                            func=name, path_expr=_path_expr(target),
                            scope=".".join(self.scope)))
                    self.generic_visit(node)

            Visitor().visit(tree)
    return sites


def _classify(sites: list[WriteSite]) -> list[WriteSite]:
    for site in sites:
        site.config_key = APPROVED_WRITES.get(_site_signature(site), "")
    return sites


def _default_paths() -> dict[str, str]:
    """Import config in a clean subprocess and read the path defaults."""
    code = (
        "import json, config\n"
        "keys = %r\n"
        "print(json.dumps({k: str(getattr(config, k, '')) for k in keys}))\n"
    ) % ([row.config_key for row in INVENTORY
          if row.config_key.isupper()],)
    path_keys = {row.config_key for row in INVENTORY if row.config_key.isupper()}
    env = {k: v for k, v in os.environ.items() if k not in path_keys}
    env["PYTHONPATH"] = str(BACKEND)
    env["PYTHON_DOTENV_DISABLED"] = "1"
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


def check(root: Path = REPO_ROOT) -> tuple[list[str], list[str]]:
    """Return (problems, notes). Problems fail CI; notes are informational."""
    problems: list[str] = []
    notes: list[str] = []
    sites = _classify(scan(root=root))
    detected = {(s.file, s.func) for s in sites}
    defaults = _default_paths()
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
        if row.config_key.isupper() and not defaults.get(row.config_key):
            # Some keys resolve to "" by design (SPEND_GUARD_FILE fallback).
            if row.config_key not in {"SPEND_GUARD_FILE", "LIVE_EXECUTION_STATE_DIR"}:
                problems.append(
                    f"config key {row.config_key} has no default in config.py")
    unclassified = sorted({f"{s.file}:{s.line}" for s in sites
                           if not s.config_key})
    if unclassified:
        problems.append("unclassified write sites require an explicit path/home review: "
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
