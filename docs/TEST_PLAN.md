# Test plan

Reviewed **2026-11-14** against [pytest.ini](../pytest.ini), the
[backend fixtures](../backend/conftest.py),
[pre-push gate](../scripts/prepush_check.sh),
[CI workflow](../.github/workflows/tests.yml), and
[Playwright configuration](../frontend/playwright.config.ts).
This defines verification scope; results must come from the run under review.

## Objectives and boundaries

Verify deterministic decision/money behavior, safe signing/accounting,
API authorization and bounded inputs, frontend journeys, and deployment/state
contracts. Preserve the [requirements](REQUIREMENTS.md) and
[data-model](DATA_MODEL.md) invariants.

Hermetic pytest uses temporary state and SQLite. The session fixture redirects
live execution state before imports, isolates shared readers/secret sidecars,
and detects real execution-state changes. A session socket guard blocks
non-loopback networking except tests explicitly marked `network`. Test fixtures
also isolate rate limits, revocations and bot challenge nonces. This is not a
license to run standalone funded scripts as part of automated tests.

Do not set `USE_SUPABASE_DB` to a production book for test execution. The
repository selector forces SQLite under pytest. Real Postgres migrations,
provider billing/availability, native browser support and mainnet outcomes
need distinct environment-specific validation.

## Coverage matrix

Suite names in the table resolve under [backend/tests](../backend/tests/)
unless prefixed with `live/` (under
[live_execution/tests](../backend/live_execution/tests/)).

| Area / requirement | Principal suites | Required outcomes |
|---|---|---|
| Discovery and input identity (FR-01) | `test_discovery_isolation.py`, `test_onchain_security.py`, `test_remediation_provider_numbers.py` | Malformed/non-finite/unknown measurements and ambiguous identity fail safely; providers stay isolated. |
| Rules and staged evidence (FR-02/03) | `test_rules.py`, `test_decision_pipeline.py`, `test_crowd_observation.py`, `test_web_staging.py`, `test_social_staging.py` | All required rules and model verdict agree; skipped checks stay explicit; paid stages obey prerequisites. |
| Money and sizing (FR-04) | `test_money_math.py`, `test_risk_budget.py`, `test_live_ticket_floor.py`, `test_jupiter_decimals.py`, `live/test_fill_actuals.py` | Hand-calculated amounts, cap boundaries, real decimals and actual fill accounting match expectations. |
| Exit management (FR-05) | `test_exit_engine.py`, `test_exit_backoff.py`, `live/test_manage_jump_guard.py` | Numeric exits/trims, cooldowns and missing-mark behavior remain deterministic. |
| Arming/signing (FR-06) | `live/test_safety_model.py`, `test_security_signing_policy.py`, `test_section94_consensus.py`, `test_section95_payer.py` | Source flags pinned; unsafe transactions, wrong payer and inconsistent consumed fields cannot sign. |
| Proof lifecycle (FR-07) | `live/test_executor_memo_flow.py`, `live/test_commit_log.py`, `test_ref_r11_memo_verify.py` | Memo precedes fill; hashes/bindings verify; failures preserve evidence. |
| Uncertain execution and state (FR-08/NFR-04) | `test_pending_buy_resolution.py`, `test_section94_sell_release.py`, `test_section94_sell_settlement.py`, `test_section94_state_validation.py` | Unknown does not authorize retry; proven expiry/recovery and repeated settlement preserve exactly-once accounting. |
| Auth/API (FR-09/NFR-01) | `test_api_routes.py`, `test_route_matrix.py`, `test_authz_surface.py`, `test_cookie_auth.py`, `test_s2_auth_hardening.py` | Every route classified; cookie expiry/revocation, proxy trust and CSRF work; API cannot sign. |
| Limits and rendering (NFR-02/03) | `test_input_validation.py`, `test_mass_assignment.py`, `test_s4_request_limits.py`, `test_s6_rate_limit_buckets.py`, `test_s4_xss_surface.py` | Schema extras/body caps/rate buckets enforce bounds; raw content is escaped. |
| Provider spend/prompts (NFR-02/03) | `test_s5_prompt_injection.py`, `test_s5_spend_caps.py`, `test_spend_projection.py`, `test_section94_http_policy.py` | Untrusted text cannot gain authority; budgets persist; responses/time/redirect policies are bounded. |
| Knowledge/learning (FR-11) | `test_file_upload_security.py`, `test_calibration.py`, `test_thesis_restate.py` | Safe bounded ingestion and lifecycle updates; no executable upload. |
| Dashboard truth (FR-10/12) | `test_zero_as_unknown.py`, `test_dashboard_freshness.py`, `test_mirror_exit_price.py`, `test_funnel_snapshots.py` | Unknown remains unknown; cash/marks/history/funnel semantics reconcile. |
| Leakage/diagnostics (NFR-05/06) | `test_secret_hygiene.py`, `test_redaction_adversarial.py`, `test_public_surface_leaks.py`, `test_section95_counters.py`, `test_section95_evidence.py` | Private data stays private, public identifiers survive redaction, counters report scope/freshness. |
| Deployment/recovery (NFR-07/08) | `test_preflight_deploy.py`, `test_deploy_contracts.py`, `test_s4_backup_v2.py`, `test_s9_monitoring_and_restore.py`, `test_db_surface_parity.py` | Mounts/roles/schema surfaces and encrypted restore obey contracts. |
| Documentation | `test_docs_security.py`, `test_route_matrix.py` | Docs avoid secret-injection examples; canonical security matrix covers registered routes. |

