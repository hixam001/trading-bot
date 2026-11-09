"""Bound request buffering before JSON parsing, including chunked bodies."""
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

import config
from api.main import RequestBodyLimitMiddleware


async def _echo(scope, receive, send):
    body = await receive()
    await send({'type': 'http.response.start', 'status': 200, 'headers': []})
    await send({'type': 'http.response.body', 'body': body['body']})


@pytest.mark.asyncio
async def test_default_one_mib_plus_one_rejected():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(
            app=RequestBodyLimitMiddleware(_echo)), base_url='http://test') as client:
        response = await client.post('/api/normal', content=b'x' * (1_048_576 + 1))
    assert response.status_code == 413


@pytest.mark.asyncio
@pytest.mark.parametrize("ensure_ascii", [False, True])
async def test_max_ingest_uses_path_override(monkeypatch, ensure_ascii):
    monkeypatch.setattr(config, 'MAX_INGEST_CHARS', 300_000)
    monkeypatch.setattr(config, 'MAX_REQUEST_BODY_BYTES', 1024)
    content = '\U0001F600' * config.MAX_INGEST_CHARS
    body = json.dumps({'documents': [{'filename': 'large.md', 'content': content}]},
                      ensure_ascii=ensure_ascii).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(
            app=RequestBodyLimitMiddleware(_echo)), base_url='http://test') as client:
        response = await client.post('/api/knowledge-base/ingest', content=body)
        assert response.status_code == 200
        assert response.content == body
        assert (await client.post('/api/normal', content=body)).status_code == 413


@pytest.mark.asyncio
async def test_ingest_chunked_over_limit_rejected(monkeypatch):
    monkeypatch.setattr(config, 'MAX_INGEST_CHARS', 32)
    async def chunks():
        for _ in range(100):
            yield b'x' * 1024
    async with httpx.AsyncClient(transport=httpx.ASGITransport(
            app=RequestBodyLimitMiddleware(_echo)), base_url='http://test') as client:
        assert (await client.post('/api/knowledge-base/ingest', content=chunks())).status_code == 413


def test_global_body_cap_honors_environment(tmp_path):
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'PYTHONPATH': str(root / 'backend'),
           'PYTHON_DOTENV_DISABLED': '1', 'MAX_REQUEST_BODY_BYTES': '2048'}
    proc = subprocess.run([sys.executable, '-c',
        'import config; assert config.MAX_REQUEST_BODY_BYTES == 2048'],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr


def test_preflight_warns_above_four_mib():
    from scripts import preflight_deploy as pf
    assert pf.check_body_buffering({'MAX_REQUEST_BODY_BYTES': '4194305'}).status == pf.WARN
    assert pf.check_body_buffering({'MAX_REQUEST_BODY_BYTES': '4194304'}).status == pf.PASS


def test_preflight_projection_warns_and_passes():
    from scripts import preflight_deploy as pf
    env = {'DATA_BACKEND': 'live', 'MAIN_LLM_PROVIDER': 'deepseek',
        'DEEPSEEK_API_KEY': 'projection-placeholder', 'LLM_DAILY_CALL_CAP': '1'}
    assert pf.check_spend_projection(env).status == pf.WARN
    env.update(LLM_DAILY_CALL_CAP='1000000', LLM_DAILY_TOKEN_CAP='1000000000',
               LLM_DAILY_COST_CAP_USD='1000000', SEARCH_DAILY_CALL_CAP='1000000')
    assert pf.check_spend_projection(env).status == pf.PASS


def test_live_startup_logs_effective_caps_before_opening_wallet(monkeypatch, caplog):
    import logging
    import run_live_cycle as runner
    monkeypatch.setattr(sys, 'argv', ['run_live_cycle.py'])
    monkeypatch.setattr(config, 'DATA_BACKEND', 'mock')
    monkeypatch.setattr(config, 'LLM_DAILY_CALL_CAP', 123)
    monkeypatch.setattr(config, 'LLM_DAILY_TOKEN_CAP', 456)
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit, match='2'):
        runner.main()
    assert any('calls=123' in r.getMessage() and 'tokens=456' in r.getMessage()
               for r in caplog.records)
    assert any('Live spend projection' in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_max_document_reaches_authenticated_ingest(monkeypatch):
    from unittest.mock import AsyncMock
    from api.main import app
    from knowledge_base import loader
    monkeypatch.setattr(config, 'RATE_LIMIT_ENABLED', False)
    monkeypatch.setattr(config, 'MAX_INGEST_CHARS', 200000)
    ingest = AsyncMock(return_value={'filename': 'maximum.md'})
    monkeypatch.setattr(loader, 'ingest_file', ingest)
    content = '\U0001f600' * config.MAX_INGEST_CHARS
    body = json.dumps({'documents': [{'filename': 'maximum.md', 'content': content}]})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url='https://testserver') as client:
        response = await client.post('/api/knowledge-base/ingest', content=body,
            headers={'Content-Type': 'application/json', 'X-Admin-Token': config.ADMIN_TOKEN})
    assert response.status_code == 200, response.text
    assert response.json()['errors'] == []
    ingest.assert_awaited_once_with('maximum.md', content)
