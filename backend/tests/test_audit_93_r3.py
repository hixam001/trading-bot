"""Operator credentials must fail closed without disabling the exit scanner."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _python(tmp_path, code, **overrides):
    env = {**os.environ, 'ADMIN_TOKEN': '', 'SESSION_SECRET': '',
           'PYTHONPATH': str(ROOT / 'backend'), 'PYTHON_DOTENV_DISABLED': '1',
           **overrides}
    return subprocess.run([sys.executable, '-c', 'import logging; logging.basicConfig(); ' + code], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize('name', ['ADMIN_TOKEN', 'SESSION_SECRET'])
def test_weak_credentials_do_not_prevent_live_import(tmp_path, name):
    proc = _python(tmp_path, f'import config; import run_live_cycle; '
        f'assert config.{name} == ""; assert config.{name}_INVALID_REASON',
        **{name: 'weak'})
    assert proc.returncode == 0, proc.stderr
    assert 'CRITICAL' in proc.stderr and name in proc.stderr
    assert 'weak' not in proc.stderr


@pytest.mark.parametrize('name', ['ADMIN_TOKEN', 'SESSION_SECRET'])
def test_api_lifespan_refuses_invalid_credentials_before_db(monkeypatch, name):
    import asyncio
    import config
    from api.main import app, lifespan
    monkeypatch.setattr(config, name + '_INVALID_REASON', 'replace with random secret', raising=False)
    async def start():
        with pytest.raises(RuntimeError, match=name):
            async with lifespan(app):
                pytest.fail('invalid API credentials permitted startup')
    asyncio.run(start())


@pytest.mark.parametrize('role,expected', [('api', 78), ('', 78), ('live', 0)])
@pytest.mark.parametrize('name', ['ADMIN_TOKEN', 'SESSION_SECRET'])
def test_entrypoint_rejects_weak_secrets_only_for_api(tmp_path, role, expected, name):
    bin_dir = tmp_path / 'bin'; bin_dir.mkdir()
    for command in ('uvicorn', 'tail'):
        p = bin_dir / command
        p.write_text('#!/bin/sh\nexit 0\n'); p.chmod(0o755)
    (bin_dir / 'python').symlink_to(sys.executable)
    app_dir = tmp_path / 'app'
    (app_dir / 'live_execution').mkdir(parents=True)
    (app_dir / 'live_execution/config.py').write_text('LIVE_TRADING_ENABLED = False\n')
    env = {**os.environ, 'ROLE': role, 'APP_DIR': str(app_dir),
        'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
        'PYTHONPATH': str(ROOT / 'backend'), 'PYTHON_DOTENV_DISABLED': '1',
        'ADMIN_TOKEN': '', 'SESSION_SECRET': '', name: 'weak',
        'WALLET_KEYPAIR_PATH': '', 'WALLET_KEYPAIR_JSON': ''}
    proc = subprocess.run(['bash', str(ROOT / 'backend/docker-entrypoint.sh')],
        env=env, capture_output=True, text=True, timeout=20)
    assert proc.returncode == expected, proc.stderr
    if expected:
        assert name in proc.stderr
        assert 'weak' not in proc.stderr