## Local test commands

From the root with dependencies installed:

```bash
# All collected backend and live tests, including session isolation.
env -u ADMIN_TOKEN .venv/bin/python -m pytest -q

# Focused API/documentation contracts.
.venv/bin/python -m pytest backend/tests/test_api_routes.py \
  backend/tests/test_route_matrix.py backend/tests/test_docs_security.py -q

# Live package tests, still hermetic; this does not launch the live runner.
.venv/bin/python -m pytest backend/live_execution/tests/ -q

# Collect current tests without claiming they passed.
.venv/bin/python -m pytest --collect-only -q
```

Run specific relevant suites before expanding. Do not pin a historical pass
count in the plan; collection evolves. Network-marked tests, if any, must be
reviewed before enabling external access. A skipped environment-specific check
is not proof of the property it would test.

## Frontend verification and browser scenarios

```bash
cd frontend
npx tsc --noEmit
npm run build
cd ..
.venv/bin/python scripts/scan_frontend_bundle.py --dist frontend/dist
```

For E2E, first install Chromium in a suitable environment using
`npx playwright install --with-deps chromium` from `frontend/`. Start a dedicated
mock API serving the built frontend (no live runner):

```bash
DATA_BACKEND=mock RATE_LIMIT_PAID_PER_MINUTE=1000 \
  RATE_LIMIT_GENERAL_PER_MINUTE=2000 \
  .venv/bin/python -m uvicorn api.main:app --app-dir backend \
  --host 127.0.0.1 --port 8000
```

Then in another terminal:

```bash
cd frontend
BASE_URL=http://127.0.0.1:8000 npm run test:e2e
```

Playwright does not start its own server. Use a dedicated test checkout and
temporary DB/state paths when an operator book exists; do not aim it at a
running funded service. Elevated rate limits above are for the mock test
process. CI separately removes dotenv files before pytest and starts its own
mock API. Local environment files must be reviewed for test suitability.

| Spec | Journey / evidence |
|---|---|
| `dashboard.spec.ts` | Dashboard shell, views and core data rendering. |
| `operator.spec.ts` | Read-only operator navigation, filters and history. |
| `keyboard.spec.ts` | Focus, shortcuts and escape/navigation behavior. |
| `xss.spec.ts` | Untrusted text cannot execute as browser markup/script. |
| `audit.spec.ts` | UI/runtime audit scenarios encoded in the current spec. |

On failure retain Playwright traces; distinguish missing browser/native packages,
server startup/auth configuration and application assertions.

## Repository gates and CI

`bash scripts/prepush_check.sh` runs nine stages and stops at the first failure.
`--stage N` runs one stage, enabling separation of hermetic checks from
registry-backed advisory commands:

| Stage | Gate |
|---|---|
| 1 | Hermetic pytest (`ADMIN_TOKEN` removed from process environment). |
| 2 | Python dependency conflicts (`pip check`). |
| 3 | Locked-manifest Python advisory audit (`pip-audit --disable-pip --no-deps`). |
| 4 | API keypair-loader isolation AST test. |
| 5 | TypeScript type check. |
| 6 | Frontend production build. |
| 7 | Frontend production dependency audit. |
| 8 | Playwright E2E; starts mock backend only if port 8000 is not already healthy. |
| 9 | Gitleaks history scan when installed. |

Because stage 8 can reuse a healthy existing service, confirm that it is the
intended mock test instance before invoking it. The script's success banner
does not replace inspection of skipped/unavailable tools.

The [CI test workflow](../.github/workflows/tests.yml) also scans the built
frontend, validates Compose with dummy configuration, cross-builds ARM64 under
QEMU and runs real container read-only-root smoke checks for both roles.
[gitleaks.yml](../.github/workflows/gitleaks.yml) owns the separate secret-scan
workflow. Registry advisories, image builds, native browser setup and hosted
integration need their respective network/runtime environments.

## Release and manual acceptance

Before an operational release, require the relevant gates above plus deployment
preflight, copied-state validation, actual browser/host verification and a
scratch restore/reconciliation drill. The keypair-free signing dry run is a
separate provider-network check; devnet/funded execution is an explicit
operator activity. See [OPERATIONS.md](OPERATIONS.md).

Record: commit under test, command, environment, pass/fail/skip counts, failure
reason and remaining checks. Preserve logs without credentials. A local pytest
pass does not establish CI success, a native ARM64 build, migrated Supabase,
mainnet safety or production recovery. Mutation evidence in
[17_MUTATION_LOG.md](17_MUTATION_LOG.md) is dated and scoped to its recorded run.
