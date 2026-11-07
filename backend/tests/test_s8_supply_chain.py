"""
backend/tests/test_s8_supply_chain.py — §92 S8 supply-chain and host proofs.

A secure application image is still compromised if CI runs a movable action
tag, the base image silently changes under it, or the container is started
with the default capability set and a writable root filesystem. These tests
pin:

  * every GitHub Action to a full commit SHA;
  * every container base image to a registry digest;
  * compose hardening (read-only root, dropped capabilities, no privilege
    escalation, pid/memory/CPU bounds, no SearXNG host port);
  * deploy preflight refusing DEBUG / disabled rate limits / disabled HTTPS /
    wildcard proxy trust / a zero body cap and lax compose services;
  * the JSON wallet channel warning operators that env secrets are visible to
    `docker inspect`, without ever logging the key material.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest
import yaml

import config
from live_execution import wallet
from live_execution import config as live_config
from scripts import preflight_deploy as pf

ROOT = Path(config.BASE_DIR).parent
WORKFLOW_DIR = ROOT / ".github" / "workflows"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}(?:\s|$)")


def _workflow_steps() -> list[tuple[str, dict]]:
    steps: list[tuple[str, dict]] = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job in (document.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                steps.append((path.name, step))
    return steps


def test_every_github_action_is_pinned_to_a_commit_sha():
    offenders = []
    for workflow, step in _workflow_steps():
        uses = (step.get("uses") or "").strip()
        if not uses or uses.startswith("./") or uses.startswith("docker://"):
            continue
        reference = uses.rsplit("@", 1)[-1]
        if not SHA_RE.fullmatch(reference):
            offenders.append(f"{workflow}: {uses}")
    assert not offenders, (
        "movable action tags are forbidden; pin to a commit SHA:\n"
        + "\n".join(offenders)
    )


def test_container_base_images_are_digest_pinned():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    from_lines = [
        line.strip() for line in dockerfile.splitlines()
        if line.strip().upper().startswith("FROM ")
    ]
    assert len(from_lines) >= 2, from_lines
    for line in from_lines:
        assert DIGEST_RE.search(line), (
            f"base image is not digest-pinned: {line}")

    compose = yaml.safe_load(
        (ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    searxng_image = compose["services"]["searxng"]["image"]
    assert DIGEST_RE.search(searxng_image), (
        f"SearXNG image is not digest-pinned: {searxng_image}")


def _compose() -> dict:
    return yaml.safe_load(
        (ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_compose_app_containers_are_hardened():
    compose = _compose()
    services = compose["services"]

    for name in ("api", "live"):
        service = services[name]
        assert service.get("read_only") is True, name
        assert "ALL" in (service.get("cap_drop") or []), name
        assert "no-new-privileges:true" in (service.get("security_opt") or []), name
        assert int(service.get("pids_limit") or 0) > 0, name
        assert service.get("mem_limit"), name
        assert float(service.get("cpus") or 0) > 0, name

    for name in ("api", "live", "searxng"):
        service = services[name]
        assert "ALL" in (service.get("cap_drop") or []), name
        assert "no-new-privileges:true" in (service.get("security_opt") or []), name
        assert int(service.get("pids_limit") or 0) > 0, name
        assert service.get("mem_limit"), name
        assert float(service.get("cpus") or 0) > 0, name

    # The search sidecar is internal-only and the execution container must
    # never publish a port. The API binds loopback for the reverse proxy.
    assert not services["searxng"].get("ports")
    assert not services["live"].get("ports")
    for binding in services["api"].get("ports") or []:
        assert str(binding).startswith("127.0.0.1:"), binding


def _safe_env() -> dict[str, str]:
    return {
        "DEBUG": "false",
        "RATE_LIMIT_ENABLED": "true",
        "FORCE_HTTPS": "true",
        "MAX_REQUEST_BODY_BYTES": "16777216",
        "TRUSTED_PROXY_CIDRS": "127.0.0.1,172.16.0.0/12,::1/128",
        "SESSION_SECRET": "s" * 48,
    }


@pytest.mark.parametrize("overrides,needle", [
    ({"DEBUG": "true"}, "DEBUG"),
    ({"RATE_LIMIT_ENABLED": "false"}, "RATE_LIMIT_ENABLED"),
    ({"FORCE_HTTPS": "false"}, "FORCE_HTTPS"),
    ({"MAX_REQUEST_BODY_BYTES": "0"}, "MAX_REQUEST_BODY_BYTES"),
    ({"TRUSTED_PROXY_CIDRS": "127.0.0.1,0.0.0.0/0"}, "TRUSTED_PROXY_CIDRS"),
])
def test_preflight_rejects_insecure_runtime_env(overrides, needle):
    check = pf.check_runtime_security({**_safe_env(), **overrides})
    assert check.status == pf.FAIL
    assert needle in check.detail


def test_preflight_accepts_hardened_runtime_env():
    assert pf.check_runtime_security(_safe_env()).status == pf.PASS


def test_preflight_flags_unhardened_compose_and_accepts_repo_compose():
    lax = {"services": {"api": {}, "live": {}, "searxng": {}}}
    lax_checks = pf.check_compose(lax)
    hardening = next(c for c in lax_checks if c.name == "container hardening")
    assert hardening.status == pf.FAIL
    assert "read_only" in hardening.detail

    repo_checks = pf.check_compose(_compose())
    repo_hardening = next(
        c for c in repo_checks if c.name == "container hardening")
    assert repo_hardening.status == pf.PASS, repo_hardening.detail


def test_json_wallet_channel_warns_without_logging_key_material(caplog,
                                                               monkeypatch):
    from solders.keypair import Keypair

    material = json.dumps(list(bytes(Keypair().to_bytes())))
    monkeypatch.setattr(live_config, "WALLET_KEYPAIR_PATH", "")
    monkeypatch.setattr(live_config, "WALLET_KEYPAIR_JSON", material)

    with caplog.at_level(logging.WARNING, logger=wallet.__name__):
        keypair = wallet.load_keypair()

    assert keypair is not None
    assert material not in caplog.text
    assert any(
        "WALLET_KEYPAIR_JSON" in record.getMessage()
        and "docker inspect" in record.getMessage()
        for record in caplog.records
    )
