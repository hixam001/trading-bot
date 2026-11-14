# Software requirements

Baseline reviewed **2026-11-14**. This records implemented responsibilities,
constraints and verification references; it is not a claim of production
certification or profitable trading. The [feature list](02_FEATURE_LIST.md)
provides feature detail, and [architecture](01_ARCHITECTURE.md) provides design
background. Use the [documentation index](README.md) to find current contracts.

## Purpose, actors and scope

The application researches Solana memecoin candidates, evaluates deterministic
rules alongside structured cloud-model judgment, executes eligible live
operations through a guarded signer, and exposes an auditable dashboard.

| Actor | Supported activity |
|---|---|
| Dashboard reader | Read intentionally public candidate, refusal, thesis and proof surfaces. |
| Operator | Authenticate for private book views, ingest knowledge, perform deliberate maintenance, manage deployment and recovery. |
| Live process | Discover, evaluate, execute, reconcile, manage positions and journal outcomes autonomously within configured controls. |
| External provider | Supply untrusted market, security, crowd, web, model or chain observations. |

This is a single-operator application. Public signup, customer accounts,
payments, brokerage services, a browser trading terminal and a second paper
execution book are outside the implemented product scope. `mock` is a data
provider mode, not a separate paper-trading product or an arming control.

## Functional requirements and traceability

Test references name maintained suites, not fixed test counts. Detailed cases
and execution commands are in [TEST_PLAN.md](TEST_PLAN.md).

| ID | Requirement / acceptance condition | Implementation | Verification reference |
|---|---|---|---|
| FR-01 | Provider selection supports mock data for hermetic development and configured live discovery/enrichment. Malformed provider numbers/identities cannot silently become valid inputs. | [providers](../backend/data_providers/), [bounded HTTP](../backend/bounded_http.py) | `test_discovery_isolation.py`, `test_remediation_provider_numbers.py`, `test_section94_http_policy.py` |
| FR-02 | Candidate entry requires every required deterministic rule and a valid model buy verdict; skipped required checks do not count as passes. | [pipeline](../backend/decision_pipeline.py), [rules](../backend/rule_engine/), [runner](../backend/run_live_cycle.py) | `test_decision_pipeline.py`, `test_rules.py`, `test_crowd_observation.py` |
| FR-03 | Expensive candidate crowd, web and social reads are staged after prerequisite checks; failures/degradation remain observable. | [pipeline](../backend/decision_pipeline.py), [LLM](../backend/llm/) | `test_web_staging.py`, `test_social_staging.py`, `test_s5_spend_caps.py` |
| FR-04 | Code owns sizing, live floors/caps, cash checks and exposure limits; missing decimals or wallet observations cannot authorize a trade. | [sizing](../backend/sizing.py), [live executor](../backend/live_execution/executor.py) | `test_money_math.py`, `test_risk_budget.py`, `test_live_ticket_floor.py`, `test_jupiter_decimals.py` |
| FR-05 | Numeric exits support stops, trailing, liquidity/invalidation/stale exits and trims, with sell-risk gates and independent scanning. | [exits](../backend/rule_engine/exits.py), [runner](../backend/run_live_cycle.py) | `test_exit_engine.py`, `test_exit_backoff.py`, `test_graceful_shutdown.py` |
| FR-06 | Signing requires identity-bound transaction policy and simulation. Human-only source flags control arming; API/model/environment cannot arm the system. | [live config](../backend/live_execution/config.py), [guard](../backend/live_execution/sim_guard.py) | Live `test_safety_model.py`, `test_security_signing_policy.py`, `test_section95_payer.py` |
| FR-07 | Seal eligible operations before execution, confirm the precommit memo, and bind actual fills to their evidence. | [commit log](../backend/live_execution/commit_log.py), [memo](../backend/live_execution/memo.py) | Live `test_executor_memo_flow.py`, `test_commit_log.py`; `test_ref_r11_memo_verify.py` |
| FR-08 | Preserve uncertain signatures, prevent duplicate buys/sells, and settle confirmed accounting once, including already-closed positions. | [ledger](../backend/live_execution/models.py), [executor](../backend/live_execution/executor.py) | `test_section94_sell_settlement.py`, `test_section94_sell_release.py`, `test_pending_buy_resolution.py` |
| FR-09 | Expose bounded decision/history/private-book/status/proof APIs; no HTTP route may place/close trades or modify arming flags. | [API](../backend/api/), [API contract](API_CONTRACT.md) | `test_api_routes.py`, `test_route_matrix.py`, `test_authz_surface.py` |
| FR-10 | Dashboard supports live, holdings, journal, market and system views, history drill-down, keyboard navigation, reconnect and explicit missing/error states. | [frontend](../frontend/src/), [app flow](APPFLOW.md) | Playwright dashboard/operator/keyboard specs; `test_dashboard_freshness.py` |
| FR-11 | Knowledge ingestion accepts bounded safe documents with operator authorization; preserve theses, reflections and learning observations. | [knowledge loader](../backend/knowledge_base/loader.py), [learning](../backend/learning_loop.py), [thesis restatement](../backend/thesis_restate.py) | `test_file_upload_security.py`, `test_thesis_restate.py`, `test_calibration.py` |
| FR-12 | SQL trade history mirrors the live ledger; current cash comes from chain, with unknown observations represented honestly. | [runner](../backend/run_live_cycle.py), [stats](../backend/api/routes/stats.py), [live book](../backend/api/routes/live_book.py) | `test_own_basis.py`, `test_zero_as_unknown.py`, `test_mirror_exit_price.py` |

