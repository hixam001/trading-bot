"""§93 R1 — simulated-EROFS fixture helpers.

The suite runs as root on some hosts, so ``chmod`` cannot simulate a
read-only filesystem.  These helpers install a write guard that raises
``OSError(errno.EROFS)`` for any write outside the writable mounts of one
compose service.  The allow-list is derived by parsing ``docker-compose.yml``
(volumes without ``:ro`` plus ``tmpfs`` entries) — never hand-copied.

The fixture also translates each allowed container path into a throwaway
directory so tests can exercise the real configured keys without touching the
repository or ``/app``.
"""
from __future__ import annotations

import builtins
import errno
import io
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

_WRITE_MODE_CHARS = frozenset("wax+")
_WRITE_FLAGS = (
    os.O_WRONLY, os.O_RDWR, os.O_APPEND, os.O_CREAT, os.O_TRUNC)
_WRITE_FLAGS_MASK = 0
for _flag in _WRITE_FLAGS:
    _WRITE_FLAGS_MASK |= _flag


def load_compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8")) or {}


def writable_mounts(service: dict) -> list[str]:
    """Container paths that are writable for this service (compose-derived)."""
    mounts: list[str] = []
    for volume in service.get("volumes") or []:
        parts = str(volume).split(":")
        if len(parts) >= 2:
            mode = parts[2] if len(parts) > 2 else "rw"
            if mode != "ro":
                mounts.append(parts[1].rstrip("/"))
    for tmpfs in service.get("tmpfs") or []:
        mounts.append(str(tmpfs).rstrip("/"))
    return mounts


def service_environment(service: dict) -> dict[str, str]:
    raw = service.get("environment") or {}
    if isinstance(raw, dict):
        return {str(k): "" if v is None else str(v) for k, v in raw.items()}
    out: dict[str, str] = {}
    for item in raw:
        key, _, value = str(item).partition("=")
        out[key] = value
    return out


def service_by_name(compose: dict, name: str) -> dict:
    return (compose.get("services") or {}).get(name) or {}


