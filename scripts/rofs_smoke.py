#!/usr/bin/env python3
"""§93 R1 — real-container read-only-root smoke test.

Runs INSIDE the image (``ROLE=live`` or ``ROLE=api``) and exercises every
runtime write path for that role.  The container is started with
``--read-only --tmpfs /tmp`` and the same volumes/env as compose, so a write
that lands on the read-only root fails loudly here instead of being swallowed
by the application.

Prints one line per check, exits non-zero on the first role failure.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROLE = os.getenv("ROLE", "").strip()
if ROLE not in ("api", "live"):
    print(f"FAIL role ROLE={ROLE!r} (expected api or live)")
    sys.exit(2)

failures: list[str] = []


def _write_read_remove(path: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("rofs-smoke\n", encoding="utf-8")
    if not target.read_text(encoding="utf-8"):
        raise OSError("empty write")
    target.unlink()


def _write_read_remove_in_dir(directory: str, name: str) -> None:
    target = Path(directory) / name
    target.write_text("rofs-smoke\n", encoding="utf-8")
    if not target.read_text(encoding="utf-8"):
        raise OSError("empty write")
    target.unlink()


def _sibling_write(directory: str, name: str) -> None:
    """Prove the directory holding a pre-existing file is writable."""
    target = Path(directory) / name
    target.write_text("rofs-smoke\n", encoding="utf-8")
    target.unlink()


def check(name: str, fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - report every failure
        failures.append(f"{name}: {type(exc).__name__}: {exc}")
        print(f"FAIL {ROLE} {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"PASS {ROLE} {name}")


def main() -> int:
    state_dir = os.getenv("LIVE_STATE_DIR") or "/app/backend/live_execution/state"
    data_dir = Path(os.getenv("DB_PATH") or "/app/data/trading_bot.db").parent
    log_dir = os.getenv("LOG_DIR") or "/app/logs"

    if ROLE == "live":
        check("LIVE_STATE_DIR", lambda: _write_read_remove_in_dir(
            state_dir, "rofs_smoke.txt"))
        for key in ("BLOCKLIST_STATE_FILE", "FOMO_PRIVY_STATE_FILE",
                    "SECRET_STORE_KEY_FILE", "SUPABASE_PIN_FILE",
                    "SPEND_GUARD_FILE"):
            path = os.getenv(key, "")
            if not path:
                failures.append(f"{key} is unset")
                print(f"FAIL live {key}: unset")
                continue
            check(key, lambda path=path: _write_read_remove(path))
        check("DB_PATH directory", lambda: _sibling_write(
            str(data_dir), "rofs_smoke.tmp"))
        check("LOG_DIR", lambda: _write_read_remove_in_dir(
            log_dir, "rofs_smoke.log"))
    else:
        check("SESSION_REVOCATION_FILE", lambda: _write_read_remove(
            os.getenv("SESSION_REVOCATION_FILE",
                      "/app/data/session_revocations.json")))
        check("KB_INGESTED_DIR", lambda: _write_read_remove_in_dir(
            os.getenv("KB_INGESTED_DIR",
                      "/app/data/knowledge_base/ingested"),
            "rofs_smoke.md"))
        check("SPEND_GUARD_FILE", lambda: _write_read_remove(
            os.getenv("SPEND_GUARD_FILE", "/app/data/spend_guard_api.json")))
        check("SUPABASE_PIN_FILE", lambda: _write_read_remove(
            os.getenv("SUPABASE_PIN_FILE", "/app/data/.supabase_fp.txt")))
        check("DB_PATH directory", lambda: _sibling_write(
            str(data_dir), "rofs_smoke.tmp"))
        check("LOG_DIR", lambda: _write_read_remove_in_dir(
            log_dir, "rofs_smoke.log"))

    if failures:
        print(f"{ROLE}: {len(failures)} read-only-root write failure(s)")
        return 1
    print(f"{ROLE}: all runtime write paths are writable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