Backend test filenames above resolve under [backend/tests](../backend/tests/)
unless marked Live, which resolves under
[backend/live_execution/tests](../backend/live_execution/tests/).

## Quality and operational requirements

| ID | Required property | Evidence / practical boundary |
|---|---|---|
| NFR-01 | Authentication, proxy-aware local trust, cookie lifetime/revocation and CSRF enforcement | Auth, cookie, HTTPS and route-matrix suites; documented in [18_SECURITY.md](18_SECURITY.md). |
| NFR-02 | Untrusted inputs, model outputs, uploaded documents and transaction bytes cannot bypass deterministic authority | Input-validation, prompt-injection, mass-assignment, XSS and signing-policy suites. |
| NFR-03 | Bound request/response sizes, timeouts, rate buckets, client counts and paid-provider budgets | Request-limit, rate-limit, bounded-HTTP and spend-cap suites; in-process state is not a distributed limiter. |
| NFR-04 | Execution-state validation, serialized mutation, signature idempotency and persistent volumes protect accounting | State-integrity/validation, close-settlement and read-only-root contract suites. |
| NFR-05 | Secrets are excluded from public bundles, diagnostics and normal backups; public transaction identifiers remain usable | Redaction, public-surface, secret-hygiene and bundle-scan checks. |
| NFR-06 | API health, engine heartbeats, alerts and private consensus counters distinguish service liveness from trading outcomes | Health/status/safety and section95 evidence/counter suites; current VM behavior needs separate observation. |
| NFR-07 | Deployment and restore preserve authority, run validated artifacts, and reconcile before resumed operation | Deployment preflight, encrypted backup/restore, shutdown and container tests. |
| NFR-08 | SQLite default and optional Supabase expose matching repository operations | Surface-parity/RLS tests; hosted Postgres integration remains a separate validation step. |

Default cadences (60-second cycle, 15-second exit scan, dashboard polling)
are implementation settings, not uptime, latency or recovery-time guarantees.
No load-tested concurrency SLO, availability SLA, recovery-point objective or
recovery-time objective is established by this documentation.

## Change acceptance

For a change to one of these requirements:

1. Identify the affected requirement and source of truth before implementation.
2. Update code, callers, types and relevant tests together.
3. Update API/data/flow/configuration docs when their observable contract changes.
4. Run affected checks and required repository gates; record exact results and
   unavailable checks without substituting historical results.
5. Keep live flag changes, funded drills, deployments and accounting repair
   under their explicit operator workflows.

Risk acceptance and unresolved security/deployment items remain in
[18_SECURITY.md](18_SECURITY.md), [11_DEPLOYMENT.md](11_DEPLOYMENT.md) and the
latest relevant [handoff](../handoff.md) sections. Existing tests do not prove
production readiness, strategy profitability or third-party availability.