def _under(path: Path, root: str) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class ErofsGuard:
    """Install EROFS semantics for one compose service's writable mounts."""

    def __init__(self, role: str, temp_root: Path):
        self.role = role
        self.temp_root = Path(temp_root)
        self.mounts = writable_mounts(service_by_name(load_compose(), role))
        self._originals: dict = {}
        self._installed = False

    # -- path mapping -----------------------------------------------------
    def to_temp(self, container_path: str) -> Path:
        p = Path(container_path)
        if _under(p, str(self.temp_root)):
            return p
        for mount in self.mounts:
            if mount == "/tmp":
                # tmpfs is already writable; do not mirror it into the
                # sandbox (pytest/tmpfile live there too).
                continue
            if _under(p, mount):
                return self.temp_root / p.relative_to("/")
        return p

    def logical(self, path) -> Path:
        p = Path(path)
        if _under(p, str(self.temp_root)):
            return Path("/") / p.relative_to(self.temp_root)
        return p

    def allowed(self, path) -> bool:
        logical = self.logical(path)
        if _under(logical, "/tmp"):
            return True
        return any(_under(logical, mount) for mount in self.mounts)

    def _ensure_parent(self, path: Path) -> None:
        parent = path.parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)

    # -- patched primitives ----------------------------------------------
    def _open(self, file, mode="r", *args, **kwargs):
        if any(c in str(mode) for c in _WRITE_MODE_CHARS):
            if not self.allowed(file):
                raise OSError(errno.EROFS, "Read-only file system (simulated)",
                              str(file))
            file = self.to_temp(file)
            if str(mode) not in ("w", "a", "x", "r+"):
                self._ensure_parent(Path(file))
        return self._originals["open"](file, mode, *args, **kwargs)

    def _os_open(self, path, flags, *args, **kwargs):
        if flags & _WRITE_FLAGS_MASK:
            if not self.allowed(path):
                raise OSError(errno.EROFS, "Read-only file system (simulated)",
                              str(path))
            path = self.to_temp(path)
            self._ensure_parent(Path(path))
        return self._originals["os_open"](path, flags, *args, **kwargs)

    def _write_text(self, path, data, *args, **kwargs):
        if not self.allowed(path):
            raise OSError(errno.EROFS, "Read-only file system (simulated)",
                          str(path))
        target = self.to_temp(path)
        self._ensure_parent(target)
        return self._originals["write_text"](target, data, *args, **kwargs)

    def _write_bytes(self, path, data):
        if not self.allowed(path):
            raise OSError(errno.EROFS, "Read-only file system (simulated)",
                          str(path))
        target = self.to_temp(path)
        self._ensure_parent(target)
        return self._originals["write_bytes"](target, data)

    def _mkdir(self, path, *args, **kwargs):
        if not self.allowed(path):
            raise OSError(errno.EROFS, "Read-only file system (simulated)",
                          str(path))
        target = self.to_temp(path)
        return self._originals["mkdir"](target, *args, **kwargs)

    def _os_replace(self, src, dst, *args, **kwargs):
        if not self.allowed(dst):
            raise OSError(errno.EROFS, "Read-only file system (simulated)",
                          str(dst))
        return self._originals["os_replace"](self.to_temp(src),
                                             self.to_temp(dst), *args, **kwargs)

    def _os_rename(self, src, dst, *args, **kwargs):
        if not self.allowed(dst):
            raise OSError(errno.EROFS, "Read-only file system (simulated)",
                          str(dst))
        return self._originals["os_rename"](self.to_temp(src),
                                            self.to_temp(dst), *args, **kwargs)

    def _path_open(self, path, mode="r", *args, **kwargs):
        if any(c in str(mode) for c in _WRITE_MODE_CHARS):
            if not self.allowed(path):
                raise OSError(errno.EROFS, "Read-only file system (simulated)",
                              str(path))
            target = self.to_temp(path)
            self._ensure_parent(target)
            return self._originals["path_open"](target, mode, *args, **kwargs)
        return self._originals["path_open"](path, mode, *args, **kwargs)

    def _path_replace(self, path, target, *args, **kwargs):
        if not self.allowed(target):
            raise OSError(errno.EROFS, "Read-only file system (simulated)",
                          str(target))
        return self._originals["path_replace"](self.to_temp(path),
                                               self.to_temp(target), *args, **kwargs)

    def _path_unlink(self, path, *args, **kwargs):
        if not self.allowed(path):
            raise OSError(errno.EROFS, "Read-only file system (simulated)",
                          str(path))
        return self._originals["path_unlink"](self.to_temp(path), *args, **kwargs)

    def _path_mkdir(self, path, *args, **kwargs):
        if not self.allowed(path):
            raise OSError(errno.EROFS, "Read-only file system (simulated)",
                          str(path))
        return self._originals["path_mkdir"](self.to_temp(path), *args, **kwargs)

    def _mkstemp(self, *args, **kwargs):
        directory = kwargs.get("dir")
        if directory is None and len(args) >= 4:
            directory = args[3]
        if directory and not self.allowed(directory):
            raise OSError(errno.EROFS, "Read-only file system (simulated)",
                          str(directory))
        if directory:
            kwargs["dir"] = str(self.to_temp(directory))
        return self._originals["mkstemp"](*args, **kwargs)

    def _mkdtemp(self, *args, **kwargs):
        directory = kwargs.get("dir")
        if directory and not self.allowed(directory):
            raise OSError(errno.EROFS, "Read-only file system (simulated)",
                          str(directory))
        if directory:
            kwargs["dir"] = str(self.to_temp(directory))
        return self._originals["mkdtemp"](*args, **kwargs)

    def _shutil_copy(self, src, dst, *args, **kwargs):
        if not self.allowed(dst):
            raise OSError(errno.EROFS, "Read-only file system (simulated)",
                          str(dst))
        return self._originals["shutil_copy"](src, self.to_temp(dst),
                                              *args, **kwargs)

    def _sqlite_connect(self, database, *args, **kwargs):
        if database and not self.allowed(database):
            raise OSError(errno.EROFS, "Read-only file system (simulated)",
                          str(database))
        return self._originals["sqlite_connect"](self.to_temp(database),
                                                 *args, **kwargs)

    def install(self) -> "ErofsGuard":
        if self._installed:
            return self
        o = self._originals
        o["open"] = builtins.open
        o["os_open"] = os.open
        o["os_replace"] = os.replace
        o["os_rename"] = os.rename
        o["write_text"] = Path.write_text
        o["write_bytes"] = Path.write_bytes
        o["path_open"] = Path.open
        o["path_replace"] = Path.replace
        o["path_unlink"] = Path.unlink
        o["path_mkdir"] = Path.mkdir
        o["mkdir"] = os.mkdir
        o["mkstemp"] = tempfile.mkstemp
        o["mkdtemp"] = tempfile.mkdtemp
        o["shutil_copy"] = shutil.copy
        o["sqlite_connect"] = sqlite3.connect

        builtins.open = self._open
        os.open = self._os_open
        os.replace = self._os_replace
        os.rename = self._os_rename
        os.mkdir = self._mkdir
        # Path methods are descriptors: assign plain functions that capture
        # the guard so ``instance.open(...)`` binds the instance as ``path``.
        Path.write_text = lambda p, data, *a, **k: self._write_text(p, data, *a, **k)
        Path.write_bytes = lambda p, data: self._write_bytes(p, data)
        Path.open = lambda p, mode="r", *a, **k: self._path_open(p, mode, *a, **k)
        Path.replace = lambda p, target, *a, **k: self._path_replace(p, target, *a, **k)
        Path.unlink = lambda p, *a, **k: self._path_unlink(p, *a, **k)
        Path.mkdir = lambda p, *a, **k: self._path_mkdir(p, *a, **k)
        tempfile.mkstemp = self._mkstemp
        tempfile.mkdtemp = self._mkdtemp
        shutil.copy = self._shutil_copy
        sqlite3.connect = self._sqlite_connect
        self._installed = True
        return self

    def restore(self) -> None:
        if not self._installed:
            return
        builtins.open = self._originals["open"]
        os.open = self._originals["os_open"]
        os.replace = self._originals["os_replace"]
        os.rename = self._originals["os_rename"]
        os.mkdir = self._originals["mkdir"]
        Path.write_text = self._originals["write_text"]
        Path.write_bytes = self._originals["write_bytes"]
        Path.open = self._originals["path_open"]
        Path.replace = self._originals["path_replace"]
        Path.unlink = self._originals["path_unlink"]
        Path.mkdir = self._originals["path_mkdir"]
        tempfile.mkstemp = self._originals["mkstemp"]
        tempfile.mkdtemp = self._originals["mkdtemp"]
        shutil.copy = self._originals["shutil_copy"]
        sqlite3.connect = self._originals["sqlite_connect"]
        self._installed = False


