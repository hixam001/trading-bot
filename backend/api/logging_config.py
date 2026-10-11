"""
backend/api/logging_config.py — Centralized logging infrastructure with rotation and secret redaction.

Guarantees:
1. Designated Log Directory:
   - Configured via config.LOG_DIR (default: <project_root>/logs).
   - Created with restricted permissions (0o750).
2. Dedicated Log Channels:
   - logs/app.log: General application records (INFO and above).
   - logs/error.log: Dedicated error log (ERROR and CRITICAL only).
   - logs/security.log: Dedicated security audit log for suspicious activity,
     bot triggers, auth failures, and rate limit violations.
3. Rotating File Storage:
   - RotatingFileHandler with 10MB per file and 5 rotated archives (app.log.1, etc.).
4. Strict Secret Scrubbing:
   - SecretRedactorFilter attached to every handler and logger.
   - Zero passwords, tokens, API keys, Bearer headers, basic auth URLs,
     or Solana wallet keypairs are written to disk or stdout.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
from typing import Any

import config
from api.error_handler import SecretRedactorFilter

_LOGGING_INITIALIZED = False


def get_log_paths() -> dict[str, Path]:
    """Return dictionary of configured log directory and file paths."""
    log_dir = Path(getattr(config, "LOG_DIR", Path(__file__).parent.parent.parent / "logs"))
    return {
        "log_dir": log_dir,
        "app_log": log_dir / getattr(config, "LOG_FILE", "app.log"),
        "error_log": log_dir / getattr(config, "ERROR_LOG_FILE", "error.log"),
        "security_log": log_dir / getattr(config, "SECURITY_LOG_FILE", "security.log"),
    }


def setup_logging(force: bool = False) -> dict[str, Path]:
    """Configure centralized rotating loggers with automatic secret redactors."""
    global _LOGGING_INITIALIZED
    paths = get_log_paths()
    log_dir = paths["log_dir"]

    # 1. Ensure log directory exists with restricted permissions (0o750)
    log_dir.mkdir(mode=0o750, parents=True, exist_ok=True)

    max_bytes = getattr(config, "LOG_MAX_BYTES", 10 * 1024 * 1024)
    backup_count = getattr(config, "LOG_BACKUP_COUNT", 5)
    log_level_str = getattr(config, "LOG_LEVEL", "INFO").strip().upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    standard_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    security_formatter = logging.Formatter(
        "%(asctime)s [SECURITY] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    redactor = SecretRedactorFilter()

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    sec_logger = logging.getLogger("security")
    sec_logger.setLevel(logging.INFO)
    sec_logger.propagate = True

    app_target = str(paths["app_log"].resolve())
    error_target = str(paths["error_log"].resolve())
    sec_target = str(paths["security_log"].resolve())

    # Close and remove existing file handlers if paths changed or force requested
    for h in list(root_logger.handlers):
        if isinstance(h, RotatingFileHandler):
            base_fn = getattr(h, "baseFilename", None)
            if force or (getattr(h, "_is_app_log", False) and base_fn != app_target):
                h.close()
                root_logger.removeHandler(h)
            elif force or (getattr(h, "_is_error_log", False) and base_fn != error_target):
                h.close()
                root_logger.removeHandler(h)

    for h in list(sec_logger.handlers):
        if isinstance(h, RotatingFileHandler):
            base_fn = getattr(h, "baseFilename", None)
            if force or (getattr(h, "_is_sec_log", False) and base_fn != sec_target):
                h.close()
                sec_logger.removeHandler(h)

    # 2. General application log handler (logs/app.log)
    if not any(
        isinstance(h, RotatingFileHandler) and getattr(h, "_is_app_log", False)
        for h in root_logger.handlers
    ):
        try:
            app_handler = RotatingFileHandler(
                paths["app_log"],
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            app_handler._is_app_log = True  # type: ignore[attr-defined]
            app_handler.setLevel(log_level)
            app_handler.setFormatter(standard_formatter)
            app_handler.addFilter(redactor)
            root_logger.addHandler(app_handler)
        except OSError as exc:
            sys.stderr.write(f"Warning: could not open app log {paths['app_log']}: {exc}\n")

    # 3. Dedicated error log handler (logs/error.log)
    if not any(
        isinstance(h, RotatingFileHandler) and getattr(h, "_is_error_log", False)
        for h in root_logger.handlers
    ):
        try:
            error_handler = RotatingFileHandler(
                paths["error_log"],
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            error_handler._is_error_log = True  # type: ignore[attr-defined]
            error_handler.setLevel(logging.ERROR)
            error_handler.setFormatter(standard_formatter)
            error_handler.addFilter(redactor)
            root_logger.addHandler(error_handler)
        except OSError as exc:
            sys.stderr.write(f"Warning: could not open error log {paths['error_log']}: {exc}\n")

    # 4. Console stream handler (stdout)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(standard_formatter)
        console_handler.addFilter(redactor)
        root_logger.addHandler(console_handler)

    # 5. Dedicated security audit logger (logs/security.log)
    sec_logger = logging.getLogger("security")
    sec_logger.setLevel(logging.INFO)
    sec_logger.propagate = True  # Also bubble up to app.log and stdout

    if not any(isinstance(h, RotatingFileHandler) and getattr(h, "_is_sec_log", False) for h in sec_logger.handlers):
        try:
            sec_handler = RotatingFileHandler(
                paths["security_log"],
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            sec_handler._is_sec_log = True  # type: ignore[attr-defined]
            sec_handler.setLevel(logging.INFO)
            sec_handler.setFormatter(security_formatter)
            sec_handler.addFilter(redactor)
            sec_logger.addHandler(sec_handler)
        except OSError as exc:
            sys.stderr.write(f"Warning: could not open security log {paths['security_log']}: {exc}\n")

    # 6. Apply SecretRedactorFilter across all subsystem loggers
    target_loggers = ("", "uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", "httpx", "api", "security")
    for name in target_loggers:
        lg = logging.getLogger(name)
        if not any(isinstance(f, SecretRedactorFilter) for f in lg.filters):
            lg.addFilter(redactor)

    _LOGGING_INITIALIZED = True
    return paths
