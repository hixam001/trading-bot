#!/usr/bin/env python3
"""§93 R1: safely probe runtime write directories, or launch CI containers.

Inside a container, ROLE selects the runtime inventory. Only exclusively
created, randomly named sibling files are written and removed: configured
state, credentials, counters, logs and databases are never opened or replaced.

On a CI host, --compose-ci reads `docker compose config --format json`, creates
throwaway named volumes, and runs the same env/mount/hardening contract in
network-disabled containers. Image directory ownership initialises new named
volumes normally; no root or world-writable permission workaround is used.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

try:
    from scripts import audit_runtime_writes as inventory
except ModuleNotFoundError:
    import audit_runtime_writes as inventory


_DIRECTORY_KEYS = {"LIVE_STATE_DIR", "LIVE_EXECUTION_STATE_DIR", "LOG_DIR",
                   "KB_INGESTED_DIR"}


def _probe_directory(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".rofs-smoke-", dir=directory)
    probe = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("rofs-smoke\n")
        if probe.read_text(encoding="utf-8") != "rofs-smoke\n":
            raise OSError("probe round-trip failed")
    finally:
        # This name was obtained with O_EXCL; never unlink a configured path.
        probe.unlink(missing_ok=True)


def main() -> int:
    role = os.getenv("ROLE", "").strip()
    if role not in {"api", "live"}:
        print("FAIL role: ROLE must be api or live")
        return 2
    paths = inventory.resolved_runtime_paths(dict(os.environ))
    failures = 0
    for key in inventory.runtime_keys(role):
        target = Path(paths[key])
        directory = target if key in _DIRECTORY_KEYS else target.parent
        try:
            _probe_directory(directory)
        except OSError as exc:
            failures += 1
            # Report the config key and errno, never state contents or env values.
            print(f"FAIL {role} {key}: {type(exc).__name__} errno={exc.errno}")
        else:
            print(f"PASS {role} {key}")
    print(f"{role}: {failures} runtime write path failure(s)")
    return 1 if failures else 0


@dataclass(frozen=True)
class ContainerSmokePlan:
    command: tuple[str, ...]
    volumes: tuple[str, ...]


def container_smoke_plan(compose: dict, role: str, image: str, namespace: str,
                         bind_root: Path | None = None) -> ContainerSmokePlan:
    """Build a reviewable Docker command from resolved compose service data."""
    service = compose["services"][role]
    if service.get("read_only") is not True:
        raise ValueError(f"{role}: read_only must remain true")
    command = ["docker", "run", "--rm", "--network", "none", "--read-only",
               "--entrypoint", "python"]
    for capability in service.get("cap_drop") or []:
        command += ["--cap-drop", str(capability)]
    for option in service.get("security_opt") or []:
        command += ["--security-opt", str(option)]
    for key, option in (("pids_limit", "--pids-limit"), ("mem_limit", "--memory"),
                        ("cpus", "--cpus")):
        if service.get(key):
            command += [option, str(service[key])]
    for tmpfs in service.get("tmpfs") or []:
        if isinstance(tmpfs, dict):
            tmpfs = tmpfs["target"]
        command += ["--tmpfs", str(tmpfs)]
    for key, value in inventory.service_environment(service).items():
        command += ["--env", f"{key}={value}"]
    volumes: list[str] = []
    for index, raw in enumerate(service.get("volumes") or []):
        if isinstance(raw, dict):
            source, target = str(raw.get("source", "")), str(raw["target"])
            read_only = bool(raw.get("read_only", False))
            kind = raw.get("type", "volume")
            if kind == "tmpfs":
                command += ["--tmpfs", target]
                continue
        else:
            source, target, read_only = inventory.split_volume(str(raw))
            kind = "bind" if source.startswith(("/", ".", "$")) else "volume"
        if kind == "bind":
            # CI uses empty fixtures instead of mounting an operator wallet or
            # other host data. Preserve the target and read-only property.
            source = str((bind_root or Path("/tmp")) / f"{namespace}-bind-{index}")
        else:
            source = f"{namespace}-{source or f'anonymous-{index}'}"
            volumes.append(source)
        mount = f"type={kind},source={source},target={target}"
        if read_only:
            mount += ",readonly"
        command += ["--mount", mount]
    command += [image, "/app/scripts/rofs_smoke.py"]
    return ContainerSmokePlan(tuple(command), tuple(volumes))


def run_compose_ci(image: str) -> int:
    """Run both roles with unique, disposable volumes and no network access."""
    result = subprocess.run(["docker", "compose", "config", "--format", "json"],
                            check=True, capture_output=True, text=True)
    compose = json.loads(result.stdout)
    namespace = "rofs-" + uuid.uuid4().hex[:12]
    volumes: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="rofs-ci-") as temporary:
        bind_root = Path(temporary)
        plans = [container_smoke_plan(compose, role, image, namespace, bind_root)
                 for role in ("api", "live")]
        # A bind fixture is a directory when the original compose source is a
        # directory, otherwise an empty file. Never read original contents.
        for role in ("api", "live"):
            for index, mount in enumerate(compose["services"][role].get("volumes") or []):
                if mount.get("type") != "bind":
                    continue
                fixture = bind_root / f"{namespace}-bind-{index}"
                if Path(mount["source"]).is_dir():
                    fixture.mkdir(mode=0o700, exist_ok=True)
                else:
                    fixture.touch(mode=0o600, exist_ok=True)
        try:
            for plan in plans:
                for volume in plan.volumes:
                    if volume not in volumes:
                        subprocess.run(["docker", "volume", "create", volume],
                                       check=True, stdout=subprocess.DEVNULL)
                        volumes.add(volume)
                subprocess.run(plan.command, check=True)
        finally:
            if volumes:
                subprocess.run(["docker", "volume", "rm", *sorted(volumes)],
                               check=True, stdout=subprocess.DEVNULL)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-ci", action="store_true")
    parser.add_argument("--image", default="trading-bot:rofs")
    args = parser.parse_args()
    sys.exit(run_compose_ci(args.image) if args.compose_ci else main())