def apply_service_env(guard: ErofsGuard, monkeypatch, role: str,
                      temp_root: Path) -> dict[str, str]:
    """Apply one service's compose env to config with allowed paths mirrored.

    Only path-valued config keys are translated; the guard rejects writes to
    the read-only root through the simulated EROFS layer.
    """
    import config
    from live_execution import config as live_config

    service = service_by_name(load_compose(), role)
    env = service_environment(service)
    path_keys = (
        "LIVE_STATE_DIR", "BLOCKLIST_STATE_FILE", "FOMO_PRIVY_STATE_FILE",
        "SECRET_STORE_KEY_FILE", "SUPABASE_PIN_FILE", "SESSION_REVOCATION_FILE",
        "KB_INGESTED_DIR", "INGESTED_KNOWLEDGE_DIR", "SPEND_GUARD_FILE",
        "DB_PATH", "LOG_DIR",
    )
    applied: dict[str, str] = {}
    for key in path_keys:
        if key not in env:
            continue
        value = env[key]
        translated = str(guard.to_temp(value)) if value.startswith("/") else value
        if key == "INGESTED_KNOWLEDGE_DIR":
            monkeypatch.setattr(config, key, Path(translated))
        else:
            monkeypatch.setattr(config, key, translated)
        if key == "LIVE_STATE_DIR":
            monkeypatch.setattr(live_config, "STATE_DIR", Path(translated))
        applied[key] = translated
    if ("KB_INGESTED_DIR" in applied
            and "INGESTED_KNOWLEDGE_DIR" not in env):
        monkeypatch.setattr(config, "INGESTED_KNOWLEDGE_DIR",
                            Path(applied["KB_INGESTED_DIR"]))
    # Path-valued keys not explicitly present in compose still need a home in
    # the test sandbox so they do not accidentally touch the repository.
    for key in ("SPEND_GUARD_FILE", "KB_INGESTED_DIR", "INGESTED_KNOWLEDGE_DIR",
                "SUPABASE_PIN_FILE", "SESSION_REVOCATION_FILE",
                "BLOCKLIST_STATE_FILE", "FOMO_PRIVY_STATE_FILE",
                "SECRET_STORE_KEY_FILE"):
        if key in applied:
            continue
        current = getattr(config, key, "")
        if not current:
            continue
        translated = guard.to_temp(str(current))
        if key == "INGESTED_KNOWLEDGE_DIR":
            monkeypatch.setattr(config, key, Path(translated))
        else:
            monkeypatch.setattr(config, key, str(translated))
    if role == "live" and "LIVE_STATE_DIR" in applied:
        live_config.STATE_DIR = Path(applied["LIVE_STATE_DIR"])
    # Make the standard throwaway dirs real.
    for key in ("LIVE_STATE_DIR", "LOG_DIR", "DB_PATH",
                "KB_INGESTED_DIR", "SPEND_GUARD_FILE",
                "SESSION_REVOCATION_FILE", "BLOCKLIST_STATE_FILE",
                "FOMO_PRIVY_STATE_FILE", "SECRET_STORE_KEY_FILE",
                "SUPABASE_PIN_FILE"):
        value = getattr(config, key, "")
        if value:
            Path(value).parent.mkdir(parents=True, exist_ok=True)
    return applied
