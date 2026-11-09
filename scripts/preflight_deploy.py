#!/usr/bin/env python3
"""
scripts/preflight_deploy.py — §87 Phase F: pre-flight the VM before arming.

Reads the deployment `.env` (plus exported environment overrides), verifies
the selected wallet secret channel, inspects the host wallet bind source for
the file channel, and parses `docker compose config`. Every check is
PASS / WARN / FAIL; the CLI exits 1 if ANY check fails. It never opens a
network socket and never prints a secret value.

Runs on the bare VM with the standard library + `cryptography` only:

    python3 scripts/preflight_deploy.py
    python3 scripts/preflight_deploy.py --env-file /opt/trading-bot/app/.env
    docker compose run --rm --entrypoint python live /app/scripts/preflight_deploy.py

The in-image variant sees the container's `/run/secrets/wallet_keypair`, not
the host bind source; run it on the host when checking host ownership/mode.
"""
from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_WALLET_HOST_PATH = "secrets/wallet_keypair.json"
# compose bind-mounts the host keypair at this fixed in-container path. The
# engine reads WALLET_KEYPAIR_PATH, so a host path here would point at a file
# that does not exist inside the live container.
CONTAINER_WALLET_PATH = "/run/secrets/wallet_keypair"
EXPECTED_WALLET_UID = 10001

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

# Mirrors backend/alerts.py FORBIDDEN_NTFY_TOPICS. Kept as a local copy so
# this script stays importable on a bare VM (no backend/httpx import).
FORBIDDEN_NTFY_TOPICS = frozenset({
    "trading-bot",
    "trading-bot-alerts",
    "alerts",
    "tradingbot",
    "default",
    "test",
})

# Shell-exported values override .env for interpolation, matching compose.
# Kept in one tuple so tests can isolate the process environment.
ENV_OVERRIDE_KEYS = (
    'DATA_BACKEND',
    'MAIN_LLM_PROVIDER',
    'DEEPSEEK_API_KEY',
    'GROQ_API_KEY',
    'ADMIN_TOKEN',
    'SEARXNG_SECRET',
    'BACKUP_ENCRYPTION_KEY',
    'SECRET_STORE_KEY',
    'FRONTEND_ORIGIN',
    'SOLANA_RPC_URL',
    'SOLANA_RPC_URL_2',
    'ALERT_NTFY_URL',
    'ALERT_NTFY_TOPIC',
    'ALERT_NTFY_TOKEN',
    'ALERT_TELEGRAM_BOT_TOKEN',
    'ALERT_TELEGRAM_CHAT_ID',
    'WALLET_KEYPAIR_PATH',
    'WALLET_KEYPAIR_HOST_PATH',
    'EXPECTED_WALLET_ADDRESS',
    'DEBUG',
    'RATE_LIMIT_ENABLED',
    'FORCE_HTTPS',
    'MAX_REQUEST_BODY_BYTES',
    'TRUSTED_PROXY_CIDRS',
    'SESSION_SECRET',
    'USE_SUPABASE_DB',
    'SUPABASE_CERT_FINGERPRINT',
    'SUPABASE_PIN_FILE',
)

# The two wallet secret channels are mutually exclusive (see wallet.py):
# WALLET_KEYPAIR_PATH is the mounted-file channel (preferred on a VM), and
# WALLET_KEYPAIR_JSON is the in-memory env channel for hosts without mounts.
# WALLET_KEYPAIR_PATH wins when both are set.
WALLET_CHANNEL_PATH = "path"
WALLET_CHANNEL_JSON = "json"
WALLET_CHANNEL_BOTH = "both"
WALLET_CHANNEL_NONE = "none"

