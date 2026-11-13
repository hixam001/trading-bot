# Security Controls and Route Matrix (§92)

This is the operator-facing security record for the production trading bot.
Every control below is enforced in code, exercised by a named test, and
summarised in the mutations table. It is deliberately written to be read
under pressure: the route matrix first, then the threat-control map, then
the incident/rotation runbooks.

> Scope note: there is no payment surface in this system. Authentication
> protects operator actions and money-adjacent read surfaces; it never
> creates accounts, charges cards, or stores customer PII.

## 0. Threat model

### Assets

1. **Funds and signing authority** — the Solana wallet keypair, the live
   execution ledger, the kill switch, and every signed transaction.
2. **Operator authority** — `ADMIN_TOKEN`, the HttpOnly session cookie,
   `SESSION_SECRET`, and the persisted revocation registry.
3. **Provider credentials** — Jupiter, RPC, Helius, and Birdeye access;
   LLM provider access; web-search access; Telegram and ntfy notification
   tokens; backup-encryption material; secret-store material; and the
   Supabase DSN/service role.
4. **Money-adjacent data** — live positions, cash, executions, theses, and
   proof/commit records. Some of these are intentionally public under
   `LIVE_BOOK_PUBLIC=true`.
5. **Availability and spend** — the API, the live cycle, the exit scanner,
   log/evidence retention, and every paid provider quota.

### Adversaries and trust boundaries

| Adversary / boundary | Capability assumed | Primary controls |
|---|---|---|
| Anonymous internet client | Can call every Public route, spoof headers, open many connections, send large/slow bodies, probe paths | Route matrix auth, request caps, limiter, headers, WebSocket caps, fail-closed 404s |
| Cross-site attacker | Can make a victim browser send a cookie-authenticated request | CSRF custom header + Origin/Referer validation; SameSite cookies |
| Credential guesser / botnet | Can rotate source IPs and replay captured challenges | Per-IP + global auth ceilings, single-use PoW, constant-time compare, concealed signup |
| Malicious upstream data | Controls token names, social posts, web pages, or knowledge documents that reach a model | Untrusted-text sanitiser/fencing; fixed model output schema; deterministic gate/sizing |
| Malicious or compromised dependency / CI action / base image | Can execute in the build or at runtime | Action SHA pinning, image digest pinning, read-only containers, cap drop, gitleaks/bundle scan, preflight |
| Network attacker on the proxy path | Can spoof `X-Forwarded-For` unless the peer is trusted; can inject Host/Origin | `TRUSTED_PROXY_CIDRS`, Origin checks, HTTPS redirect, host validation at the proxy |
| Curious/compromised host user | Can read process environment, mounted files, Docker metadata, logs | File-channel wallet preferred, mode 0600 state files, redaction, backup denylist, operator rotation |
| Accidental operator disclosure | Pastes secrets into logs/chat or commits runtime files | Central redaction, gitleaks, frontend scanner, `.gitignore`, boot entropy checks |
| Backup thief | Steals an archive at rest | scrypt + AES-256-GCM v2 envelope, denylisted key material, integrity-before-restore |
| Restore/operator error | Restores stale or partial state and arms it | Multi-pass restore, SQLite integrity check, pre-restore snapshot, forced kill-switch latch, reconcile-before-arm runbook |

### Security objectives

- No unauthenticated request can mutate state, spend money, or read the
  private book.
- No secret value or keypair appears in a log, error body, alert payload,
  prompt, transcript, backup, or built frontend.
- Funds move only through the deterministic gate, sizing, signing, and
  simulation path; the LLM can return a verdict but cannot set size, mint,
  stops, caps, slippage, or the kill switch.
- A spent or unavailable LLM/search budget stops **entries** and never stops
  exits.
- Paid-provider spend is bounded in-process and at the provider dashboard.
- Logs and security events survive a flood without unbounded memory growth.
- Backups are confidential, tamper-evident, restorable, and never include the
  wallet keypair; a restore starts disarmed only after reconcile.
- Deployed artifacts are pinned and a deployment that loses a control fails
  preflight.

### Assumptions

- One API worker per state directory. Multi-replica deployments must add a
  shared limiter/revocation/spend store or an edge control.
- `TRUSTED_PROXY_CIDRS` contains only the real proxy/LB addresses.
- The host OS, Docker daemon, firewall, and Tailscale ACLs are administered
  separately and reviewed before launch.
- Provider dashboards enforce the billing caps described in §4 item 10; the
  application guard is the first line, not the only line.
- The operator keeps `.env`, wallet material, and backups off the repository
  and off chat/log surfaces.

## 1. Route matrix

Every HTTP route, WebSocket, and static mount registered by `api.main.app`
must appear exactly once here. `backend/tests/test_route_matrix.py` fails if a
route is added without a row.

Auth legend:

- **Public** — unauthenticated by design; response is safe for a public
  dashboard/proof surface.
- **Loopback/Admin** — direct loopback (`127.0.0.1`) or `X-Admin-Token` /
  valid session cookie; forwarded requests lose the loopback shortcut.
- **Operator** — `X-Admin-Token` or valid HttpOnly session cookie.
- **Cookie+CSRF** — session cookie; unsafe methods also require
  `X-Requested-With: XMLHttpRequest` and, when present, a same/allowed
  `Origin`/`Referer`.
- **Concealed** — returns 404 by default (`ALLOW_SIGNUP=true` removes the
  concealment but registration remains unsupported).

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | /api/feed | Public | Paginated feed events. |
| GET | /api/mint/{mint}/history | Public | Per-mint history; mint format validated. |
| GET | /api/events.json | Public | Public event proof surface. |
| GET | /api/funnel | Public | Funnel counts. |
| GET | /api/funnel/snapshots | Public | Funnel snapshots. |
| GET | /api/holdings | Loopback/Admin | Real wallet positions/cash. |
| GET | /api/journal | Public | Closed trades. |
| GET | /api/stats | Loopback/Admin | Book statistics and wallet cash. |
| GET | /api/market-regime | Public | Regime snapshot. |
| GET | /api/promotion-gate | Public | Promotion-gate evidence. |
| GET | /api/knowledge-base | Public | Static knowledge and document index (no bodies). |
| POST | /api/knowledge-base/ingest | Operator | Mutating; strict schema, extension and size checks. |
| GET | /api/system-status | Public | Health/status summary. |
| GET | /api/safety | Loopback/Admin | Safety state; public only with `LIVE_BOOK_PUBLIC=true`. |
| GET | /api/proof.json | Public | Decision proof record. |
| GET | /api/exits.json | Public | Exit thresholds and bound sells. |
| GET | /api/verify.json | Public | Recomputed commit hashes. |
| GET | /api/binding.json | Public | Commit-to-chain binding; no wallet address in detail. |
| GET | /api/refusals.json | Public | Gate/model refusals. |
| GET | /api/theses.json | Public | Durable thesis book. |
| GET | /api/disclosure.json | Public | Kill-switch/break transparency. |
| GET | /api/reasoning.json | Public | Decision reasoning record. |
| POST | /api/admin/reset | Operator | Destructive book reset; token + `confirm=yes`; audit logged. |
| GET | /api/admin/monitoring/security-events | Operator | Security event ring buffer. |
| GET | /api/admin/monitoring/logs | Operator | Log file status. |
| GET | /api/auth/challenge | Public | Single-use signed PoW challenge. |
| POST | /api/auth/login | Public | Rate-limited; bot check; constant-time token compare. |
| POST | /api/auth/signup | Concealed | 404 unless `ALLOW_SIGNUP=true`; never creates accounts. |
| POST | /api/auth/logout | Cookie+CSRF | Revokes signature and clears cookie. Explicit no-cookie exemption: idempotent 200; a cookie-bearing mutation still requires CSRF. |
| GET | /api/auth/session | Public | Reports auth state; never returns the token. |
| GET | /api/live/portfolio | Loopback/Admin | Live book; public only with `LIVE_BOOK_PUBLIC=true`. |
| GET | /api/live/executions | Loopback/Admin | Live ledger; public only with `LIVE_BOOK_PUBLIC=true`. |
| GET | /api/health | Public | Container health probe. |
| WS | /ws/feed | Public | Origin allowlist, per-IP and global connection caps, 30s liveness. |
| GET | / | Public | SPA shell or JSON banner. |
| MOUNT | /assets | Public | Built static assets only. |
| GET | /api/{full_path:path} | Public | Loud JSON 404 for unknown API paths. |
| GET | /api | Public | Loud JSON 404 for bare API prefix. |
| GET | /{full_path:path} | Public | SPA fallback; dotfile/sourcemap/secret-extension probes blocked. |

