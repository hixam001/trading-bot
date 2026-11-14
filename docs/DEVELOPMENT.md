# Development guide

Reviewed **2026-11-14**. Start with the [requirements](REQUIREMENTS.md) and
[application flow](APPFLOW.md). This guide starts the API/dashboard independently
of the live decision process.

## Local prerequisites and installation

Use Python 3.11+ (CI uses 3.14), Node.js 20+ and npm. Native browser/scraper
dependencies may need additional host packages; follow the deployment guide
when exercising those transports.

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock
cd frontend
npm ci
cd ..
```

The lockfile pins Python installation inputs; `backend/requirements.txt` is the
direct dependency manifest. Install test/audit tools separately if absent:

```bash
.venv/bin/python -m pip install pytest pytest-asyncio pip-audit pyflakes
```

Copy `.env.example` to `.env` only when you need a local configuration file;
it stays gitignored. Do not copy production credentials or execution state
into development. A configured `ADMIN_TOKEN` must meet startup entropy checks.

## Run the dashboard without the trading runner

First build the frontend, then start only FastAPI:

```bash
cd frontend
npm run build
cd ..
DATA_BACKEND=mock .venv/bin/python -m uvicorn api.main:app \
  --app-dir backend --host 127.0.0.1 --port 8000
```

Open `http://localhost:8000`. The API serves the built SPA. A fresh database
can have an empty feed/book: serving the API does not generate decision cycles.
It may return unavailable live balances without wallet configuration. Use a
dedicated development checkout/data path if an operator book exists locally.

For frontend hot reload, run `npm run dev -- --host 127.0.0.1` in `frontend/`
with the API running separately. [vite.config.ts](../frontend/vite.config.ts)
contains the local `/api` and `/ws` proxies. Production split origins instead
use [VITE_API_BASE_URL](CONFIGURATION.md#frontend-origin).

`start.sh` is the operational launcher and can start the autonomous live
runner. The committed live flags are armed. Use the API-only command above
for ordinary UI work; `DATA_BACKEND=mock` is a provider selector and does not
disarm execution. Funded drills and flag changes follow the operator guide.

## Repository map

| Path | Responsibility |
|---|---|
| `backend/models.py`, `sizing.py`, `rule_engine/` | Domain records and deterministic money/rule/exit logic. |
| `backend/decision_pipeline.py`, `data_providers/`, `llm/` | Shared staged evidence and model/provider adapters. |
| `backend/run_live_cycle.py`, `live_execution/` | Autonomous orchestration, signer, ledger and recovery. |
| `backend/api/` | FastAPI routes, auth/middleware, SQL repositories and WebSocket broadcaster. |
| `frontend/src/` | React views, hooks, public API URL helpers and response types. |
| `backend/tests/`, `backend/live_execution/tests/`, `frontend/e2e/` | Backend/live hermetic tests and browser scenarios. |
| `migrations/supabase/`, `scripts/`, `deploy/` | Remote schema, diagnostics/recovery and deployment assets. |
| `docs/`, `frontend/DESIGN.md`, `handoff.md` | Contracts, UI design and historical implementation/operational context. |

## Change workflow

1. Read the relevant handlers/modules, callers and existing tests. Check
   `git status` before edits so unrelated work stays out of the change.
2. Keep rules/sizing pure and provider evidence explicit. Preserve unknown
   values, stable mint identities and model degradation provenance.
3. Preserve the shared-brain/live-execution import boundary. API code must
   never load a signing key or gain a trade-execution route.
4. Use repository methods and bound parameters for SQL, and validated locked
   ledger methods for execution state. Never repair history by clearing JSON.
5. For a response/schema/behavior change, update the matching frontend types,
   tests, migrations and reference docs. Check [requirements](REQUIREMENTS.md)
   for affected invariants.
6. Run focused checks first, then the repository verification gates appropriate
   to the change. Record the actual result and any unavailable checks.
7. Inspect `git diff --check` and the staged diff. Stage explicit intended
   paths; keep credentials, generated bundles and runtime files untracked.

See [TEST_PLAN.md](TEST_PLAN.md) for exact test commands and
[18_SECURITY.md](18_SECURITY.md) for credential/security changes. Existing
auth, safety and accounting tests are regression boundaries; update behavior
only with an intentional corresponding contract change.

## Review and documentation maintenance

A review should explain the triggering problem, resulting behavior, relevant
verification and practical limits. Commit using the repository's existing
author identity; documentation does not require an added contributor registry
or attribution trailer. Do not add co-author attribution automatically.

Keep the API contract, data model, application flow, requirements, configuration
and test plan synchronized with affected implementation changes. Historical
audits stay dated; do not turn an older test count or a local result into a
current CI/production claim. Update the [documentation index](README.md) when
adding a guide.
