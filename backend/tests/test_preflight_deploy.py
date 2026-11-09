"""
§87 Phase F — deploy preflight checks.

Hermetic: no docker daemon, no network. `check_compose` consumes a parsed
`docker compose config` dict; the wallet owner is injected so the uid-10001
contract is testable on any build user.
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from scripts import preflight_deploy as pf


def _env(**overrides) -> dict[str, str]:
    env = {
        "DATA_BACKEND": "live",
        "MAIN_LLM_PROVIDER": "deepseek",
        "DEEPSEEK_API_KEY": "sk-deepseek-test",
        "ADMIN_TOKEN": "a" * 40,
        "SEARXNG_SECRET": "b" * 40,
        "BACKUP_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "SECRET_STORE_KEY": "",
        "FRONTEND_ORIGIN": "https://bot.example.com",
        "SOLANA_RPC_URL": "https://mainnet.helius-rpc.com/?api-key=x",
        "SOLANA_RPC_URL_2": "https://solana-mainnet.g.alchemy.com/v2/x",
        "ALERT_NTFY_URL": "https://ntfy.sh",
        "ALERT_NTFY_TOPIC": "private-abc123",
        "WALLET_KEYPAIR_PATH": "/run/secrets/wallet_keypair",
        "WALLET_KEYPAIR_HOST_PATH": "secrets/wallet_keypair.json",
        "EXPECTED_WALLET_ADDRESS": "ExpectedPubkey1111111111111111111111111",
    }
    env.update(overrides)
    return env


def _wallet(root, mode=0o600):
    path = root / "secrets" / "wallet_keypair.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1,2,3]")
    path.chmod(mode)
    return path


def _hardened_service(**overrides) -> dict:
    service = {
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "pids_limit": 256,
        "mem_limit": "1g",
        "cpus": 1.0,
    }
    service.update(overrides)
    return service


def _compose(**api_env) -> dict:
    """The real repo compose, with the API wallet env overridden for tests."""
    import yaml
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load(
        (repo / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["api"]
    env = service.get("environment") or []
    merged = []
    for item in env:
        key = str(item).split("=", 1)[0]
        if key in api_env:
            merged.append(f"{key}={api_env[key]}")
        else:
            merged.append(str(item))
    for key, value in api_env.items():
        if not any(str(i).split("=", 1)[0] == key for i in env):
            merged.append(f"{key}={value}")
    service["environment"] = merged
    return compose


def _run(env, root, compose=None, **kw):
    return pf.run_preflight(
        env, root, compose_config=_compose() if compose is None else compose,
        uid_reader=lambda p: 10001, **kw)


def _status(checks, name):
    return next(c.status for c in checks if c.name == name)


# ---------------------------------------------------------------- happy path

def test_all_good_preflight_passes(tmp_path):
    _wallet(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("DATA_BACKEND=live\n")
    env_file.chmod(0o600)

    checks = _run(_env(), tmp_path, env_file=env_file)

    assert all(c.status != pf.FAIL for c in checks), checks
    assert {c.status for c in checks} <= {pf.PASS, pf.WARN}
    assert _status(checks, "wallet file") == pf.PASS
    assert _status(checks, "compose config") == pf.PASS


# ---------------------------------------------------------------- env file

def test_missing_env_file_is_a_fail(tmp_path):
    check = pf.check_env_file(tmp_path / ".env")
    assert check.status == pf.FAIL
    assert "missing" in check.detail


def test_group_readable_env_file_is_a_warning(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("X=1\n")
    env_file.chmod(0o644)
    check = pf.check_env_file(env_file)
    assert check.status == pf.WARN
    assert "chmod 600" in check.detail


def test_load_env_file_parses_quotes_comments_and_export(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "export DATA_BACKEND=live\n"
        "ADMIN_TOKEN='abc'\n"
        'QUOTED="value"\n'
        "EMPTY=\n"
        "NOT A LINE\n"
    )
    env = pf.load_env_file(env_file)
    assert env == {"DATA_BACKEND": "live", "ADMIN_TOKEN": "abc",
                   "QUOTED": "value", "EMPTY": ""}


# ---------------------------------------------------------------- data/LLM

def test_data_backend_must_be_live():
    assert pf.check_data_backend(_env(DATA_BACKEND="live")).status == pf.PASS
    for bad in ("mock", "", "paper"):
        assert pf.check_data_backend(_env(DATA_BACKEND=bad)).status == pf.FAIL


def test_main_llm_key_follows_the_selected_provider():
    assert pf.check_llm_key(_env()).status == pf.PASS
    assert pf.check_llm_key(
        _env(MAIN_LLM_PROVIDER="deepseek", DEEPSEEK_API_KEY="")).status == pf.FAIL
    assert pf.check_llm_key(
        _env(MAIN_LLM_PROVIDER="groq", GROQ_API_KEY="g")).status == pf.PASS
    assert pf.check_llm_key(
        _env(MAIN_LLM_PROVIDER="groq", GROQ_API_KEY="")).status == pf.FAIL
    assert pf.check_llm_key(
        _env(MAIN_LLM_PROVIDER="typo")).status == pf.FAIL


# ---------------------------------------------------------------- secrets

def test_admin_token_must_be_at_least_32_chars():
    assert pf.check_admin_token(_env(ADMIN_TOKEN="a" * 32)).status == pf.PASS
    assert pf.check_admin_token(_env(ADMIN_TOKEN="a" * 31)).status == pf.FAIL
    assert pf.check_admin_token(_env(ADMIN_TOKEN="")).status == pf.FAIL


def test_searxng_secret_rejects_empty_and_defaults():
    assert pf.check_searxng_secret(_env(SEARXNG_SECRET="c" * 40)).status == pf.PASS
    for bad in ("", "ultrasecretkey", "changeme", "secret"):
        assert pf.check_searxng_secret(_env(SEARXNG_SECRET=bad)).status == pf.FAIL
    assert pf.check_searxng_secret(_env(SEARXNG_SECRET="short")).status == pf.WARN


def test_backup_key_must_be_valid_fernet_and_distinct():
    good = Fernet.generate_key().decode()
    assert pf.check_backup_key(_env(BACKUP_ENCRYPTION_KEY=good)).status == pf.PASS
    assert pf.check_backup_key(_env(BACKUP_ENCRYPTION_KEY="")).status == pf.FAIL
    assert pf.check_backup_key(_env(BACKUP_ENCRYPTION_KEY="not-a-key")).status == pf.FAIL
    assert pf.check_backup_key(_env(
        BACKUP_ENCRYPTION_KEY=good, SECRET_STORE_KEY=good)).status == pf.FAIL


# ---------------------------------------------------------------- network config

def test_frontend_origin_requires_https_for_public_hosts(tmp_path):
    assert pf.check_frontend_origin(
        _env(FRONTEND_ORIGIN="https://bot.example.com")).status == pf.PASS
    assert pf.check_frontend_origin(
        _env(FRONTEND_ORIGIN="http://bot.example.com")).status == pf.FAIL
    assert pf.check_frontend_origin(
        _env(FRONTEND_ORIGIN="http://localhost:8000,http://127.0.0.1:8000")
    ).status == pf.WARN
    assert pf.check_frontend_origin(
        _env(FRONTEND_ORIGIN="https://localhost:8000")).status == pf.WARN
    assert pf.check_frontend_origin(
        _env(FRONTEND_ORIGIN="")).status == pf.WARN


def test_secondary_rpc_must_have_a_different_host():
    assert pf.check_secondary_rpc(_env()).status == pf.PASS
    assert pf.check_secondary_rpc(
        _env(SOLANA_RPC_URL_2="")).status == pf.FAIL
    assert pf.check_secondary_rpc(_env(
        SOLANA_RPC_URL="https://api.mainnet-beta.solana.com",
        SOLANA_RPC_URL_2="https://api.mainnet-beta.solana.com/other-key",
    )).status == pf.FAIL
    assert pf.check_secondary_rpc(_env(
        SOLANA_RPC_URL="", SOLANA_RPC_URL_2="https://x.example")).status == pf.FAIL


def test_alert_channel_accepts_ntfy_or_telegram_only():
    assert pf.check_alert_channel(_env()).status == pf.PASS
    assert pf.check_alert_channel(_env(
        ALERT_NTFY_TOPIC="",
        ALERT_TELEGRAM_BOT_TOKEN="t", ALERT_TELEGRAM_CHAT_ID="c",
    )).status == pf.PASS
    assert pf.check_alert_channel(_env(
        ALERT_NTFY_TOPIC="trading-bot")).status == pf.FAIL
    assert pf.check_alert_channel(_env(
        ALERT_NTFY_TOPIC="",
        ALERT_TELEGRAM_BOT_TOKEN="t", ALERT_TELEGRAM_CHAT_ID="",
    )).status == pf.FAIL
    assert pf.check_alert_channel(_env(ALERT_NTFY_TOPIC="")).status == pf.FAIL


# ---------------------------------------------------------------- wallet

def test_wallet_good_file_passes_and_identity_warns_when_unset(tmp_path):
    _wallet(tmp_path)
    checks = pf.check_wallet_file(_env(), tmp_path, uid_reader=lambda p: 10001)
    assert _status(checks, "wallet file") == pf.PASS
    assert _status(checks, "wallet identity") == pf.PASS

    checks = pf.check_wallet_file(
        _env(EXPECTED_WALLET_ADDRESS=""), tmp_path, uid_reader=lambda p: 10001)
    assert _status(checks, "wallet identity") == pf.WARN


def test_wallet_missing_file_fails_with_the_exact_fix(tmp_path):
    checks = pf.check_wallet_file(_env(), tmp_path, uid_reader=lambda p: 10001)
    assert _status(checks, "wallet file") == pf.FAIL
    fix = next(c for c in checks if c.name == "wallet fix")
    assert fix.status == pf.FAIL
    assert "sudo chown 10001:10001" in fix.detail
    assert "sudo chmod 600" in fix.detail


def test_wallet_directory_is_not_a_keypair(tmp_path):
    (tmp_path / "secrets" / "wallet_keypair.json").mkdir(parents=True)
    checks = pf.check_wallet_file(_env(), tmp_path, uid_reader=lambda p: 10001)
    assert _status(checks, "wallet file") == pf.FAIL
    assert any("not a regular file" in c.detail for c in checks)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o777])
def test_wallet_loose_mode_fails(tmp_path, mode):
    _wallet(tmp_path, mode)
    checks = pf.check_wallet_file(_env(), tmp_path, uid_reader=lambda p: 10001)
    assert _status(checks, "wallet file") == pf.FAIL
    assert any("mode" in c.detail for c in checks)


def test_wallet_0400_is_accepted(tmp_path):
    _wallet(tmp_path, 0o400)
    checks = pf.check_wallet_file(_env(), tmp_path, uid_reader=lambda p: 10001)
    assert _status(checks, "wallet file") == pf.PASS


def test_wallet_wrong_owner_fails_with_chown(tmp_path):
    _wallet(tmp_path)
    checks = pf.check_wallet_file(_env(), tmp_path, uid_reader=lambda p: 1000)
    assert _status(checks, "wallet file") == pf.FAIL
    assert any("owner uid 1000" in c.detail for c in checks)
    assert any("sudo chown 10001:10001" in c.detail for c in checks)


def test_wallet_path_is_resolved_relative_to_root(tmp_path):
    _wallet(tmp_path)
    checks = pf.check_wallet_file(_env(), tmp_path, uid_reader=lambda p: 10001)
    assert str(tmp_path / "secrets" / "wallet_keypair.json") in (
        next(c for c in checks if c.name == "wallet file").detail)


# ---------------------------------------------------------------- wallet channel

def test_wallet_channel_requires_exactly_one_channel():
    assert pf.check_wallet_channel(_env()).status == pf.PASS
    assert pf.check_wallet_channel(_env(
        WALLET_KEYPAIR_PATH="",
        WALLET_KEYPAIR_JSON="[1,2,3]")).status == pf.PASS
    assert pf.check_wallet_channel(_env(
        WALLET_KEYPAIR_JSON="[1,2,3]")).status == pf.WARN
    assert pf.check_wallet_channel(_env(
        WALLET_KEYPAIR_PATH="")).status == pf.FAIL


def test_path_channel_must_use_the_container_mount_target():
    assert pf.CONTAINER_WALLET_PATH == "/run/secrets/wallet_keypair"
    good = pf.check_wallet_channel(_env())
    assert good.status == pf.PASS

    host_path = pf.check_wallet_channel(_env(
        WALLET_KEYPAIR_PATH="/opt/trading-bot/app/secrets/wallet_keypair.json"))
    assert host_path.status == pf.FAIL
    assert pf.CONTAINER_WALLET_PATH in host_path.detail
    assert "this is the in-container path" in host_path.detail
    assert "host side is WALLET_KEYPAIR_HOST_PATH" in host_path.detail

    both = pf.check_wallet_channel(_env(
        WALLET_KEYPAIR_PATH="/opt/trading-bot/app/secrets/wallet_keypair.json",
        WALLET_KEYPAIR_JSON="[1,2,3]"))
    assert both.status == pf.FAIL
    assert pf.CONTAINER_WALLET_PATH in both.detail
    assert "this is the in-container path" in both.detail

    both_with_container_path = pf.check_wallet_channel(_env(
        WALLET_KEYPAIR_JSON="[1,2,3]"))
    assert both_with_container_path.status == pf.WARN
    assert "exactly one channel" in both_with_container_path.detail


def test_json_channel_warns_when_the_compose_bind_source_is_missing(tmp_path):
    """JSON-channel signing works, but compose still bind-mounts a host path."""
    env = _env(WALLET_KEYPAIR_PATH="", WALLET_KEYPAIR_JSON="[1,2,3]")
    checks = _run(env, tmp_path)
    assert all(c.status != pf.FAIL for c in checks), checks
    assert _status(checks, "wallet channel") == pf.PASS
    assert _status(checks, "wallet file") == pf.WARN
    detail = next(c for c in checks if c.name == "wallet file").detail
    assert "bind" in detail
    assert "install -m 600 -o 10001 /dev/null" in detail
    assert "WALLET_KEYPAIR_HOST_PATH=/dev/null" in detail


def test_json_channel_warns_when_the_compose_bind_source_is_a_directory(
        tmp_path):
    (tmp_path / "secrets" / "wallet_keypair.json").mkdir(parents=True)
    env = _env(WALLET_KEYPAIR_PATH="", WALLET_KEYPAIR_JSON="[1,2,3]")
    checks = pf.check_wallet_file(env, tmp_path, uid_reader=lambda p: 10001)
    assert _status(checks, "wallet file") == pf.WARN
    assert "not a regular file" in next(
        c for c in checks if c.name == "wallet file").detail


def test_json_channel_accepts_the_devnull_placeholder(tmp_path):
    env = _env(
        WALLET_KEYPAIR_PATH="", WALLET_KEYPAIR_JSON="[1,2,3]",
        WALLET_KEYPAIR_HOST_PATH="/dev/null")
    checks = _run(env, tmp_path)
    assert all(c.status != pf.FAIL for c in checks), checks
    wallet = next(c for c in checks if c.name == "wallet file")
    assert wallet.status == pf.PASS
    assert "zero-file placeholder" in wallet.detail
    assert "/dev/null" in wallet.detail
    assert not any(
        c.status == pf.WARN and "wallet file" in c.detail for c in checks)


def test_json_channel_directory_warn_carries_the_rmdir_and_install_fix(
        tmp_path):
    (tmp_path / "secrets" / "wallet_keypair.json").mkdir(parents=True)
    env = _env(WALLET_KEYPAIR_PATH="", WALLET_KEYPAIR_JSON="[1,2,3]")
    checks = pf.check_wallet_file(env, tmp_path, uid_reader=lambda p: 10001)
    wallet = next(c for c in checks if c.name == "wallet file")
    assert wallet.status == pf.WARN
    assert "not a regular file" in wallet.detail
    assert "Docker likely created a directory" in wallet.detail
    assert "sudo rmdir" in wallet.detail
    assert "install -m 600 -o 10001 /dev/null" in wallet.detail
    assert "WALLET_KEYPAIR_HOST_PATH=/dev/null" in wallet.detail


def test_json_channel_other_character_device_stays_a_warning(tmp_path):
    env = _env(
        WALLET_KEYPAIR_PATH="", WALLET_KEYPAIR_JSON="[1,2,3]",
        WALLET_KEYPAIR_HOST_PATH="/dev/zero")
    checks = pf.check_wallet_file(env, tmp_path, uid_reader=lambda p: 10001)
    wallet = next(c for c in checks if c.name == "wallet file")
    assert wallet.status == pf.WARN
    assert "not a regular file" in wallet.detail


def test_file_channel_rejects_the_devnull_placeholder(tmp_path):
    env = _env(WALLET_KEYPAIR_HOST_PATH="/dev/null")
    checks = pf.check_wallet_file(env, tmp_path, uid_reader=lambda p: 10001)
    wallet = next(c for c in checks if c.name == "wallet file")
    assert wallet.status == pf.FAIL
    assert "not a regular file" in wallet.detail


def test_json_channel_passes_when_the_compose_bind_source_is_a_file(tmp_path):
    _wallet(tmp_path)
    env = _env(WALLET_KEYPAIR_PATH="", WALLET_KEYPAIR_JSON="[1,2,3]")
    checks = pf.check_wallet_file(env, tmp_path, uid_reader=lambda p: 10001)
    assert _status(checks, "wallet file") == pf.PASS
    assert "unused" in next(
        c for c in checks if c.name == "wallet file").detail


def test_readability_checked_only_as_container_uid_or_root(tmp_path, monkeypatch):
    """Running as ubuntu/1000 cannot read a 0600 file owned by 10001, but the
    container can. The host-side os.access check must not FAIL there."""
    _wallet(tmp_path)
    monkeypatch.setattr(pf.os, "access", lambda path, mode: False)

    def check(euid):
        return pf.check_wallet_file(
            _env(), tmp_path, uid_reader=lambda p: 10001,
            euid_reader=lambda: euid)

    assert _status(check(1000), "wallet file") == pf.PASS
    for euid in (10001, 0):
        checks = check(euid)
        assert _status(checks, "wallet file") == pf.FAIL
        assert "not readable" in next(
            c for c in checks if c.name == "wallet file").detail


def test_env_file_permission_error_is_reported_as_not_traversable(
        tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("X=1\n")
    real_stat = type(env_file).stat

    def denied(self, *args, **kwargs):
        if self == env_file:
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(type(env_file), "stat", denied)
    check = pf.check_env_file(env_file)
    assert check.status == pf.FAIL
    assert "directory not traversable" in check.detail


def test_wallet_permission_error_is_reported_as_not_traversable(
        tmp_path, monkeypatch):
    wallet = _wallet(tmp_path)
    real_stat = type(wallet).stat

    def denied(self, *args, **kwargs):
        if self == wallet:
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(type(wallet), "stat", denied)
    checks = pf.check_wallet_file(
        _env(), tmp_path, uid_reader=lambda p: 10001,
        euid_reader=lambda: 1000)
    wallet_check = next(c for c in checks if c.name == "wallet file")
    assert wallet_check.status == pf.FAIL
    assert "directory not traversable" in wallet_check.detail
    assert "missing" not in wallet_check.detail


# ---------------------------------------------------------------- compose

def test_compose_good_config_passes():
    checks = pf.check_compose(_compose())
    assert _status(checks, "compose config") == pf.PASS
    assert _status(checks, "api wallet isolation") == pf.PASS


def test_compose_parse_error_fails():
    checks = pf.check_compose(None, "no such service: searxng")
    assert len(checks) == 1
    assert checks[0].status == pf.FAIL
    assert "searxng" in checks[0].detail


def test_compose_missing_service_fails():
    checks = pf.check_compose({"services": {"api": {}, "live": {}}})
    assert checks[0].status == pf.FAIL
    assert "searxng" in checks[0].detail


def test_compose_api_wallet_leak_fails_without_printing_the_value():
    checks = pf.check_compose(_compose(WALLET_KEYPAIR_JSON="SECRETVALUE"))
    leak = next(c for c in checks if c.name == "api wallet isolation")
    assert leak.status == pf.FAIL
    assert "WALLET_KEYPAIR_JSON" in leak.detail
    assert "SECRETVALUE" not in leak.detail


def test_compose_api_env_list_form_is_supported():
    config = {"services": {
        "api": {"environment": ["WALLET_KEYPAIR_PATH=",
                                "WALLET_KEYPAIR_JSON=", "ROLE=api"]},
        "live": {}, "searxng": {},
    }}
    checks = pf.check_compose(config)
    assert _status(checks, "api wallet isolation") == pf.PASS


# ---------------------------------------------------------------- CLI

def test_cli_returns_zero_when_every_check_passes(tmp_path, monkeypatch,
                                                  capsys):
    _wallet(tmp_path)
    env_file = tmp_path / ".env"
    env = _env()
    env_file.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    env_file.chmod(0o600)
    monkeypatch.setattr(pf, "_default_uid", lambda p: 10001)
    for key in pf.ENV_OVERRIDE_KEYS:
        monkeypatch.delenv(key, raising=False)

    rc = pf.main(["--env-file", str(env_file), "--root", str(tmp_path),
                  "--skip-compose"])

    out = capsys.readouterr().out
    assert rc == 0, out
    assert "0 FAIL" in out
    assert env["BACKUP_ENCRYPTION_KEY"] not in out, "secret leaked to output"


def test_cli_returns_one_on_any_failure(tmp_path, capsys):
    _wallet(tmp_path)
    env = _env(DATA_BACKEND="mock")
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    env_file.chmod(0o600)

    rc = pf.main(["--env-file", str(env_file), "--root", str(tmp_path),
                  "--skip-compose"])

    out = capsys.readouterr().out
    assert rc == 1
    assert "DO NOT ARM" in out