## 1b. Runtime write paths

Every write the running services perform has a home on a writable mount. The
table is generated from `scripts/audit_runtime_writes.py`, which AST-scans
`backend/` and `scripts/` for write-shaped calls and resolves each path's
default from `config.py`. The compose contract test
(`backend/tests/test_audit_93_r1.py`) re-parses `docker-compose.yml` and
fails when a new write path resolves to the read-only root.

| Path (default) | Config key | Service(s) | Mounted volume |
|---|---|---|---|
| `<backend>/blocklist_state.json` | `BLOCKLIST_STATE_FILE` | live | engine_state:/app/backend/live_execution/state |
| `<repo>/.fomo_privy.json` | `FOMO_PRIVY_STATE_FILE` | live | engine_state:/app/backend/live_execution/state |
| `<repo>/.secret_store.key` | `SECRET_STORE_KEY_FILE` | live | engine_state:/app/backend/live_execution/state |
| `<backend>/.supabase_fp.txt` | `SUPABASE_PIN_FILE` | api, live | api engine_data:/app/data; live engine_state:/app/backend/live_execution/state |
| `<repo>/state/session_revocations.json` | `SESSION_REVOCATION_FILE` | api | engine_data:/app/data |
| `<backend>/knowledge_base/ingested/` | `KB_INGESTED_DIR` | api | engine_data:/app/data |
| `<LIVE_STATE_DIR>/spend_guard.json` | `SPEND_GUARD_FILE` | api, live | api engine_data:/app/data/spend_guard_api.json; live engine_state |
| `<LIVE_STATE_DIR>/{break_state,kill_switch,heartbeat,commits,executions,confirmations,safety_counters,crowd_chain_status,wallet_pubkey}.*` | `LIVE_STATE_DIR` | live | engine_state:/app/backend/live_execution/state |
| `<LIVE_STATE_DIR>/consensus_counters.json (atomic .tmp sibling)` | `LIVE_STATE_DIR` | live | engine_state:/app/backend/live_execution/state (api read-only) |
| `<DB_PATH>/trading_bot.db` | `DB_PATH` | api, live | engine_data:/app/data |
| `<LOG_DIR>/{app,error,security}.log` | `LOG_DIR` | api, live | engine_logs:/app/logs |
| `<BACKUP_DIR>/state-*.tar.gz.enc` | `BACKUP_DIR (CLI/env)` | operator | engine_backups:/app/backups |
| `<LIVE_STATE_DIR>/resolve_pending_buy_audit.log` | `LIVE_STATE_DIR (operator CLI)` | operator | engine_state (or operator-specified --audit-log) |
| `<LIVE_STATE_DIR>/safety_counters.json (ack script)` | `LIVE_STATE_DIR (operator CLI)` | operator | engine_state (or operator-specified state dir) |
| `<backend>/live_execution/dex_labels.json (dev refresh)` | `fixed repo path (operator/dev script)` | operator | image root (dev only; not a compose service) |
| `repo files mutated by the mutation harness (dev/CI)` | `repo root (dev/CI tool)` | dev | repository checkout |
| `<LIVE_STATE_DIR>/break_state.json` | `BREAK_STATE_FILE` | live | engine_state |
| `<LIVE_STATE_DIR>/kill_switch.json` | `KILL_SWITCH_FILE` | live | engine_state |
| `<backend>/live_execution/state` | `LIVE_EXECUTION_STATE_DIR` | live | engine_state |
| `<LOG_DIR>/app.log` | `LOG_FILE` | api, live | engine_logs |
| `<LOG_DIR>/error.log` | `ERROR_LOG_FILE` | api, live | engine_logs |
| `<LOG_DIR>/security.log` | `SECURITY_LOG_FILE` | api, live | engine_logs |
| `unique .rofs-smoke-* siblings of configured runtime paths` | `runtime inventory (smoke only)` | operator, dev | same mounts as the selected compose service |

The API service keeps a **separate** spend-guard counter
(`/app/data/spend_guard_api.json`) from the live runner's
(`/app/backend/live_execution/state/spend_guard.json`): API-side LLM calls
(knowledge-base digests) get an independent capped budget and cannot consume the
live runner's entry budget. Both counters roll over at UTC midnight and fail
closed when unwritable.

## 2. Control ↔ test map

