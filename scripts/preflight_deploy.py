#!/usr/bin/env python3
"""
scripts/preflight_deploy.py — §87 Phase F: pre-flight the VM before arming.

Reads the deployment `.env` (plus exported environment overrides), inspects the
host wallet bind source, and parses `docker compose config`. Every check is
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
    "DATA_BACKEND", "MAIN_LLM_PROVIDER", "DEEPSEEK_API_KEY",
    "GROQ_API_KEY", "ADMIN_TOKEN", "SEARXNG_SECRET",
    "BACKUP_ENCRYPTION_KEY", "SECRET_STORE_KEY", "FRONTEND_ORIGIN",
    "SOLANA_RPC_URL", "SOLANA_RPC_URL_2", "ALERT_NTFY_URL",
    "ALERT_NTFY_TOPIC", "ALERT_NTFY_TOKEN",
    "ALERT_TELEGRAM_BOT_TOKEN", "ALERT_TELEGRAM_CHAT_ID",
    "WALLET_KEYPAIR_HOST_PATH", "EXPECTED_WALLET_ADDRESS",
)

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


def check_env_file(path: Path) -> Check:
    if not path.exists():
        return _check("env file", FAIL,
                      f"{path} is missing — copy .env.example and fill it")
    if not path.is_file():
        return _check("env file", FAIL, f"{path} is not a regular file")
    mode = stat.S_IMODE(path.stat().st_mode)
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


def _default_uid(path: Path) -> int:
    return path.stat().st_uid


def check_wallet_file(env: dict[str, str], root: Path,
                      uid_reader=None) -> list[Check]:
    """Host bind source checks: regular file, 0600/0400, uid 10001, readable."""
    if uid_reader is None:
        uid_reader = _default_uid
    configured = _value(env, "WALLET_KEYPAIR_HOST_PATH")
    host_path = configured or DEFAULT_WALLET_HOST_PATH
    path = Path(host_path)
    if not path.is_absolute():
        path = root / path

    checks: list[Check] = []
    if not path.exists():
        checks.append(_check(
            "wallet file", FAIL,
            f"{path} is missing — the compose bind mount needs a host file"))
        checks.append(_check(
            "wallet fix", FAIL,
            f"create {path}, then: sudo chown {EXPECTED_WALLET_UID}:"
            f"{EXPECTED_WALLET_UID} {path} && sudo chmod 600 {path}"))
        return checks
    if not path.is_file():
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

    st = path.stat()
    mode = stat.S_IMODE(st.st_mode)
    uid = uid_reader(path)
    problems: list[str] = []
    if mode not in (0o600, 0o400):
        problems.append(f"mode {mode:o} (must be 600 or 400)")
    if uid != EXPECTED_WALLET_UID:
        problems.append(f"owner uid {uid} (container runs as "
                        f"{EXPECTED_WALLET_UID})")
    if not os.access(path, os.R_OK):
        problems.append("not readable by the current user")

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

    expected = _value(env, "EXPECTED_WALLET_ADDRESS")
    if expected:
        checks.append(_check("wallet identity", PASS,
                             "EXPECTED_WALLET_ADDRESS is set (pubkey pinned)"))
    else:
        checks.append(_check(
            "wallet identity", WARN,
            "EXPECTED_WALLET_ADDRESS is unset — the keypair's pubkey is not "
            "pinned; set it before arming"))
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
    return checks


def run_preflight(env: dict[str, str], root: Path,
                  compose_config: dict | None = None,
                  compose_error: str = "",
                  env_file: Path | None = None,
                  uid_reader=None,
                  include_compose: bool = True) -> list[Check]:
    checks: list[Check] = []
    if env_file is not None:
        checks.append(check_env_file(env_file))
    checks.extend([
        check_data_backend(env),
        check_llm_key(env),
        check_admin_token(env),
        check_searxng_secret(env),
        check_backup_key(env),
        check_frontend_origin(env),
        check_secondary_rpc(env),
        check_alert_channel(env),
    ])
    checks.extend(check_wallet_file(env, root, uid_reader=uid_reader))
    if include_compose:
        checks.extend(check_compose(compose_config, compose_error))
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
