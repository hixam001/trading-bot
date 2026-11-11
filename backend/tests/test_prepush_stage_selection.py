"""Canonical gate stage partitioning preserves default order/failure behavior."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.fixture
def gate(tmp_path):
    root = tmp_path / 'repo'
    (root / 'scripts').mkdir(parents=True)
    (root / 'frontend').mkdir()
    source = Path(__file__).resolve().parents[2] / 'scripts' / 'prepush_check.sh'
    script = root / 'scripts' / source.name
    shutil.copyfile(source, script)
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    trace = tmp_path / 'trace'
    for tool in ('python3','pip','pip-audit','npx','npm','curl','gitleaks'):
        path = binaries / tool
        path.write_text('''#!/bin/bash
name="${0##*/}"
printf '%s:%s\\n' "$name" "$*" >>"$TRACE_PATH"
if [[ "$name" == "${FAIL_TOOL:-}" ]]; then exit 27; fi
if [[ "$name" == "curl" ]]; then echo '{"ok":true}'; fi
''')
        path.chmod(0o755)
    def run(*args, fail=None):
        env = {'PATH':str(binaries)+':/usr/bin','TRACE_PATH':str(trace)}
        if fail: env['FAIL_TOOL'] = fail
        result = subprocess.run(['/bin/bash',str(script),*args],env=env,capture_output=True,text=True)
        calls = trace.read_text().splitlines() if trace.exists() else []
        return result,calls
    return run


@pytest.mark.parametrize('stage,tool',[(1,'python3'),(2,'pip'),(3,'pip-audit'),(4,'python3'),(5,'npx'),(6,'npm'),(7,'npm'),(9,'gitleaks')])
def test_selected_stage_executes_only_its_check(gate,stage,tool):
    result,calls = gate('--stage',str(stage))
    assert result.returncode == 0, result.stderr
    assert len(calls)==1 and calls[0].startswith(tool+':')
    assert f'STAGE {stage} PASSED' in result.stdout
    assert 'ALL PRE-PUSH CHECKS PASSED' not in result.stdout
    if stage==3: assert '--disable-pip --no-deps -r backend/requirements.lock' in calls[0]
    if stage==7: assert '--omit=dev --ignore-scripts' in calls[0]


def test_default_gate_keeps_all_checks_in_order(gate):
    result,calls = gate()
    assert result.returncode==0,result.stderr
    assert [call.split(':',1)[0] for call in calls] == ['python3','pip','pip-audit','python3','npx','npm','npm','curl','npm','gitleaks']
    assert 'ALL PRE-PUSH CHECKS PASSED' in result.stdout


@pytest.mark.parametrize('args',[('--stage','0'),('--stage','10'),('--stage','all'),('--stage',),('--stage','1','extra'),('--unknown',)])
def test_invalid_stage_refuses_without_any_check(gate,args):
    result,calls = gate(*args)
    assert result.returncode==2 and calls==[]


def test_failed_stage_stops_default_gate_and_never_reports_success(gate):
    result,calls = gate(fail='pip-audit')
    assert result.returncode==27
    assert [call.split(':',1)[0] for call in calls]==['python3','pip','pip-audit']
    assert 'ALL PRE-PUSH CHECKS PASSED' not in result.stdout