| Phase | Control | Enforced by | Proof |
|---|---|---|---|
| S1 | Central redaction of config/runtime secrets, keypairs, PEM, Bearer, credentialed URLs, provider tokens | `backend/redaction.py`; logging filter, 4xx handler, alerts | `test_redaction_adversarial.py`, `test_error_handling_security.py`, `test_logging_and_monitoring.py` |
| S1 | No secret material tracked/ignored; built frontend scanned before deploy | `.gitignore`, `scripts/scan_frontend_bundle.py`, CI step | `test_secret_hygiene.py`, gitleaks history/tracked-tree scans |
| S2 | Route inventory cannot drift | this matrix | `test_route_matrix.py` |
| S2 | CSRF on cookie-auth mutations | `csrf_protect_middleware` | `test_s2_auth_hardening.py` |
| S2 | Logout revocation survives restart | `SESSION_REVOCATION_FILE` registry | `test_s2_auth_hardening.py` |
| S2 | Distributed brute force bounded | global failure window + per-IP window | `test_s2_auth_hardening.py` |
| S2 | PoW challenge single-use | `_USED_POW_NONCES` | `test_s2_auth_hardening.py` |
| S2 | Signup concealed by default | `ALLOW_SIGNUP=false` → 404 | `test_s2_auth_hardening.py` |
| S2 | Request schemas reject unknown fields | Pydantic `extra="forbid"` | `test_mass_assignment.py` |
| S2 / R3 | Invalid operator secrets disable auth; API refuses boot; live imports continue | `config.validate_api_credentials`, API lifespan and entrypoint | `test_s2_auth_hardening.py`, `test_audit_93_r3.py` |
| S2 | Session signing key separable from operator token | `SESSION_SECRET` | `test_s2_auth_hardening.py` |
| S3 | Public fail-soft reasons are fixed strings, never exception text | `disclosure.py`, `live_book.py` | `test_public_surface_leaks.py` |
| S3 | Binding checks withhold the configured wallet and fee payer | `proof.py` `_verify_binding_checks` | `test_public_surface_leaks.py` |
| S3 | Transaction/memo error objects are not stringified publicly | `proof.py` `_verify_memo` | `test_public_surface_leaks.py` |
| S3 | Every documented Public GET is scanned for secrets, keys, tracebacks, paths | route matrix + real ASGI app | `test_public_surface_leaks.py` |
| S4 | Interpolated SQL identifiers come only from a guarded module constant | `db._assert_allowed_reset_table`, `db_pg._assert_allowed_reset_table` | `test_s4_sql_allowlist.py` |
| S4 | PostgREST `anon`/`authenticated` have no table/sequence rights; every public table has RLS | `migrations/supabase/007_revoke_anon_authenticated.sql` | `test_s4_migrations_rls.py` |
| S4 | Request bodies are capped before parsing, including chunked bodies | `RequestBodyLimitMiddleware` | `test_s4_request_limits.py` |
| S4 | Backups use a random-salt scrypt KDF + AES-256-GCM; legacy archives still restore | `scripts/backup_crypto.py`, `backup_state.py`, `restore_state.py` | `test_s4_backup_v2.py`, `test_audit_79_e.py`, `test_audit_81_e.py` |
| S4 | Untrusted token/social text is escaped; no raw-HTML/eval sinks; no multipart uploads | React text rendering + static guard | `test_s4_xss_surface.py`, `frontend/e2e/xss.spec.ts` |
| S5 | Every third-party field sent to a model is normalised, bounded, and fenced as data | `backend/llm/untrusted.py`; thinker/brain/social/KB/web callers | `test_s5_prompt_injection.py` |
| S5 | Injection cannot change size, mint, stops, caps, slippage, or the kill switch | fixed model schema + deterministic gate/sizing | `test_s5_prompt_injection.py` |
| S5 | Prompts and stored transcripts contain no API keys, wallet address, or full config; usage table stores metadata only | prompt builders + `llm_call_usage` schema | `test_s5_prompt_injection.py` |
| S5 | LLM call attempts are reserved before requests; token/cost stop thresholds use post-response accounting (overshoot is possible), persisted and fail-closed | `backend/llm/spend_guard.py`, `llm/client.py` | `test_s5_spend_caps.py` |
| S5 | Entry-side web search has a hard daily call ceiling | `spend_guard.reserve_search`, `web_research.search_web` | `test_s5_spend_caps.py` |
| S5 | A spent model budget pauses entries but never exits; exits do not call the LLM | price-only exit scanner; post-close reflection is fire-and-forget | `test_s5_spend_caps.py` |
| S5 | One deduped `SPEND_CAP` alert per provider/day | `spend_guard._alert`, `AlertEvent.SPEND_CAP` | `test_s5_spend_caps.py` |
| S6 | Requests are bucketed by `(tier, client IP)` with separate auth/paid/general ceilings | `backend/api/rate_limiter.py` | `test_s6_rate_limit_buckets.py`, existing limiter integration tests |
| S6 | Every 429 carries a positive `Retry-After`, coherent reset epoch, and remaining count | `RateLimitMiddleware` | `test_s6_rate_limit_buckets.py` |
| S6 | Limiter key memory is hard-bounded; expired buckets are swept and new clients are counted at capacity | `SlidingWindowRateLimiter` LRU + expiry sweep | `test_s6_rate_limit_buckets.py` |
| S6 | Client IP is taken from `X-Forwarded-For` only when the peer is a configured trusted proxy | `get_client_ip`, `TRUSTED_PROXY_CIDRS` | `test_s6_rate_limit_buckets.py`, `test_security_audit_remediations.py` |
| S6 | Every launch site pins `--workers 1` and bounds concurrency, keep-alive, incomplete headers, and graceful shutdown | `docker-entrypoint.sh`, `start.sh`, `prepush_check.sh` | `test_s6_deploy_limits.py` |
| S6 | Public DB-scan endpoints declare finite `limit`/`offset` ceilings; over-max is rejected before the handler | feed/funnel route `Query(..., le=...)` | `test_s6_deploy_limits.py` |
| S6 | Operator billing caps are set at every paid provider, so a missed app cap still cannot become an unbounded bill | provider dashboards (Groq/OpenRouter, Birdeye, Helius/RPC, Oracle budget, Telegram/ntfy) | Operator checklist §4, item 10 |
| S7 | Security headers are present on 404/405/413/429/500, static, and SPA responses, not just 200s | `backend/api/security_headers.py`; middleware + error handlers | `test_s7_response_hardening.py` |
| S7 | CSP forbids `<base>` hijacking and framing; `nosniff`, `DENY`, `no-referrer`, COOP, and Permissions-Policy are always set | `security_headers.content_security_policy` | `test_s7_response_hardening.py` |
| S7 | Synthetic test hostnames (`test`, `testserver`, `testclient`, `app`) are trusted only while pytest is running | `config.TESTING`, `_is_loopback_host` | `test_s7_response_hardening.py` |
| S7 | DEBUG mode does not bypass operator auth, CSRF, or rate limits, and debug exception text is redacted | auth/CSRF/limiter guards + `redact_text` in error handler | `test_s7_response_hardening.py` |
| S8 | Every GitHub Action is pinned to a full 40-character commit SHA | `.github/workflows/*.yml` | `test_s8_supply_chain.py` |
| S8 | Docker base images and the SearXNG sidecar are pinned by registry digest | `Dockerfile`, `docker-compose.yml` | `test_s8_supply_chain.py` |
| S8 | App containers use a read-only root, `cap_drop: ALL`, `no-new-privileges`, pid/memory/CPU limits; API binds loopback and SearXNG publishes no host port | `docker-compose.yml` | `test_s8_supply_chain.py` |
| S8 | Deploy preflight rejects DEBUG, disabled rate limits/HTTPS, wildcard proxy trust, a zero body cap, and unhardened compose services | `scripts/preflight_deploy.py` | `test_s8_supply_chain.py`, `test_preflight_deploy.py` |
| S8 | Selecting the JSON wallet channel logs a warning that env secrets are visible to `docker inspect`; the mounted file channel remains preferred | `live_execution/wallet.py`, `docs/11_DEPLOYMENT.md` | `test_s8_supply_chain.py` |
| S9 | Every `SecurityEventType` has a production emitter; a new enum member without one fails the test | `backend/api/*` + monitoring schema | `test_s9_monitoring_and_restore.py::test_every_security_event_type_has_a_production_emitter` |
| S9 | Schema-rejected knowledge-base uploads still produce `SUSPICIOUS_UPLOAD` evidence | `backend/api/error_handler.py` validation handler | `test_s9_monitoring_and_restore.py::test_schema_rejected_upload_records_suspicious_upload` |
| S9 | Repeated identical events collapse into one bounded event with `occurrences` and `last_seen`; the dedupe map has a hard LRU cap | `SecurityMonitor.record_event`, `MAX_DEDUPE_KEYS` | `test_s9_monitoring_and_restore.py::test_repeated_events_collapse_but_keep_the_true_count`, `test_dedupe_map_is_hard_bounded` |
| S9 | A login-failure burst alerts once per IP/window, not once per failed request; suppression state is bounded and cleared by `clear()` | `SecurityMonitor._record_burst_locked`, `MAX_BURST_KEYS` | `test_s9_monitoring_and_restore.py::test_login_burst_alert_fires_once_per_ip_per_window` |
| S9 | A backup restore drill verifies SQLite rows, excludes the wallet keypair, and forcibly engages the kill switch with an operator reason | `scripts/restore_state.py` Pass 4 safety latch | `test_s9_monitoring_and_restore.py::test_backup_restore_drill_verifies_book_and_trips_kill_switch` |

Phases S1–S9 are complete; this table is the authoritative control↔test map,
not a marketing summary.

| R1 | Every writer classified; composed paths respect mounts, nested read-only mounts and independent overrides; smoke preserves state | `audit_runtime_writes.py`, preflight, compose and CI smoke | `test_audit_93_r1.py` |
| R2 | State failures ERROR-log and enqueue hourly alerts/security events; TLS exact pin enforced during each handshake before DB auth | `alerts.py`, blocklist/auth/Privy, `api/db_pg.py` | `test_audit_93_r2.py` |
| R3 | API-only weak-secret boot gate; live exit scanner can import | `config.py`, API lifespan, entrypoint | `test_audit_93_r3.py` |
| R4 | Registered public signatures survive shape redaction; configured/runtime secrets always win | `public_registry.py`, redactor, signing/send/confirmation/pending receipts | `test_audit_93_r4.py`, unchanged `test_redaction_adversarial.py` |
| R5 | Sanitised ticker keys shared by prompt/parse/lookup; duplicate/placeholder keys excluded before bounding | `llm/llm_brain.py` | `test_audit_93_r5.py` |
| R6 | 1 MiB global pre-auth buffer; ingest accounts for UTF-8/JSON escapes; spend projection warns and logs without cap increases | ASGI body middleware, preflight, `spend_projection.py`, runner startup | `test_audit_93_r6.py`, `test_spend_projection.py` |
| R7 | All synthetic peers pytest-only; disabled signup concealed before validation; real credential-free route probes | shared host helper, signup route, route matrix tests | `test_audit_93_r7.py`, `test_route_matrix.py` |