DEFAULT_SEARXNG_SECRETS = frozenset({
    "ultrasecretkey",
    "searxng",
    "changeme",
    "change-me",
    "secret",
    "default",
    "password",
})


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _check(name: str, status: str, detail: str) -> Check:
    return Check(name=name, status=status, detail=detail)


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv-style file (KEY=VALUE, comments, optional quotes).

    Deliberately small and dependency-free. Missing files raise OSError so the
    caller can render a FAIL row instead of silently checking an empty env.
    """
    env: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            env[key] = value
    return env


def _value(env: dict[str, str], key: str) -> str:
    return (env.get(key) or "").strip()


def _current_euid() -> int:
    """Effective uid, or 0 on platforms without geteuid (tests/Windows)."""
    geteuid = getattr(os, "geteuid", None)
    return geteuid() if geteuid is not None else 0


def _not_traversable_detail(path: Path, euid: int) -> str:
    return (
        f"{path}: directory not traversable by uid {euid} — check the "
        "execute (+x) permission on every parent directory"
    )


def check_env_file(path: Path) -> Check:
    """Inspect the .env file without following an unreadable parent path.

    `Path.exists()` swallows PermissionError and reports False, which used to
    render a non-traversable directory as a false "file missing" FAIL. Stat
    first so the operator gets the real cause.
    """
    euid = _current_euid()
    try:
        st = path.stat()
    except FileNotFoundError:
        return _check("env file", FAIL,
                      f"{path} is missing — copy .env.example and fill it")
    except PermissionError:
        return _check("env file", FAIL, _not_traversable_detail(path, euid))
    except OSError as exc:
        return _check("env file", FAIL,
                      f"{path} cannot be inspected: {exc}")
    if not stat.S_ISREG(st.st_mode):
        return _check("env file", FAIL, f"{path} is not a regular file")
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        return _check("env file", WARN,
                      f"{path} is group/world readable (mode {mode:o}); "
                      f"chmod 600 {path}")
    return _check("env file", PASS, f"{path} (mode {mode:o})")


def check_data_backend(env: dict[str, str]) -> Check:
    backend = _value(env, "DATA_BACKEND").lower()
    if backend == "live":
        return _check("data backend", PASS, "DATA_BACKEND=live")
    return _check("data backend", FAIL,
                  f"DATA_BACKEND={backend or '<unset>'} — must be live")


def check_llm_key(env: dict[str, str]) -> Check:
    provider = _value(env, "MAIN_LLM_PROVIDER").lower() or "deepseek"
    keys = {"deepseek": "DEEPSEEK_API_KEY", "groq": "GROQ_API_KEY"}
    key_name = keys.get(provider)
    if key_name is None:
        return _check("main LLM key", FAIL,
                      f"MAIN_LLM_PROVIDER={provider!r} is not a known "
                      f"provider (expected deepseek or groq)")
    if not _value(env, key_name):
        return _check("main LLM key", FAIL,
                      f"MAIN_LLM_PROVIDER={provider} requires {key_name}")
    return _check("main LLM key", PASS,
                  f"{key_name} present for provider {provider}")


def check_admin_token(env: dict[str, str]) -> Check:
    token = _value(env, "ADMIN_TOKEN")
    if not token:
        return _check("admin token", FAIL,
                      "ADMIN_TOKEN is empty — operator API/login fail closed")
    if len(token) < 32:
        return _check("admin token", FAIL,
                      f"ADMIN_TOKEN is {len(token)} chars; need >= 32 "
                      "(openssl rand -hex 32)")
    return _check("admin token", PASS, "ADMIN_TOKEN >= 32 chars")


def check_runtime_security(env: dict[str, str]) -> Check:
    """Refuse runtime settings that would undo the §92 server controls."""
    truthy = {"1", "true", "yes", "on"}
    falsy = {"0", "false", "no", "off"}
    problems: list[str] = []

    if _value(env, "DEBUG").lower() in truthy:
        problems.append("DEBUG is enabled (must be false in deployment)")
    if _value(env, "RATE_LIMIT_ENABLED").lower() in falsy:
        problems.append("RATE_LIMIT_ENABLED is false (tiered limiter disabled)")
    if _value(env, "FORCE_HTTPS").lower() in falsy:
        problems.append("FORCE_HTTPS is false (plaintext origin allowed)")

    body = _value(env, "MAX_REQUEST_BODY_BYTES")
    if body:
        try:
            body_bytes = int(body)
        except ValueError:
            problems.append("MAX_REQUEST_BODY_BYTES is not an integer")
        else:
            if body_bytes <= 0:
                problems.append("MAX_REQUEST_BODY_BYTES must be positive")

    trusted = _value(env, "TRUSTED_PROXY_CIDRS")
    cidrs = [part.strip() for part in trusted.split(",") if part.strip()]
    if any(cidr in {"0.0.0.0/0", "::/0"} for cidr in cidrs):
        problems.append(
            "TRUSTED_PROXY_CIDRS trusts every address (X-Forwarded-For "
            "spoofing)")

    if problems:
        return _check("runtime security", FAIL, "; ".join(problems))
    if not _value(env, "SESSION_SECRET"):
        return _check(
            "runtime security", WARN,
            "SESSION_SECRET unset; sessions derive from ADMIN_TOKEN, so a "
            "token rotation logs every browser out")
    return _check(
        "runtime security", PASS,
        "DEBUG off, limiter and HTTPS on, proxy trust bounded")


def check_searxng_secret(env: dict[str, str]) -> Check:
    secret = _value(env, "SEARXNG_SECRET")
    if not secret:
        return _check("searxng secret", FAIL,
                      "SEARXNG_SECRET is empty (openssl rand -hex 32)")
    if secret.lower() in DEFAULT_SEARXNG_SECRETS:
        return _check("searxng secret", FAIL,
                      "SEARXNG_SECRET is a known default; generate a random "
                      "value (openssl rand -hex 32)")
    if len(secret) < 32:
        return _check("searxng secret", WARN,
                      f"SEARXNG_SECRET is only {len(secret)} chars; prefer "
                      ">= 32 (openssl rand -hex 32)")
    return _check("searxng secret", PASS, "SEARXNG_SECRET is non-default")


def _valid_fernet_key(value: str) -> tuple[bool, str]:
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        return False, f"cryptography unavailable: {exc}"
    try:
        Fernet(value.encode("utf-8"))
    except Exception:
        return False, "not a valid Fernet key"
    return True, "valid Fernet key"


def check_backup_key(env: dict[str, str]) -> Check:
    key = _value(env, "BACKUP_ENCRYPTION_KEY")
    if not key:
        return _check("backup key", FAIL,
                      "BACKUP_ENCRYPTION_KEY is empty — backups cannot be "
                      "encrypted")
    valid, reason = _valid_fernet_key(key)
    if not valid:
        return _check("backup key", FAIL,
                      f"BACKUP_ENCRYPTION_KEY {reason}")
    secret_store_key = _value(env, "SECRET_STORE_KEY")
    if secret_store_key and key == secret_store_key:
        return _check("backup key", FAIL,
                      "BACKUP_ENCRYPTION_KEY equals SECRET_STORE_KEY — use "
                      "a separate key")
    return _check("backup key", PASS,
                  "valid Fernet key, distinct from SECRET_STORE_KEY")


def _origin_hosts(origin: str) -> list[str]:
    return [part.strip() for part in origin.split(",") if part.strip()]


def _is_loopback_host(host: str) -> bool:
    host = host.lower()
    return host in {"localhost", "127.0.0.1", "::1", "[::1]"}


def check_frontend_origin(env: dict[str, str]) -> Check:
    origins = _origin_hosts(_value(env, "FRONTEND_ORIGIN"))
    if not origins:
        return _check("frontend origin", WARN,
                      "FRONTEND_ORIGIN unset — only same-origin/localhost "
                      "requests are allowed")
    loopback_only = True
    insecure: list[str] = []
    for origin in origins:
        parsed = urlparse(origin)
        host = parsed.hostname or ""
        if not _is_loopback_host(host):
            loopback_only = False
            if parsed.scheme != "https":
                insecure.append(origin)
    if insecure:
        return _check("frontend origin", FAIL,
                      "non-loopback origin(s) must be https: "
                      + ", ".join(insecure))
    if loopback_only:
        return _check("frontend origin", WARN,
                      "only loopback origins configured — set the public "
                      "https origin before exposing the dashboard")
    return _check("frontend origin", PASS, "public origin uses https")


def _rpc_host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def check_secondary_rpc(env: dict[str, str]) -> Check:
    primary = _value(env, "SOLANA_RPC_URL")
    secondary = _value(env, "SOLANA_RPC_URL_2")
    if not secondary:
        return _check("secondary RPC", FAIL,
                      "SOLANA_RPC_URL_2 is empty — diverse-RPC consensus "
                      "cannot run")
    primary_host = _rpc_host(primary)
    secondary_host = _rpc_host(secondary)
    if not primary_host or not secondary_host:
        return _check("secondary RPC", FAIL,
                      "SOLANA_RPC_URL and SOLANA_RPC_URL_2 must both be "
                      "absolute URLs with a host")
    if primary_host == secondary_host:
        return _check("secondary RPC", FAIL,
                      f"both RPC URLs use host {primary_host!r}; the second "
                      "endpoint must be a different provider")
    return _check("secondary RPC", PASS,
                  f"{primary_host} != {secondary_host}")


def check_alert_channel(env: dict[str, str]) -> Check:
    topic = _value(env, "ALERT_NTFY_TOPIC")
    url = _value(env, "ALERT_NTFY_URL").rstrip("/") or "https://ntfy.sh"
    token = _value(env, "ALERT_NTFY_TOKEN")
    ntfy_ok = bool(topic)
    if ntfy_ok and ("ntfy.sh" in url or not token):
        if topic.lower() in FORBIDDEN_NTFY_TOPICS:
            ntfy_ok = False
    telegram_ok = bool(_value(env, "ALERT_TELEGRAM_BOT_TOKEN")
                       and _value(env, "ALERT_TELEGRAM_CHAT_ID"))
    if ntfy_ok or telegram_ok:
        channels = [name for name, ok in (("ntfy", ntfy_ok),
                                          ("telegram", telegram_ok)) if ok]
        return _check("alert channel", PASS,
                      "configured: " + ", ".join(channels))
    return _check("alert channel", FAIL,
                  "no usable alert channel — set a private ALERT_NTFY_TOPIC "
                  "or ALERT_TELEGRAM_BOT_TOKEN + ALERT_TELEGRAM_CHAT_ID")


def wallet_channel(env: dict[str, str]) -> str:
    """Which secret channel `.env` selects: path, json, both, or none."""
    has_path = bool(_value(env, "WALLET_KEYPAIR_PATH"))
    has_json = bool(_value(env, "WALLET_KEYPAIR_JSON"))
    if has_path and has_json:
        return WALLET_CHANNEL_BOTH
    if has_json:
        return WALLET_CHANNEL_JSON
    if has_path:
        return WALLET_CHANNEL_PATH
    return WALLET_CHANNEL_NONE


def check_wallet_channel(env: dict[str, str]) -> Check:
    """`.env` must select exactly one wallet secret channel.

    Without this check a JSON-channel deployment (no host keypair file) was
    failed by the host-file check even though the engine signs fine, and a
    no-channel deployment passed the host-file check while the engine would
    refuse to start.

    For the file channel the value must be the compose mount target: the host
    path belongs in WALLET_KEYPAIR_HOST_PATH and is meaningless inside the
    live container.
    """
    channel = wallet_channel(env)
    if channel == WALLET_CHANNEL_NONE:
        return _check(
            "wallet channel", FAIL,
            "no wallet configured: set exactly one of WALLET_KEYPAIR_PATH "
            "(preferred, /run/secrets/wallet_keypair in compose) or "
            "WALLET_KEYPAIR_JSON — the engine refuses to sign without one")
    configured = _value(env, "WALLET_KEYPAIR_PATH")
    if (channel in (WALLET_CHANNEL_PATH, WALLET_CHANNEL_BOTH)
            and configured != CONTAINER_WALLET_PATH):
        return _check(
            "wallet channel", FAIL,
            f"WALLET_KEYPAIR_PATH={configured!r} must be "
            f"{CONTAINER_WALLET_PATH!r} — this is the in-container path; the "
            "host side is WALLET_KEYPAIR_HOST_PATH")
    if channel == WALLET_CHANNEL_BOTH:
        return _check(
            "wallet channel", WARN,
            "both WALLET_KEYPAIR_PATH and WALLET_KEYPAIR_JSON are set; "
            "WALLET_KEYPAIR_PATH wins and the JSON material is ignored — "
            "keep exactly one channel")
    if channel == WALLET_CHANNEL_JSON:
        return _check(
            "wallet channel", PASS,
            "WALLET_KEYPAIR_JSON (env) selected; no host keypair file needed")
    return _check(
        "wallet channel", PASS,
        f"WALLET_KEYPAIR_PATH={configured} (mounted secret file)")


def _wallet_identity_check(env: dict[str, str]) -> Check:
    if _value(env, "EXPECTED_WALLET_ADDRESS"):
        return _check("wallet identity", PASS,
                      "EXPECTED_WALLET_ADDRESS is set (pubkey pinned)")
    return _check(
        "wallet identity", WARN,
        "EXPECTED_WALLET_ADDRESS is unset — the keypair's pubkey is not "
        "pinned; set it before arming")


def _default_uid(path: Path) -> int:
    return path.stat().st_uid


def _wallet_host_path(env: dict[str, str], root: Path) -> Path:
    """Resolve the host side of the compose wallet bind mount."""
    configured = _value(env, "WALLET_KEYPAIR_HOST_PATH")
    path = Path(configured or DEFAULT_WALLET_HOST_PATH)
    if not path.is_absolute():
        path = root / path
    return path


def _json_bind_source_check(env: dict[str, str], root: Path) -> Check:
    """JSON signing needs no host file, but compose still bind-mounts one.

    A missing source is silently created as a root-owned directory by Docker;
    the engine still signs from WALLET_KEYPAIR_JSON, so this is a WARN (and a
    later switch to the file channel would fail until it is fixed).
    """
    path = _wallet_host_path(env, root)
    try:
        st = path.stat()
    except FileNotFoundError:
        return _check(
            "wallet file", WARN,
            f"JSON channel selected but the compose bind mount still needs a "
            f"host file: create an empty placeholder (install -m 600 -o "
            f"10001 /dev/null {path}) or set "
            "WALLET_KEYPAIR_HOST_PATH=/dev/null")
    except PermissionError:
        return _check(
            "wallet file", WARN,
            f"WALLET_KEYPAIR_JSON signs from env, but the compose bind "
            f"source {path} cannot be inspected (parent directory not "
            "traversable)")
    except OSError as exc:
        return _check(
            "wallet file", WARN,
            f"WALLET_KEYPAIR_JSON signs from env, but the compose bind "
            f"source {path} cannot be inspected: {exc}")
    if stat.S_ISCHR(st.st_mode) and path == Path(os.devnull):
        return _check(
            "wallet file", PASS,
            "compose bind source is /dev/null (zero-file placeholder; the "
            "JSON channel signs from env)")
    if not stat.S_ISREG(st.st_mode):
        return _check(
            "wallet file", WARN,
            f"WALLET_KEYPAIR_JSON signs from env, but the compose bind "
            f"source {path} is not a regular file (Docker likely created a "
            f"directory): sudo rmdir {path} && sudo install -m 600 -o 10001 "
            f"/dev/null {path}, or set "
            "WALLET_KEYPAIR_HOST_PATH=/dev/null")
    return _check(
        "wallet file", PASS,
        f"compose bind source {path} is present (unused by the JSON channel)")


def check_wallet_file(env: dict[str, str], root: Path,
                      uid_reader=None, euid_reader=None) -> list[Check]:
    """Host bind source checks: regular file, 0600/0400, uid 10001, readable.

    The host-side readability test is only meaningful when this process runs
    as the container uid (10001) or root. An operator running the preflight
    as their own account (e.g. ubuntu/1000) cannot read a chmod-600 file owned
    by 10001 even though the container can; testing os.access there produced
    a false FAIL. The ownership/mode checks still run for every euid.
    """
    if uid_reader is None:
        uid_reader = _default_uid
    if euid_reader is None:
        euid_reader = _current_euid

    checks: list[Check] = []
    if wallet_channel(env) == WALLET_CHANNEL_JSON:
        checks.append(_json_bind_source_check(env, root))
        checks.append(_wallet_identity_check(env))
        return checks

    path = _wallet_host_path(env, root)

    euid = euid_reader()
    try:
        st = path.stat()
    except FileNotFoundError:
        checks.append(_check(
            "wallet file", FAIL,
            f"{path} is missing — the compose bind mount needs a host file"))
        checks.append(_check(
            "wallet fix", FAIL,
            f"create {path}, then: sudo chown {EXPECTED_WALLET_UID}:"
            f"{EXPECTED_WALLET_UID} {path} && sudo chmod 600 {path}"))
        return checks
    except PermissionError:
        checks.append(_check(
            "wallet file", FAIL, _not_traversable_detail(path, euid)))
        checks.append(_check(
            "wallet fix", FAIL,
            f"sudo chmod +x the parent directories of {path}, then: sudo "
            f"chown {EXPECTED_WALLET_UID}:{EXPECTED_WALLET_UID} {path} && "
            f"sudo chmod 600 {path}"))
        return checks
    except OSError as exc:
        checks.append(_check(
            "wallet file", FAIL, f"{path} cannot be inspected: {exc}"))
        checks.append(_check(
            "wallet fix", FAIL,
            f"inspect {path} and its parent directories, then: sudo chown "
            f"{EXPECTED_WALLET_UID}:{EXPECTED_WALLET_UID} {path} && "
            f"sudo chmod 600 {path}"))
        return checks

    if not stat.S_ISREG(st.st_mode):
        checks.append(_check(
            "wallet file", FAIL,
            f"{path} is not a regular file (a missing bind source can become "
            "a directory)"))
        checks.append(_check(
            "wallet fix", FAIL,
            f"replace it with a keypair file, then: sudo chown "
            f"{EXPECTED_WALLET_UID}:{EXPECTED_WALLET_UID} {path} && "
            f"sudo chmod 600 {path}"))
        return checks

    mode = stat.S_IMODE(st.st_mode)
    uid = uid_reader(path)
    problems: list[str] = []
    if mode not in (0o600, 0o400):
        problems.append(f"mode {mode:o} (must be 600 or 400)")
    if uid != EXPECTED_WALLET_UID:
        problems.append(f"owner uid {uid} (container runs as "
                        f"{EXPECTED_WALLET_UID})")
    # Only the container uid (or root) can answer "will the engine read it?".
    if euid in (EXPECTED_WALLET_UID, 0) and not os.access(path, os.R_OK):
        problems.append(f"not readable by uid {euid}")

    if problems:
        checks.append(_check("wallet file", FAIL,
                             f"{path}: " + "; ".join(problems)))
        checks.append(_check(
            "wallet fix", FAIL,
            f"sudo chown {EXPECTED_WALLET_UID}:{EXPECTED_WALLET_UID} {path} "
            f"&& sudo chmod 600 {path}"))
    else:
        checks.append(_check(
            "wallet file", PASS,
            f"{path} regular, mode {mode:o}, uid {uid}"))

    checks.append(_wallet_identity_check(env))
    return checks


def compose_config_command(root: Path, env_file: Path | None = None) -> list[str]:
    cmd = ["docker", "compose"]
    if env_file is not None:
        cmd += ["--env-file", str(env_file)]
    cmd += ["config", "--format", "json"]
    return cmd


def load_compose_config(root: Path,
                        env_file: Path | None = None) -> tuple[dict | None, str]:
    """Run `docker compose config` locally (no network). Returns (config, error)."""
    try:
        proc = subprocess.run(
            compose_config_command(root, env_file),
            cwd=str(root), capture_output=True, text=True, timeout=45,
        )
    except FileNotFoundError:
        return None, "docker CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return None, "docker compose config timed out"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return None, detail[-1] if detail else "docker compose config failed"
    try:
        import json
        return json.loads(proc.stdout), ""
    except (TypeError, ValueError) as exc:
        return None, f"docker compose config output is not JSON: {exc}"


def _service_env(service: dict) -> dict[str, str]:
    raw = service.get("environment") or {}
    if isinstance(raw, dict):
        return {str(k): "" if v is None else str(v) for k, v in raw.items()}
    out: dict[str, str] = {}
    for item in raw:
        key, _, value = str(item).partition("=")
        out[key] = value
    return out


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _deploy_limit(service: dict, key: str):
    limits = (
        ((service.get("deploy") or {}).get("resources") or {})
        .get("limits") or {}
    )
    return limits.get(key)


def _is_loopback_binding(binding) -> bool:
    if isinstance(binding, dict):
        return binding.get("host_ip") in ("127.0.0.1", "::1")
    text = str(binding)
    return text.startswith(("127.0.0.1:", "[::1]:"))


def _compose_hardening_issues(services: dict) -> list[str]:
    """§92 S8: the compose file must carry the runtime hardening contract."""
    issues: list[str] = []
    for name in ("api", "live"):
        service = services.get(name) or {}
        if service.get("read_only") is not True:
            issues.append(f"{name}: read_only is not true")
        for binding in service.get("ports") or []:
            if not _is_loopback_binding(binding):
                issues.append(f"{name}: publishes a non-loopback port {binding}")

    for name in ("api", "live", "searxng"):
        service = services.get(name) or {}
        if "ALL" not in _as_list(service.get("cap_drop")):
            issues.append(f"{name}: cap_drop does not include ALL")
        if "no-new-privileges:true" not in _as_list(service.get("security_opt")):
            issues.append(f"{name}: no-new-privileges is not set")
        try:
            pids = int(service.get("pids_limit") or 0)
        except (TypeError, ValueError):
            pids = 0
        if pids <= 0:
            issues.append(f"{name}: pids_limit is missing")
        if not (service.get("mem_limit") or _deploy_limit(service, "memory")):
            issues.append(f"{name}: memory limit is missing")
        if not (service.get("cpus") or _deploy_limit(service, "cpus")):
            issues.append(f"{name}: CPU limit is missing")

    if (services.get("searxng") or {}).get("ports"):
        issues.append("searxng: publishes a host port (must stay internal)")
    return issues


def _runtime_inventory():
    try:
        from scripts import audit_runtime_writes
    except ModuleNotFoundError:  # direct `python scripts/preflight_deploy.py`
        import audit_runtime_writes
    return audit_runtime_writes


def _writable_mounts(service: dict) -> list[str]:
    return _runtime_inventory().writable_mounts(service)


def _is_under(path: str, root: str) -> bool:
    return _runtime_inventory().is_under(path, root)


RUNTIME_WRITE_PATHS = {
    role: _runtime_inventory().runtime_keys(role) for role in ("api", "live")
}


def check_runtime_write_paths(config: dict | None, env: dict[str, str],
                              error: str = "") -> list[Check]:
    """No runtime write may resolve onto the read-only root or a :ro mount."""
    if config is None:
        return [_check("runtime write paths", FAIL,
                       error or "compose config unavailable")]
    inventory = _runtime_inventory()
    services = config.get("services") or {}
    findings: list[str] = []
    checked = 0
    for role in ("api", "live"):
        service = services.get(role) or {}
        effective = {**env, **_service_env(service)}
        paths = inventory.resolved_runtime_paths(effective)
        for key in inventory.runtime_keys(role):
            raw = paths.get(key)
            if not raw or not inventory.writable_path(raw, service):
                findings.append(f"{role}: {key} is on the read-only root or a read-only mount")
            else:
                checked += 1
    if findings:
        return [_check("runtime write paths", FAIL, "; ".join(findings))]
    return [_check("runtime write paths", PASS,
                   f"{checked} write paths on writable mounts/tmpfs")]


def check_supabase_tls(config: dict | None, env: dict[str, str],
                       error: str = "") -> list[Check]:
    """Each DB-using service needs an operator pin or a writable TOFU path."""
    if config is None:
        if _value(env, "USE_SUPABASE_DB") != "1":
            return [_check("supabase tls pin", PASS, "USE_SUPABASE_DB is not 1")]
        if _value(env, "SUPABASE_CERT_FINGERPRINT"):
            return [_check("supabase tls pin", PASS,
                           "SUPABASE_CERT_FINGERPRINT is set (no TOFU)")]
        return [_check("supabase tls pin", WARN,
                       error or "compose config unavailable; cannot verify pin path")]
    inventory = _runtime_inventory()
    findings: list[str] = []
    enabled = []
    for role in ("api", "live"):
        service = (config.get("services") or {}).get(role) or {}
        effective = {**env, **_service_env(service)}
        if str(effective.get("USE_SUPABASE_DB", "")) != "1":
            continue
        enabled.append(role)
        if effective.get("SUPABASE_CERT_FINGERPRINT"):
            continue
        pin = inventory.resolved_runtime_paths(effective)["SUPABASE_PIN_FILE"]
        if not inventory.writable_path(pin, service):
            findings.append(role)
    if findings:
        return [_check("supabase tls pin", WARN,
                       "No operator pin or writable SUPABASE_PIN_FILE for "
                       + ", ".join(findings)
                       + "; TOFU pin will be held in memory for the process lifetime")]
    return [_check("supabase tls pin", PASS,
                   "operator pin or writable TOFU path for each enabled service"
                   if enabled else "USE_SUPABASE_DB is not 1")]


def check_compose(config: dict | None, error: str = "") -> list[Check]:
    if config is None:
        return [_check("compose config", FAIL,
                       error or "docker compose config did not parse")]
    services = config.get("services")
    if not isinstance(services, dict):
        return [_check("compose config", FAIL,
                       "compose config has no services mapping")]
    missing = [name for name in ("api", "live", "searxng")
               if name not in services]
    if missing:
        return [_check("compose config", FAIL,
                       "missing compose service(s): " + ", ".join(missing))]

    api_env = _service_env(services["api"])
    leaked = sorted(k for k, v in api_env.items()
                    if k.startswith("WALLET_") and v.strip())
    checks = [_check("compose config", PASS,
                     "parsed; api/live/searxng present")]
    if leaked:
        checks.append(_check(
            "api wallet isolation", FAIL,
            "WALLET_* values are visible to the API service: "
            + ", ".join(leaked)))
    else:
        checks.append(_check(
            "api wallet isolation", PASS,
            "no non-empty WALLET_* in the api service environment"))

    hardening = _compose_hardening_issues(services)
    checks.append(_check(
        "container hardening",
        FAIL if hardening else PASS,
        "; ".join(hardening) if hardening else
        "read-only app root, ALL caps dropped, no-new-privileges, "
        "pid/memory/CPU bounds, searxng internal-only",
    ))
    return checks


def run_preflight(env: dict[str, str], root: Path,
                  compose_config: dict | None = None,
                  compose_error: str = "",
                  env_file: Path | None = None,
                  uid_reader=None,
                  euid_reader=None,
                  include_compose: bool = True) -> list[Check]:
    checks: list[Check] = []
    if env_file is not None:
        checks.append(check_env_file(env_file))
    checks.extend([
        check_data_backend(env),
        check_llm_key(env),
        check_admin_token(env),
        check_runtime_security(env),
        check_searxng_secret(env),
        check_backup_key(env),
        check_frontend_origin(env),
        check_secondary_rpc(env),
        check_alert_channel(env),
        check_wallet_channel(env),
    ])
    checks.extend(check_wallet_file(env, root, uid_reader=uid_reader,
                                    euid_reader=euid_reader))
    if include_compose:
        checks.extend(check_compose(compose_config, compose_error))
        checks.extend(check_runtime_write_paths(compose_config, env,
                                                compose_error))
        checks.extend(check_supabase_tls(compose_config, env, compose_error))
    return checks


def _print_table(checks: list[Check]) -> None:
    width = max([len(c.name) for c in checks] + [5])
    print("=" * 78)
    print("DEPLOY PREFLIGHT (§87 F1) — no network, no secrets printed")
    print("=" * 78)
    for c in checks:
        print(f"{c.status:<4}  {c.name:<{width}}  {c.detail}")
    print("=" * 78)
    failures = sum(1 for c in checks if c.status == FAIL)
    warnings = sum(1 for c in checks if c.status == WARN)
    passed = sum(1 for c in checks if c.status == PASS)
    print(f"{passed} PASS, {warnings} WARN, {failures} FAIL")
    if failures:
        print("DO NOT ARM: fix every FAIL above, then re-run this preflight.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preflight the Oracle VM .env/wallet/compose before "
                    "arming (§87 F1). Exit 1 on any FAIL.")
    parser.add_argument("--env-file", type=Path,
                        help="path to the deployment .env "
                             f"(default: {DEFAULT_ENV_FILE})")
    parser.add_argument("--root", type=Path,
                        help="project root used for relative wallet paths "
                             f"(default: {REPO_ROOT})")
    parser.add_argument("--skip-compose", action="store_true",
                        help="skip `docker compose config` (unit/debug only)")
    args = parser.parse_args(argv)

    root = (args.root or REPO_ROOT).resolve()
    env_file = (args.env_file or root / ".env").resolve()

    file_env: dict[str, str] = {}
    env_error = ""
    try:
        file_env = load_env_file(env_file)
    except OSError as exc:
        env_error = str(exc)

    # Shell-exported values override .env for interpolation, matching compose.
    env = {**file_env}
    for key in ENV_OVERRIDE_KEYS:
        if key in os.environ:
            env[key] = os.environ[key]

    if args.skip_compose:
        config, compose_error = None, ""
    else:
        config, compose_error = load_compose_config(
            root, env_file if env_file.exists() else None)
        if env_error:
            compose_error = f"{compose_error}; {env_error}".strip("; ")

    checks = run_preflight(
        env, root, compose_config=config, compose_error=compose_error,
        env_file=env_file, include_compose=not args.skip_compose,
    )
    _print_table(checks)
    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
