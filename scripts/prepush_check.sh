#!/usr/bin/env bash
set -euo pipefail

# scripts/prepush_check.sh — Canonical Pre-Push Verification Gate (§81 Phase F-4)
# Runs all verification steps in order and stops at the first failure.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Run stages separately when tests must be network-isolated and only advisory
# tools may access their registries. The default still runs the entire gate.
STAGE="all"
if [[ $# -gt 0 ]]; then
    if [[ $# -ne 2 || "$1" != "--stage" || ! "$2" =~ ^[1-9]$ ]]; then
        echo "Usage: $0 [--stage 1..9]" >&2
        exit 2
    fi
    STAGE="$2"
fi
run_stage() { [[ "$STAGE" == "all" || "$STAGE" == "$1" ]]; }

PYTHON_BIN=".venv/bin/python"
PIP_BIN=".venv/bin/pip"
PIP_AUDIT_BIN=".venv/bin/pip-audit"

if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="python3"
    PIP_BIN="pip"
    PIP_AUDIT_BIN="pip-audit"
fi

if run_stage 1; then
    echo "=== [1/9] Running hermetic pytest suite ==="
    env -u ADMIN_TOKEN "$PYTHON_BIN" -m pytest -q
fi

if run_stage 2; then
    echo "=== [2/9] Checking Python package dependencies (pip check) ==="
    "$PIP_BIN" check
fi

if run_stage 3; then
    echo "=== [3/9] Auditing Python dependencies for vulnerabilities (pip-audit) ==="
    "$PIP_AUDIT_BIN" --disable-pip --no-deps -r backend/requirements.lock
fi

if run_stage 4; then
    echo "=== [4/9] Verifying AST keypair isolation (backend/api/ boundary) ==="
    "$PYTHON_BIN" -m pytest backend/tests/test_audit_77_b.py -k test_api_modules_have_no_load_keypair_references -q
fi

if run_stage 5; then
    echo "=== [5/9] Checking frontend TypeScript types (npx tsc --noEmit) ==="
    (cd frontend && npx tsc --noEmit)
fi

if run_stage 6; then
    echo "=== [6/9] Building frontend production bundle (npm run build) ==="
    (cd frontend && npm run build)
fi

if run_stage 7; then
    echo "=== [7/9] Auditing frontend production dependencies (npm audit --omit=dev) ==="
    (cd frontend && npm audit --omit=dev --ignore-scripts)
fi

if run_stage 8; then
    echo "=== [8/9] Running Playwright E2E integration tests (npm run test:e2e) ==="
    BACKEND_PID=""
    if ! curl -s http://127.0.0.1:8000/api/health | grep -q "ok"; then
        echo "Starting local mock backend on port 8000 for E2E tests..."
        DATA_BACKEND=mock RATE_LIMIT_PAID_PER_MINUTE=1000 RATE_LIMIT_GENERAL_PER_MINUTE=2000 "$PYTHON_BIN" -m uvicorn api.main:app --app-dir backend --host 127.0.0.1 --port 8000 --workers 1 --limit-concurrency 200 --timeout-keep-alive 5 --h11-max-incomplete-event-size 65536 --timeout-graceful-shutdown 20 >/dev/null 2>&1 &
        BACKEND_PID=$!
        for i in $(seq 1 30); do
            if curl -s http://127.0.0.1:8000/api/health | grep -q "ok"; then
                echo "Mock backend ready on port 8000."
                break
            fi
            sleep 0.5
        done
    fi

    (cd frontend && npm run test:e2e)
    if [[ -n "$BACKEND_PID" ]]; then
        kill "$BACKEND_PID" 2>/dev/null || true
        wait "$BACKEND_PID" 2>/dev/null || true
    fi
fi

if run_stage 9; then
    echo "=== [9/9] Scanning for secret leaks (gitleaks) ==="
    if command -v gitleaks >/dev/null 2>&1; then
        gitleaks git --verbose
    else
        echo "NOTICE: gitleaks binary is not installed on this system — skipping local gitleaks scan."
        echo "Gitleaks scan will execute in CI via .github/workflows/tests.yml."
    fi
fi

if [[ "$STAGE" == "all" ]]; then
    echo "=== ALL PRE-PUSH CHECKS PASSED ==="
else
    echo "=== PRE-PUSH STAGE $STAGE PASSED ==="
fi