## 3. Residual risks

- §93: API/live spend counters are independent, not a shared global budget.
  Both apply the configured caps; set smaller per-service API caps if desired.
  The projection covers live demand, excludes API digests, and uses stated
  scenarios and the repository pricing snapshot. Token/cost estimates are not
  mathematical upper bounds or measurements of actual provider billing.
- §93: an unwritable TOFU pin is held only for the process lifetime. Restart
  loses that pin unless an operator fingerprint or persisted file exists.
  Every pinned connection checks the actual certificate during its TLS
  handshake before PostgreSQL authentication; the pool init hook checks it
  again. Any probe failure after CA verification fails prevents pool creation;
  there is no encrypted-but-unauthenticated fallback. TOFU still trusts the
  first successfully probed certificate; prefer an independently verified
  operator pin to protect bootstrap. Real Supabase integration is unverified.
- §93: the bounded public-signature registry is process-local (5,000 LRU entries).
  Evicted/unregistered signatures are conservatively masked; callers must
  register persisted signatures when receiving them again. Configured and
  runtime secrets are replaced before public exemptions.
- §93: normal request bodies default to 1,048,576 bytes. Only KB ingest uses
  `12 * MAX_INGEST_CHARS + 65,536` bytes: four UTF-8 bytes plus up to eight
  extra JSON-escape bytes per character, with fixed metadata overhead. At
  defaults this is 2,465,536 bytes, still bounded before auth. Batches must fit
  this aggregate request ceiling; split larger batches into separate requests.


- S1: redaction is heuristic for unknown token shapes. Configured/runtime
  values and the provider shapes used by this repo are covered; a brand-new
  provider format needs a new pattern.
- S1: local `logs/` is ignored and mode-restricted, but an operator who
  copies logs off-host must treat them as potentially sensitive.
- S2: `SESSION_SECRET` is optional. Without it, session signing derives from
  `ADMIN_TOKEN`, so rotating the operator token logs every browser out.
- S2: revocation persistence is per-host. A multi-host API deployment needs
  a shared revocation store (or rotate the signing secret) for cluster-wide
  logout.
- S3: public failure reasons are intentionally fixed strings. Operators must
  read the server log (where the exception is logged with redaction) to learn
  whether a public feed failed because of a corrupt file, RPC outage, or
  permissions. The public body only tells a consumer "fail closed".
- S3: `LIVE_BOOK_PUBLIC=true` still publishes live wallet balances, positions,
  and executions by product design. Only failure text and the binding report's
  raw key echo were trimmed; enabling that flag remains a deliberate privacy
  trade-off.
- S3: the public-route scanner uses sentinel values configured at runtime. A
  future route that leaks a secret not present in `config` at scan time (for
  example a per-request credential) needs a route-specific test.
- S4: the PostgREST lock-down is SQL that must actually be applied to the
  hosted Supabase project. The repository test proves the migration is
  present and complete; it cannot prove the dashboard ran it. Verify with the
  queries at the bottom of migration 007.
- S4: the body cap is enforced at the ASGI app. A reverse proxy should also
  enforce `client_max_body_size`; the app cap remains the backstop.
- S4: scrypt parameters are fixed at n=2^15, r=8, p=1. Raising them requires a
  new archive version or every existing archive becomes undecryptable.
- S4: XSS is prevented by React text rendering plus the static sink test.
  Third-party components added later must keep the no-raw-HTML rule.
- S5: prompt sanitisation and fencing reduce injection surface but are not a
  mathematical guarantee; the real containment is structural — the model can
  only return a verdict, and size, mint, stops, caps, slippage, and the kill
  switch are fixed by deterministic code. A future feature that lets model
  output set any of those fields must add a new control and test.
- S5: RPC and Birdeye are intentionally NOT under the daily spend guard in this
  phase. Exit pricing and route/liquidity checks use them, so a tripped cap
  could delay a stop-loss. Their hard ceilings are provider-side quotas plus
  operator alerting; document and review them at the provider. If a future
  design splits read-only entry RPC from exit RPC, add a separate entry cap.
- S5: each spend counter requires one writer process. Atomic replacement keeps
  file contents whole but does not serialize cross-process read-modify-write;
  replicas sharing a counter can lose increments. API and live counters remain
  intentionally separate scopes. Do not combine them or add writers without
  transactional reservations; there is no combined multi-service hard maximum.
- S5: token and estimated-cost values are post-response usage stop thresholds.
  Concurrent in-flight responses can overshoot them, and missing usage or billed
  failures can undercount them. Calls are reserved before sending; unknown
  tokenization/pricing is not converted into a claimed conservative cost bound.
  Zero caps still deny requests; provider billing limits remain the backstop.
- S5: cost is estimated from the provider's advertised token prices. A
  provider-side price change or a missing `usage` block makes the estimate
  stale; the call/token caps and provider dashboard remain the backstops.
- S5: the brain prompt intentionally includes book context (cash, deployed
  size, open symbols, thesis text) so it can reason about the book. It never
  includes the wallet address, private key, API keys, or a full config dump.
  Keep it that way when adding fields to `build_wallet_block`.
- S6: rate-limit counters are per process. `--workers 1` is asserted at every
  launch site for exactly this reason; horizontal scaling (another container,
  not another worker) needs a shared counter or an edge/WAF limit, otherwise
  each replica grants the full quota.
- S6: the limiter is IP-based. An attacker with many real addresses (or a
  botnet) can spread requests and evict honest clients from the bounded LRU,
  resetting those clients' budgets. The LRU bound protects memory, not
  fairness; put a provider/edge rate limit in front for internet-facing
  deployments.
- S6: `X-Forwarded-For` is trusted only from `TRUSTED_PROXY_CIDRS`. Keep that
  list to the actual proxy/LB addresses. If it is widened (for example to a
  whole VCN), direct clients can spoof their bucket identity.
- S6: a process restart clears in-memory limiter and lockout state. Durable
  controls (session revocations, spend guard) survive; brute-force throttling
  starts a new window. Provider-side auth controls remain the backstop.
- S6: `/api/funnel` still allows a window of 10,000 rows. It is finite and
  rate-limited, but it is the largest public scan in the app; put the API
  behind a cache/CDN if it is exposed to heavy anonymous traffic.
- S6: `--limit-concurrency`, request-body, and header limits apply inside the
  app/uvicorn. A reverse proxy must also set connection/body/header limits;
  the app limits are the backstop, not the only layer.
- S6: billing caps are operator-configured at the provider. The app cannot
  prove a dashboard budget was entered; verify each provider before launch.
- S7: the CSP still permits `style-src 'unsafe-inline'` because the dashboard
  uses inline style attributes. Moving to nonces/hashes is a follow-up; it is
  not a script-execution hole because `script-src` stays `'self'`.
- S7: the 500 response is generated by Starlette's outermost
  `ServerErrorMiddleware`, outside the normal header middleware. The shared
  `apply_security_headers` helper is called from the error handler, so a
  future custom 500 path must keep using that helper.
- S7: debug exception text is redacted, but `DEBUG=true` still returns the
  exception type/message and enables `/docs`. Keep `DEBUG=false` in every
  deployed environment; the S7 tests prove debug does not weaken auth, CSRF,
  or rate limits, not that debug is safe to expose publicly.
- S7: `config.TESTING` is derived from `"pytest" in sys.modules`, not from an
  environment variable, so a deployed process cannot opt into test-host trust.
  A future test runner that does not import pytest must set the flag through a
  fixture, not production config.
- S7: the HTTPS redirect still uses the request Host header to build the
  target URL. A forged Host from a non-loopback peer produces a redirect to
  that host; deploy behind a proxy that validates Host (or add an explicit
  host allowlist) if host-header poisoning is in scope.
