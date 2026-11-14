"""
tests/test_deploy_contracts.py — static deploy contracts (§87 Phase A).

No Docker daemon is available in the audit sandbox, so these are source
contracts that catch the whole class of "a volume was added at /app/... but
the image never created the mount point" (root-owned mount point -> the bot
user cannot write) and the wallet-mount/uid contract that made the keypair
unreadable inside the container.
"""
from __future__ import annotations

import re

import yaml

import config

ROOT = config.BASE_DIR.parent
COMPOSE = ROOT / "docker-compose.yml"
DOCKERFILE = ROOT / "Dockerfile"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def _dockerfile_mkdir_line() -> str:
    text = DOCKERFILE.read_text()
    match = re.search(r"mkdir -p ([^\n]+)", text)
    assert match, "Dockerfile has no 'mkdir -p' runtime-directory line"
    return match.group(1)


def test_every_app_volume_target_is_created_by_the_image():
    """A named/bind mount at /app/<x> is created root:root by Docker when the
    image has no such directory. Every declared target under /app must appear
    in the image's mkdir list, so the next volume added cannot be forgotten."""
    mkdir_line = _dockerfile_mkdir_line()
    missing = []
    for service_name, service in _compose()["services"].items():
        for volume in service.get("volumes") or []:
            if not isinstance(volume, str):
                continue
            parts = volume.split(":")
            if len(parts) < 2:
                continue
            target = parts[1]
            if not target.startswith("/app/"):
                continue
            if target not in mkdir_line:
                missing.append((service_name, target))
    assert not missing, (
        "volume targets missing from the Dockerfile mkdir -p line: "
        f"{missing}\nline: {mkdir_line}"
    )


def test_backup_volume_point_is_private_to_bot():
    text = DOCKERFILE.read_text()
    mkdir_line = _dockerfile_mkdir_line()
    assert "/app/backups" in mkdir_line
    assert "chmod 700 /app/backups" in text
    # chown -R bot:bot /app runs AFTER the mkdir, so backups ends up bot-owned.
    assert text.index("mkdir -p") < text.index("chown -R bot:bot /app")


def test_live_wallet_mount_is_enabled_with_one_documented_path():
    live = _compose()["services"]["live"]
    volumes = live.get("volumes") or []
    wallet_mounts = [v for v in volumes
                     if isinstance(v, str)
                     and ":/run/secrets/wallet_keypair" in v]
    assert wallet_mounts == [
        "${WALLET_KEYPAIR_HOST_PATH:-./secrets/wallet_keypair.json}"
        ":/run/secrets/wallet_keypair:ro"
    ]
    # The api service must never receive wallet key material.
    api_env = _compose()["services"]["api"].get("environment") or []
    assert "WALLET_KEYPAIR_PATH=" in api_env
    assert "WALLET_KEYPAIR_JSON=" in api_env
    api_volumes = _compose()["services"]["api"].get("volumes") or []
    assert not any("wallet_keypair" in v for v in api_volumes)


def test_live_restart_policy_survives_daemon_restart_and_has_grace():
    live = _compose()["services"]["live"]
    assert live["restart"] == "unless-stopped"
    assert live["init"] is True
    assert live["stop_grace_period"] == "150s"


def test_all_services_have_bounded_json_file_logs():
    compose = _compose()
    anchor = compose["x-logging"]
    assert anchor["driver"] == "json-file"
    assert anchor["options"]["max-size"] == "10m"
    assert anchor["options"]["max-file"] == "5"
    for name, service in compose["services"].items():
        assert service.get("logging") == anchor, f"{name} has no bounded logging"


def test_backup_wrapper_is_cwd_independent_and_fails_loudly():
    script = (ROOT / "deploy" / "backup.sh").read_text()
    assert 'cd "$(dirname "$0")/.."' in script
    assert "if ! docker compose exec -T live python /app/scripts/backup_state.py" in script
    assert "exit 1" in script
    guide = (ROOT / "docs" / "07_BACKUP_RESTORE.md").read_text()
    assert "cd /opt/trading-bot/app && ./deploy/backup.sh" in guide
    assert "cd /opt/trading-bot && ./deploy/backup.sh" not in guide


def test_dockerignore_keeps_wallet_material_and_state_archives_out():
    """The build context must never include the wallet keypair or encrypted
    backups, even though the Dockerfile does not COPY them: BuildKit still
    sends the context to the daemon, and a future COPY must not be able to
    bake a secret in by accident."""
    text = (ROOT / ".dockerignore").read_text()
    for pattern in ("secrets/", "**/wallet_keypair*.json", "*.enc", "backups/"):
        assert pattern in text, f".dockerignore is missing {pattern!r}"


def test_dockerfile_declares_sigterm_and_private_state_dirs():
    text = DOCKERFILE.read_text()
    assert "STOPSIGNAL SIGTERM" in text
    # Real-money state and the SQLite book are not world-readable in the image.
    match = re.search(r"chmod 700 ([^\n]+)", text)
    assert match, "Dockerfile has no chmod 700 runtime-directory line"
    private = match.group(1)
    for path in ("/app/backups", "/app/data",
                 "/app/backend/live_execution/state"):
        assert path in private, f"{path} is not chmod 700 in the image"


def test_api_service_reaps_children_and_has_a_stop_window():
    """The API must shut down on SIGTERM like the live service; init reaps
    the uvicorn child instead of leaving zombies for the container's PID 1."""
    api = _compose()["services"]["api"]
    assert api["init"] is True
    assert api["stop_signal"] == "SIGTERM"
    assert api["stop_grace_period"] == "30s"


def test_live_service_declares_its_stop_signal():
    live = _compose()["services"]["live"]
    assert live["stop_signal"] == "SIGTERM"
    assert live["stop_grace_period"] == "150s"


def test_compose_exposes_skip_browser_to_both_build_services():
    """§90-D1: a Chromium install can fail under QEMU/ARM. Both services build
    the same image, so both must accept SKIP_BROWSER=1 from the operator/CI."""
    compose = _compose()
    for name in ("api", "live"):
        build = compose["services"][name]["build"]
        assert isinstance(build, dict), f"{name} build is not a mapping"
        assert build["args"]["SKIP_BROWSER"] == "${SKIP_BROWSER:-0}"
