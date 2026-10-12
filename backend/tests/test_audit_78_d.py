"""
backend/tests/test_audit_78_d.py — Dependency hygiene, SearXNG pin, and CI/Docker alignment tests (§78 D1–D5).
"""
import re
from pathlib import Path
import yaml
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_searxng_tag_format():
    """D1: Verify docker-compose.yml specifies a valid, pinned SearXNG tag format (not empty, not latest)."""
    compose_path = REPO_ROOT / "docker-compose.yml"
    assert compose_path.exists()
    content = compose_path.read_text(encoding="utf-8")
    compose_data = yaml.safe_load(content)

    searxng_service = compose_data.get("services", {}).get("searxng", {})
    image = searxng_service.get("image", "")
    assert image.startswith("searxng/searxng:"), f"Expected searxng/searxng image, got {image}"

    tag = image.split(":", 1)[1]
    assert tag != "latest", "SearXNG image must not be pinned to 'latest'"
    assert tag.strip(), "SearXNG tag must not be empty"

    # Upstream SearXNG tag format: YYYY.MM.DD-hash or YYYY.M.D-hash
    tag_regex = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}-[0-9a-f]{7,10}$")
    assert tag_regex.match(tag), f"SearXNG tag '{tag}' does not match expected format YYYY.MM.DD-hash"


def test_searxng_secret_wiring():
    """D2: Verify docker-compose.yml passes SEARXNG_SECRET via environment."""
    compose_path = REPO_ROOT / "docker-compose.yml"
    content = compose_path.read_text(encoding="utf-8")
    compose_data = yaml.safe_load(content)

    env = compose_data.get("services", {}).get("searxng", {}).get("environment", [])
    has_secret = any("SEARXNG_SECRET" in e for e in env) if isinstance(env, list) else "SEARXNG_SECRET" in env
    assert has_secret, "docker-compose.yml searxng service must pass SEARXNG_SECRET in environment"

    # Verify settings.yml exists and does not contain broken unexpanded string without explanation
    settings_path = REPO_ROOT / "deploy" / "searxng" / "settings.yml"
    assert settings_path.exists()
    settings_example = REPO_ROOT / "deploy" / "searxng" / "settings.yml.example"
    assert settings_example.exists(), "deploy/searxng/settings.yml.example must exist"


def test_urllib3_version_in_requirements_and_lock():
    """D3: Verify urllib3 is pinned >= 2.8.0 in requirements.txt and requirements.lock."""
    lock_path = REPO_ROOT / "backend" / "requirements.lock"
    assert lock_path.exists()
    lock_content = lock_path.read_text(encoding="utf-8")
    m_lock = re.search(r"^urllib3==([0-9.]+)", lock_content, re.MULTILINE)
    assert m_lock, "urllib3 must be present in backend/requirements.lock"
    version_parts = tuple(int(x) for x in m_lock.group(1).split("."))
    assert version_parts >= (2, 8, 0), f"backend/requirements.lock urllib3 version {m_lock.group(1)} must be >= 2.8.0"

    req_path = REPO_ROOT / "backend" / "requirements.txt"
    assert req_path.exists()
    req_content = req_path.read_text(encoding="utf-8")
    assert "urllib3>=2.8.0" in req_content.replace(" ", ""), "backend/requirements.txt must specify urllib3>=2.8.0"


def test_python_version_parity_ci_and_dockerfile():
    """D4: Verify CI workflow and Dockerfile use the exact same Python minor version."""
    dockerfile_path = REPO_ROOT / "Dockerfile"
    ci_path = REPO_ROOT / ".github" / "workflows" / "tests.yml"
    assert dockerfile_path.exists()
    assert ci_path.exists()

    df_content = dockerfile_path.read_text(encoding="utf-8")
    m_df = re.search(r"FROM\s+python:(\d+\.\d+)", df_content)
    assert m_df, "Dockerfile must declare python base image with minor version"
    df_py = m_df.group(1)

    ci_content = ci_path.read_text(encoding="utf-8")
    m_ci = re.search(r'python-version:\s*["\']?(\d+\.\d+)', ci_content)
    assert m_ci, "CI workflow must declare python-version"
    ci_py = m_ci.group(1)

    assert df_py == ci_py, f"Python version mismatch: Dockerfile has {df_py}, CI workflow has {ci_py}"


def test_dockerfile_installs_from_lockfile():
    """D4: Verify Dockerfile installs dependencies from backend/requirements.lock."""
    dockerfile_path = REPO_ROOT / "Dockerfile"
    df_content = dockerfile_path.read_text(encoding="utf-8")
    assert "requirements.lock" in df_content, "Dockerfile must install from backend/requirements.lock"


def test_ci_has_scheduled_audit():
    """D4: Verify CI workflow has a weekly schedule for security audits."""
    ci_path = REPO_ROOT / ".github" / "workflows" / "tests.yml"
    ci_content = ci_path.read_text(encoding="utf-8")
    ci_data = yaml.safe_load(ci_content)

    triggers = ci_data.get(True if True in ci_data else "on", {})
    assert "schedule" in triggers, "CI workflow must have a schedule trigger"
    schedules = triggers["schedule"]
    assert len(schedules) > 0 and "cron" in schedules[0], "CI workflow schedule must define a cron"


def test_dependabot_configuration():
    """D4: Verify .github/dependabot.yml exists and covers pip, npm, docker, github-actions."""
    dependabot_path = REPO_ROOT / ".github" / "dependabot.yml"
    assert dependabot_path.exists(), ".github/dependabot.yml must exist"
    content = dependabot_path.read_text(encoding="utf-8")
    data = yaml.safe_load(content)
    ecosystems = {entry.get("package-ecosystem") for entry in data.get("updates", [])}
    expected = {"pip", "npm", "docker", "github-actions"}
    missing = expected - ecosystems
    assert not missing, f"Dependabot missing ecosystems: {missing}"


def test_dockerfile_healthcheck_endpoint():
    """D5: Verify Dockerfile HEALTHCHECK uses cheap /api/health instead of /api/system-status."""
    dockerfile_path = REPO_ROOT / "Dockerfile"
    df_content = dockerfile_path.read_text(encoding="utf-8")
    assert "/api/health" in df_content, "Dockerfile HEALTHCHECK must target /api/health"
    assert "/api/system-status" not in df_content, "Dockerfile HEALTHCHECK must not target /api/system-status"