- S8: digest pinning stops tag drift but does not deliver base-image security
  updates. Bumping a digest is an explicit operator/CI change; review the
  upstream diff and re-run the build, compose validation, and preflight.
- S8: action SHA pinning removes movable-tag risk, but the pinned commit is
  still trusted upstream code. Review the action diff when bumping a SHA.
- S8: `read_only` relies on the app writing only to its mounted volumes and
  `/tmp`. A future dependency that writes elsewhere (for example a new browser
  cache) needs a tmpfs/volume and a test update. A write outside those mounts
  raises EROFS; code that catches it can keep running with degraded features.
  The §93 compose contract in `test_audit_93_r1.py` and
  `scripts/rofs_smoke.py` check the shipped write paths; they cannot guarantee
  that every future dependency handles a failed write safely.
- S8: `pids_limit`, `mem_limit`, and `cpus` are coarse host protections, not a
  guarantee that the process stays within its own heap or provider quota.
  Keep the application and provider alerts as the backstop.
- S8: preflight inspects the declared `.env` and `docker compose config`, not
  the live container (`docker inspect`) or provider dashboards. Re-run it
  after every `.env`/compose change and verify provider budgets separately.
- S8: the JSON wallet channel warning is advisory. An operator who accepts the
  `docker inspect` exposure can still use it; the mounted file channel is the
  documented safe default.
- S9: the security event ring buffer and dedupe index are in memory. A restart
  loses the operator-visible recent events, and the compiled summary counters
  reset. `logs/security.log` survives rotation (10 MB × 5), but off-host
  retention/SIEM shipping is an operator responsibility.
- S9: dedupe collapses identical events inside a 10-second window. The event
  keeps the true total in `occurrences`, the first `timestamp`, and the latest
  `last_seen`; it does not retain every individual timestamp. If per-request
  timing is required for an investigation, read the raw `security.log` before
  it rotates, or add an external log sink.
- S9: the burst alert is deduped per IP for 10 minutes and the suppression map
  is bounded at 2,000 IPs. An attacker with many real addresses can still cause
  one alert per address; the alert subsystem's global flood cap is the
  backstop, not a replacement for provider/edge rate limiting.
- S9: event detail strings are scrubbed through `sanitize_error_detail`, but
  they are still operator-supplied/request-derived. Keep them fixed strings
  where possible; a future emitter that interpolates raw user input must be
  tested against the S1 redaction contract.
- S9: the restore drill uses a synthetic SQLite book and throwaway directory.
  It proves the code path, the archive denylist, and the kill-switch latch; it
  does not prove a specific production archive is the right recovery point.
  Reconcile against the chain before arming after any real restore.
- S9: the forced kill-switch latch is a state file. A process/operator with
  write access can clear it without reconcile. The runbook and the
  reconcile-before-arm rule are the human control; do not automate a clear on
  startup.
- S9: no tamper-evident log signing or remote syslog is configured. An attacker
  with host write access can alter local logs; ship security.log off-host if
  that adversary is in scope.

## 4. Operator checklist (pre-launch)

1. `ADMIN_TOKEN` and `SESSION_SECRET` are distinct, generated with
   `openssl rand -hex 32`, stored only in the server `.env`, mode 0600.
2. `ALLOW_SIGNUP` is unset/false; requests to `/api/auth/signup` return 404.
3. `LIVE_BOOK_PUBLIC` is false unless the live book is intentionally public.
4. `FRONTEND_ORIGIN` lists only real dashboard origins (no `*`).
5. `SESSION_REVOCATION_FILE` lives on persistent storage, owned by the
   service user. The default backup covers `LIVE_STATE_DIR`; the API's
   revocation file on `engine_data` is outside that backup set. After loss of
   that volume, rotate the session signing secret before serving traffic so
   previously revoked cookies cannot become valid again.
6. Built frontend has been scanned (`python scripts/scan_frontend_bundle.py`).
7. `gitleaks detect` is clean on the deployed commit and its history.
8. `LLM_DAILY_CALL_CAP`, `LLM_DAILY_TOKEN_CAP`, `LLM_DAILY_COST_CAP_USD`, and
   `SEARCH_DAILY_CALL_CAP` are set to deliberate values (never 0 unless you
   intend zero calls). Verify `LIVE_STATE_DIR/spend_guard.json` is mode 0600
   and writable; a corrupt/unwritable guard pauses entries by design.
9. RPC/Birdeye provider quotas are configured and alerting is on: those calls
   are intentionally not blocked by the LLM/search guard because exits use
   them.
10. Set a hard provider-side billing/quota cap for every paid surface and keep
    the app caps as the first line, not the only line:
    - Groq and OpenRouter: project/account spend limit and daily request cap
      matching `LLM_DAILY_CALL_CAP` / `LLM_DAILY_COST_CAP_USD`.
    - Birdeye: plan quota alert plus a hard daily request budget.
    - Helius/RPC: plan quota alert; RPC is deliberately outside the LLM guard
      because exits need it.
    - Oracle Cloud: a $1 monthly budget with alerts on the tenancy (Always Free
      resources still need a budget alarm so a shape/egress mistake is caught).
    - Telegram/ntfy: rate limits on the bot/topic so an alert loop cannot
      become a message bill or notification flood.
11. Apply the deploy limits at the edge too: reverse proxy
    `client_max_body_size`, connection/header limits, and a coarse per-IP rate
    limit. The app enforces `MAX_REQUEST_BODY_BYTES`, body/header caps,
    `--limit-concurrency 200`, and the tiered limiter as the backstop.
12. Confirm `DEBUG=false` in the deployed `.env` and that `TESTING` is not
    imported from environment variables (it is pytest-only). Check a response
    from the public origin carries `Content-Security-Policy` with
    `base-uri 'none'` and `X-Frame-Options: DENY`.
13. Run `python3 scripts/preflight_deploy.py` on the VM before arming. It
    must print `0 FAIL`; a `runtime security` or `container hardening` FAIL
    means the deployed env/compose has undone an S6/S7/S8 control.
14. Keep the API port bound to `127.0.0.1` and SearXNG without a host port.
    Bump Docker digests and GitHub Action SHAs deliberately, then re-run
    `docker compose config` and the preflight.
15. Run `python -m pytest -q backend/tests/test_s9_monitoring_and_restore.py`
    (or the full suite) and then perform a backup→restore drill into a
    throwaway directory with the current archive. Verify the book row-for-row,
    confirm no keypair is present, and confirm the restored kill switch is
    `{"tripped": true}`. Never point a drill at the live state directory.
16. After arming, review `/api/admin/monitoring/security-events` and
    `logs/security.log`. Confirm `LOGIN_FAILURE_BURST`, `SPEND_CAP`, and
    `INTERNAL_ERROR` alerts reach the operator channel, and that log rotation
    (10 MB × 5) is adequate for the expected traffic. Ship `security.log`
    off-host if host tampering is in scope.
17. Confirm `SESSION_REVOCATION_FILE` is persistent and mode 0600. It is outside
    the default `LIVE_STATE_DIR` backup set; rotate the session signing secret
    if its volume is lost. Confirm `LIVE_STATE_DIR/spend_guard.json` is writable and
    that a corrupt guard pauses entries rather than exits.
18. Do not clear a restored kill switch until reconcile reports clean. The
    restore command intentionally trips it with `run reconcile and clear
    manually`; clearing without reconcile is an operator error, not a code
    path.

## 5. Rotation runbook

### Operator token (`ADMIN_TOKEN`)

1. Generate `openssl rand -hex 32`.
2. Update `.env` on the server (mode 0600); do not paste it into chat/logs.
3. Restart the API; confirm the old header token is rejected. With a dedicated
   session secret, an existing cookie may still report authenticated.
4. If `SESSION_SECRET` is unset, all sessions are invalidated by this
   rotation — expected. If set, sessions survive.
5. Clearing `ADMIN_TOKEN` disables login and operator authorization even for
   dedicated-secret cookies; session status reports `auth_disabled`. Re-enabling
   the old secrets can restore unexpired/unrevoked sessions. For an incident,
   rotate both accepted signing secrets; check security events.

### Session secret (`SESSION_SECRET`)

1. Generate a new distinct value; update `.env`.
2. Restart the API. Cookies signed by the old dedicated secret become invalid.
   Legacy admin-token-signed cookies remain accepted while that token remains
   configured. Rotate both secrets to invalidate all accepted signing keys.
3. Confirm login issues a new cookie and logout persists a revocation.

### Provider keys (Jupiter, LLM, alerts)

1. Rotate at the provider first, then update `.env`, then restart.
2. Run the preflight check; send one test alert if alert channels are used.
3. Confirm no old key remains in `logs/` or the built frontend.

## 6. Incident runbook (suspected compromise)

1. **Contain:** engage the kill switch, stop the live cycle, remove the API
   from the public proxy if the operator surface is suspect.
2. **Rotate:** `ADMIN_TOKEN`, `SESSION_SECRET`, wallet/API provider keys,
   alert channel tokens. Assume every credential readable by the process is
   burned.
3. **Preserve evidence:** copy (do not delete) `logs/`, `security.log`,
   `state/session_revocations.json`, and the execution ledger for review.
4. **Assess money path:** compare the ledger against on-chain balances and
   `/api/live/portfolio`; reconcile every fill, nonce, and transfer.
5. **Recover:** restore from an encrypted backup only after verifying the
   archive digest; start disarmed, then re-enable under supervision.
6. **Record:** add a dated entry to the incident log with scope, timeline,
   credentials rotated, and follow-up tests.

## 7. Backup encryption

Backups are encrypted at rest and are never committed. New archives use the
versioned v2 envelope: a per-archive random salt, scrypt (n=2^15, r=8, p=1),
and AES-256-GCM with the header as associated data. A wrong key or any header/
ciphertext tamper fails closed. Restore still accepts legacy Fernet archives,
so the last pre-upgrade backup is not stranded. The backup key must be a valid
Fernet key, differ from `SECRET_STORE_KEY`, and live only in the server
`.env`; preflight enforces both. The archive denylist refuses keypairs,
secret-store keys, sidecars, `.env`, and `.pem` files.

## 8. UNVERIFIED items

The following require infrastructure outside the repository and are tracked
here until an operator supplies evidence. They are launch blockers only where
the operator judges the control necessary for the deployment; none is silently
assumed green by the test suite.

- Docker image digest pinning: the committed digests were resolved from the
  registry, and CI built the pinned ARM64 image and validated the compose file
  successfully on 2026-11-09 ([run 42565425194](https://github.com/hixam001/trading-bot/actions/runs/42565425194)).
  Re-verify the pull on the deployment host and record it.
- GitHub Action SHA pinning: SHAs were resolved via `git ls-remote`; CI ran the
  pinned actions successfully on 2026-11-09 ([Tests 42565425194](https://github.com/hixam001/trading-bot/actions/runs/42565425194),
  [gitleaks 42565425256](https://github.com/hixam001/trading-bot/actions/runs/42565425256),
  [CodeQL 42565425309](https://github.com/hixam001/trading-bot/actions/runs/42565425309)).
  Re-check each SHA when bumping.
- Host firewall/iptables, Docker daemon configuration, and Tailscale ACLs: not
  visible from the repository. Confirm the API is reachable only through the
  intended proxy and that the host is not exposing port 8000.
- Hosted Supabase migration 007: the repository test proves the migration is
  present and complete; it cannot prove the hosted project ran it. Apply it and
  run the verification queries at the bottom of the migration.
- Provider-side billing/quota caps (Groq/OpenRouter, Birdeye, Helius/RPC,
  Oracle budget, Telegram/ntfy): dashboard state is not observable from code.
  Configure and screenshot/record each cap before launch.
- Real backup restore on the host: the automated drill uses a synthetic book
  in a throwaway directory. Restore a current archive into a throwaway target
  on the real host, verify the rows and kill-switch latch, then destroy the
  throwaway target.
- Full Playwright suite against a real backend: the dashboard/operator specs
  need the API on `:8000`; a `vite preview`-only run has pre-existing
  dashboard/operator failures unrelated to §92. Run the suite against the real
  API before declaring the UI green.
- Reverse proxy behavior: TLS termination, Host allowlist, body/header/
  connection limits, and forwarded-header hygiene are proxy configuration, not
  app code. Verify on the deployed proxy.
- OS patch level, Docker engine version, and at-rest disk encryption: host
  controls outside the repository.
- Real alert delivery: the code path and flood caps are tested; delivery to the
  operator's actual ntfy/Telegram destination must be tested once.
- Wallet keypair custody and rotation: the repository never reads or prints the
  real keypair in tests. Confirm the production file channel/permissions and
  custody procedure separately.
- gitleaks history/tracked-tree scan and the built-frontend secret scan: both
  were re-run on the §92 tree on 2026-11-09 (gitleaks v8.30.1: 334 commits,
  11.45 MB, no leaks after allowlisting synthetic test sentinels; frontend
  scanner clean). Re-run both on the exact deployed commit before launch.

## 9. Findings and launch sign-off

This is the engineering-review findings record for §92. Severity is scoped to
this single-operator system: **High** = auth, secret, supply-chain, or
money-path exposure that could lead to loss/compromise; **Medium** = DoS,
availability, or defense-in-depth weakness; **Low** = evidence/recovery gap.

| ID | Severity | Finding and evidence | Fix | Proof |
|---|---|---|---|---|
| F1 | High | Logs, 4xx/5xx bodies, alert payloads, and the security monitor used ad-hoc formatting; a provider token, Bearer header, keypair, PEM, or credentialed URL could reach disk/operator chat. | Central `backend/redaction.py`; logging filter, error handlers, alerts, security monitor all delegate to it. | `test_redaction_adversarial.py`, `test_error_handling_security.py`, `test_logging_and_monitoring.py` |
| F2 | High | A backend-only secret name/value could be inlined into the built frontend; runtime wallet/DB/backup files were not uniformly ignored. | `scripts/scan_frontend_bundle.py`, CI scan, `.gitignore`/`.gitleaks.toml` hardening. | `test_secret_hygiene.py`, gitleaks run recorded in S1 |
| F3 | High | Cookie-authenticated mutations lacked a CSRF proof; logout revocations died on restart; brute force was per-IP only; PoW challenges were replayable; signup was discoverable; unknown JSON fields were silently accepted; weak `ADMIN_TOKEN`/`SESSION_SECRET` could boot. | CSRF middleware + Origin/Referer check, persisted revocations, global failure ceiling, single-use PoW, concealed signup, Pydantic `extra="forbid"`, boot-time entropy validation, optional dedicated `SESSION_SECRET`. | `test_s2_auth_hardening.py`, `test_mass_assignment.py`, `test_route_matrix.py` |
| F4 | High | Public disclosure/live-book/proof paths could return exception text, filesystem paths, or the configured wallet/fee payer on a binding mismatch. | Fixed public reasons, binding mismatch withholds wallet/fee payer, no raw exception stringification. | `test_public_surface_leaks.py` |
| F5 | High | Admin reset interpolated SQL identifiers; PostgREST `anon`/`authenticated` retained table rights and some public tables lacked RLS; request bodies were unbounded; backups used a fast KDF. | Guarded reset-table allowlist, migration 007 revoke+RLS, ASGI body cap, v2 scrypt+AES-256-GCM backup envelope. | `test_s4_sql_allowlist.py`, `test_s4_migrations_rls.py`, `test_s4_request_limits.py`, `test_s4_backup_v2.py` |
| F6 | High | Untrusted token/social/web/KB text reached prompts without a stable data boundary; LLM/search spend had no hard daily ceiling; secrets could be interpolated into prompts/transcripts. | `backend/llm/untrusted.py` fencing/sanitisation, fixed output schema, persisted `spend_guard.py`, prompt field allowlists, usage metadata only. | `test_s5_prompt_injection.py`, `test_s5_spend_caps.py` |
| F7 | Medium | Limiter state was unbounded, `X-Forwarded-For` trust was too broad, multiple workers could each grant a full quota, 429s did not prove `Retry-After`, public DB scans were open-ended, and server body/header/keep-alive/concurrency limits were implicit. | Ordered LRU limiter with expiry sweep, trusted-proxy CIDR check, `--workers 1` + bounded uvicorn flags at every launch site, finite query ceilings. | `test_s6_rate_limit_buckets.py`, `test_s6_deploy_limits.py` |
| F8 | Medium | Security headers were only reliably present on 200s; CSP lacked `base-uri`; debug/test paths could weaken auth/CSRF/rate limits; debug exception text was not redacted. | Shared `security_headers.py` used by middleware and error handlers, `base-uri 'none'`, pytest-only test-host trust, debug bypass tests, redacted debug text. | `test_s7_response_hardening.py` |
| F9 | Medium | Actions/images used movable tags, compose services had writable roots/capabilities/no resource caps, preflight did not reject lost runtime controls, and the JSON wallet channel was silent about `docker inspect` exposure. | Full SHA/digest pinning, read-only/cap-drop/no-new-privileges/resource limits, SearXNG internal-only, preflight runtime+compose checks, wallet-channel warning. | `test_s8_supply_chain.py`, `test_preflight_deploy.py` |
| F10 | Low | Schema-rejected uploads produced no security event; identical event floods could evict evidence; login-burst alerts fired per request; no automated restore drill proved the kill-switch latch. | Bounded dedupe with true counts, schema-rejection event, once-per-IP/window burst alert, restore drill test. | `test_s9_monitoring_and_restore.py`, `docs/17_MUTATION_LOG.md` |

### Verified safe (evidence-backed)

- No unauthenticated route can mutate state or move funds. Every mutating
  route is Operator, Cookie+CSRF, or Concealed in the matrix, and
  `test_route_matrix.py` fails if a route is added without a row.
- The money path is unchanged: the deterministic gate, sizing, simulation,
  signing, kill switch, and exit scanner still control execution. The LLM can
  only return a verdict; `test_s5_prompt_injection.py` proves model output
  cannot set size/mint/stops/caps/slippage/kill switch.
- A spent or unavailable LLM/search budget pauses **entries** only. Exits do
  not call the LLM, and the S5 tests assert the exit path remains operational.
- There is no payment surface, no card data, no customer PII, no file upload/
  multipart endpoint, and no frontend Supabase client. Knowledge-base ingest is
  JSON-only with extension/size/path checks.
- No real credential was found in the tracked tree, git history, or built frontend. Full-history gitleaks v8.30.1 scanned 334 commits / 11.45 MB with no leaks after allowlisting only synthetic test sentinels; the frontend scanner currently passes.
- The wallet keypair is excluded from backup archives by the denylist, and the
  restore drill asserts the restored state contains no `wallet_keypair.json`.
- CSRF is not satisfied by SameSite alone; a cross-site form cannot set
  `X-Requested-With`, and a mismatched `Origin`/`Referer` is rejected even with
  the header.
- Single-worker launch is asserted at all four launch sites; the limiter has a
  hard key bound, expiry sweep, and new-client accounting; 429s carry
  `Retry-After`.
- Containers run read-only with `cap_drop: ALL`, `no-new-privileges`, resource
  limits, loopback-only API, and no SearXNG host port.
- Supply-chain references are pinned by full SHA/digest and preflight rejects a
  deployment that loses DEBUG/HTTPS/rate-limit/body-cap/compose hardening.
- Every security event type has a production emitter; dedupe and burst-alert
  state are hard-bounded; a restore forces the kill switch engaged.
- The decisions listed in the prompt were preserved: fomo/Privy, hardcoded
  live flags, Jupiter Swap V1, kill-switch default off, the 11-rule gate, and
  the public transparency endpoints. §92 did not change money-path behaviour.

### Rotate-now list

No credential was proven exposed by this review. Rotate immediately if any of
the following is true, because the review cannot see the operator's shell
history, chat, host backups, or provider dashboards:

- `ADMIN_TOKEN` or `SESSION_SECRET` was ever pasted into chat, a ticket, a
  shell transcript, or a log.
- The Solana wallet keypair, `BACKUP_ENCRYPTION_KEY`, `SECRET_STORE_KEY`, or a
  provider key was stored outside the server `.env`/mounted key file.
- A pre-§92 backup archive or log bundle left the host without encryption.
- A hosted Supabase `service_role` key or DSN was used in a frontend build or
  committed `.env`.
- Any S1/S8 scan is non-clean on the exact deployed commit.

If in doubt, rotate `ADMIN_TOKEN`, `SESSION_SECRET`, provider keys, and alert
tokens; treat the wallet as compromised if its keypair was ever outside the
approved custody boundary.

### Operator-must-do list

1. Generate real `ADMIN_TOKEN`, `SESSION_SECRET`, `BACKUP_ENCRYPTION_KEY`, and
   `SECRET_STORE_KEY` with `openssl rand -hex 32`; keep them distinct, mode
   0600, server-only.
2. Apply Supabase migration 007 and run its verification queries; do not assume
   the repository test applied it remotely.
3. Configure provider-side hard billing/quota caps and alerting for every paid
   surface listed in §4 item 10.
4. Run `scripts/preflight_deploy.py`; require `0 FAIL`, `DEBUG=false`, HTTPS/
   rate limits on, exact `TRUSTED_PROXY_CIDRS`, and a non-zero body cap.
5. Confirm API loopback binding, no SearXNG host port, and host firewall/
   Tailscale ACLs that expose only the intended proxy.
6. Create a current encrypted backup; run the restore drill into a throwaway
   directory; verify rows, denylist, and the forced kill-switch latch.
7. Test real alert delivery (`LOGIN_FAILURE_BURST`, `SPEND_CAP`,
   `INTERNAL_ERROR`) and confirm `security.log` rotation/retention/off-host
   copying.
8. Keep `SESSION_REVOCATION_FILE` persistent and backed up; keep
   `LIVE_STATE_DIR/spend_guard.json` writable/mode 0600.
9. Run the full pytest suite, tsc/build, frontend secret scan, Playwright
   against the real API on `:8000`, prepush, and CI on the deployed commit.
10. Re-check every UNVERIFIED item in §8 and record the evidence before
    declaring launch sign-off.


## 10. §93 findings — 2026-11-09

These IDs refer to the follow-up root-filesystem audit, separately from §92
section 9. Full failing/passing evidence, projection assumptions and operational
limitations are in [the §93 report](19_ROOTFS_AUDIT.md).

| ID | Severity | Evidence | Fix | Test |
|---|---|---|---|---|
| F1 | High | Read-only defaults had no writable mount; smoke initially overwrote configured state | Compose homes, strict AST/site inventory, expanded-mount preflight, simulated EROFS and exclusive sibling smoke; loud persistence failures | `test_audit_93_r1.py`, `test_audit_93_r2.py` |
| F2 | High | Weak-token config import stopped the wallet-holding runner | Clear invalid credential, record CRITICAL reason; reject API startup only | `test_audit_93_r3.py` |
| F3 | Medium | FILLED signatures matched raw secret-key shape; plain `token=BONK` was masked | Bounded public registry after secret replacement; context-sensitive generic params | `test_audit_93_r4.py` |
| F4 | Medium | Sanitised model ticker disagreed with raw candidate key | Shared normalization, collision/placeholder exclusions and candidate-owned mint selection | `test_audit_93_r5.py` |
| F5 | Medium | 60-second tick already needs 1,440 brain calls/day before other stages | Offline per-provider call/token/cost/search scenarios, startup log and preflight WARN; caps unchanged | `test_spend_projection.py`, `test_audit_93_r6.py` |
| F6 | Medium | 200 simultaneous 16 MiB bodies could buffer about 3.2 GB | 1 MiB global limit; bounded ingest override including escaped JSON; >4 MiB preflight WARN | `test_audit_93_r6.py` |
| F7 | Low | Synthetic hosts received trust outside pytest | Shared `config.is_test_host` used by all four consumers | `test_audit_93_r7.py` |
| F8 | Low | Disabled signup returned 422 for empty/invalid bodies | Conceal at route handler entry before FastAPI parses input | `test_audit_93_r7.py` |
| F9 | Low | Route matrix listed auth without testing it | Probe all matrix rows without credentials; explicit idempotent logout exception | `test_route_matrix.py` |
| F10 | Low | Backup and read-only failure claims exceeded behavior | Corrected revocation backup exclusion and silent-write residual; contract/smoke evidence linked | `test_audit_93_r1.py`, this document |

Verified green CI on the final application code: [Tests & Security Audit
42665808979](https://github.com/hixam001/trading-bot/actions/runs/42665808979),
including ARM64/native builds and real-container ROFS smoke for both roles.
The exact synthetic redaction-fixture exclusions passed [gitleaks
42666899068](https://github.com/hixam001/trading-bot/actions/runs/42666899068).
Docker is unavailable locally; its runtime verification was observed in CI.


## §94: recovering an unresolved exit

Absence alone and blockhash expiry alone never release a signed sell. Automatic
release requires explicit historical ABSENT from every configured endpoint,
explicit `isBlockhashValid(..., commitment="finalized") == False` from each,
at least two distinct hosts, a known signing timestamp and minimum signing age.
`SELL_ATTEMPT_RELEASE_MIN_AGE_SECONDS` defaults to 600 seconds; positive values
clamp to at least 300, and invalid/zero/negative values use the default. Two
complete observations at least 60 seconds apart are required; the first persists
in `release_candidate_since`, and an unmet fact resets it. A release archives
the attempt as failed and returns without authorizing another order.

All configured RPCs could be lying or behind. The combined evidence and delayed
reobservation reduce that residual risk; they cannot eliminate it. Legacy
attempts without `signed_at` require operator resolution. A held attempt logs
an ERROR each cycle and uses the existing per-mint alert cooldown.

Run `python scripts/resolve_pending_sell.py --state-dir <state-copy> list` to
inspect signatures, age, evidence and explorer links without changing files.
After independently verifying that the signature never landed, use
`release --mint M --signature S --i-verified-signature-never-landed` and type
`release` at the prompt. For a chain-confirmed sell use
`confirm --mint M --signature S --proceeds-usd X` and type `confirm`. All writes
use the locked ledger/commit APIs; unknown signatures refuse. Do not release on
silence, a missing field, a timeout, unknown blockhash validity or age alone.


## §94: simulation consensus scope

Consensus requires account presence, program owner, executable flag, data length
and simulation error to agree. SPL/Token-2022 control bytes (mint, token owner,
delegate, state, native reserve, delegated amount, close authority, extensions)
remain exact on wallet accounts and foreign vaults. Only the u64 payload of the
Token-2022 TransferFeeAmount extension (type 2, length 8) is market state; its TLV
header, account discriminator and all other extension bytes remain exact.

Wallet ownership is discovered from exact token owner facts on both responses,
including accounts outside the derived ATA list. Wallet amounts and lamports
keep their existing tolerances. Opaque pool/curve/oracle bytes and lamports
are not used by a guard and no longer need equality across slots. Foreign vault
amounts are used only by guard F; consensus compares F's actual boolean verdict
against the guard's chosen pre-state, rather than imposing a vault amount
tolerance. If either response would make F disagree with the other, consensus
refuses. The guard still executes F on the chosen response. Signed aggregate
wallet output accounting and the single-RPC sell fallback remain in force.

Refusals log `SIM_CONSENSUS_REFUSED side=... category=... addr=...`; no new
per-refusal alert is added. **§95:** `/api/health` returns exactly `{"status":"ok"}`.
No unauthenticated route exposes counters, categories or side breakdowns.
API and live have independent process memory. The live heartbeat hook now
atomically writes `STATE_DIR/consensus_counters.json` (a temporary sibling plus
rename); failure logs a warning without interrupting heartbeat or execution.
Only live writes this file; API retains `engine_state:ro`. No new HTTP route
is needed; the operator script is sufficient.

The fixed schema is `written_at` (epoch seconds), `since_boot_at`,
`scope="live-process"`, buy/sell `attempts`, and buy/sell `refused` counts for
`structural`, `control`, `wallet_amount`, `wallet_lamports`, `vault_amount` and
`malformed`. Counts are integers, with no addresses, mints or signatures.
They reset on process restart; the old file is never loaded into the counters.
`since_boot_at` marks that boundary. Logs remain the source of truth across
restarts and the cross-check during burn-in.

```sh
python3 scripts/consensus_status.py --state-dir /path/to/copied/state --cycle-interval-seconds 60
docker compose exec -T api python scripts/consensus_status.py
docker compose logs --since 1h live | rg 'SIM_CONSENSUS_REFUSED|SIM_CONSENSUS_ATTEMPT'
```

The script opens only `consensus_counters.json`, read-only, refuses symlinks,
and performs no locking, repair, network calls or secret-sidecar reads.
It prints counts and file age; older than twice the cycle interval is `STALE`.
Missing data says `STALE: no data yet`, never healthy zeros; corrupt and
unreadable data are also `STALE`. Exit codes: 0 fresh, 1 invalid/stale,
2 missing/unreadable. State-dir defaults to `LIVE_EXECUTION_STATE_DIR`, then
`LIVE_STATE_DIR`, then `backend/live_execution/state`; cycle interval defaults
to `TICK_INTERVAL_SECONDS` (60 seconds if unset). The script does not load
`.env`; pass the deployed interval explicitly when reading a copied directory.

**§95 payer identity:** check F's consensus predicate receives the explicit
payer supplied to the guard. Intent/pre-state without a payer is malformed;
`intent.wallet != payer_pubkey` refuses as `guard_refused` before any RPC.
Intent still supplies the input mint and raw amount; tolerances are unchanged.

For burn-in, count log events over the same window (one attempted buy is a
pre-state fetch):

```sh
docker compose logs --since 1h live | awk '/SIM_CONSENSUS_ATTEMPT side=buy phase=pre/{attempts++} /SIM_CONSENSUS_REFUSED side=buy/{refusals++} END{printf "buy attempts=%d consensus refusals=%d\n",attempts,refusals}'
```

These are refusal events per attempted buy; multiple RPC comparisons can emit
multiple events for one attempt. Real refusal rates require VM burn-in.


## §94: read-only state preflight

Before upgrading, stop the old signer, copy its state to a private scratch
directory, and run `python scripts/validate_state.py --state-dir <copy>`. Keep
that copy and the old image for rollback. The validator reads recognized
authority files only, never creates locks or import caches, and prints counts
and invalid row indices/field names/reasons without values. Exit codes are
valid, invalid and unreadable as described in `docs/11_DEPLOYMENT.md`. The same
validators run as deployment preflight's `state validation` check for existing
configured state directories; invalid/unreadable state fails preflight.
Historical writer fixtures pass without loosening the runtime validators.
Real VM state acceptance remains unverified until that copied-state check runs.


## §94: explicit external HTTP policy

`bounded_request` continues to cap wire/decoded bodies, total time and redirect
following. A caller explicitly requesting redirects now receives ValueError
before a request; an incompatible explicit Accept-Encoding also refuses rather
than being silently replaced. Identity headers are case-insensitive and sent
once. Client-default compression headers are still replaced with identity.
Returned redirects log only host and status on a per-host hourly cadence, with
a bounded host cache. Configure final provider URLs; no redirect is followed.

The offline source audit found no production caller explicitly asking for
redirect following. HTTP SearXNG examples are local-service URLs, not evidence
that the VM endpoints work. Custom provider URLs and redirects need operator
verification; retained verification ran with external networking disabled.
