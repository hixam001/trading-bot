# Security Controls and Route Matrix (§92)

This is the operator-facing security record for the production trading bot.
Every control below is enforced in code, exercised by a named test, and
summarised in the mutations table. It is deliberately written to be read
under pressure: the route matrix first, then the threat-control map, then
the incident/rotation runbooks.

> Scope note: there is no payment surface in this system. Authentication
> protects operator actions and money-adjacent read surfaces; it never
> creates accounts, charges cards, or stores customer PII.

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
| POST | /api/auth/logout | Cookie+CSRF | Revokes signature and clears cookie. |
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
| S2 | Weak ADMIN_TOKEN/SESSION_SECRET refuses boot | `config.py` `RuntimeError` | `test_s2_auth_hardening.py` |
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
| S5 | LLM calls have hard daily call/token/cost ceilings, persisted across restart and fail-closed | `backend/llm/spend_guard.py`, `llm/client.py` | `test_s5_spend_caps.py` |
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

Phases S5–S9 are appended as they land; this table is the authoritative
control↔test map, not a marketing summary.

## 3. Residual risks

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
- S5: the spend guard is per-host. Two API process replicas sharing one
  `LIVE_STATE_DIR` serialize on the filesystem atomic replace but do not share
  an in-process lock; use one writer per state directory, or move the counter
  to a transactional store before horizontal scaling.
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

## 4. Operator checklist (pre-launch)

1. `ADMIN_TOKEN` and `SESSION_SECRET` are distinct, generated with
   `openssl rand -hex 32`, stored only in the server `.env`, mode 0600.
2. `ALLOW_SIGNUP` is unset/false; requests to `/api/auth/signup` return 404.
3. `LIVE_BOOK_PUBLIC` is false unless the live book is intentionally public.
4. `FRONTEND_ORIGIN` lists only real dashboard origins (no `*`).
5. `SESSION_REVOCATION_FILE` lives on persistent storage, owned by the
   service user, and is included in backups.
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

## 5. Rotation runbook

### Operator token (`ADMIN_TOKEN`)

1. Generate `openssl rand -hex 32`.
2. Update `.env` on the server (mode 0600); do not paste it into chat/logs.
3. Restart the API; confirm `/api/auth/session` is unauthenticated and old
   tokens are rejected.
4. If `SESSION_SECRET` is unset, all sessions are invalidated by this
   rotation — expected. If set, sessions survive.
5. Revoke any leaked token by rotating immediately; check security events.

### Session secret (`SESSION_SECRET`)

1. Generate a new distinct value; update `.env`.
2. Restart the API. All existing browser sessions become invalid.
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
here until an operator supplies evidence:

- Docker image digest pinning: registry digests must be resolved on the
  deployment host/CI and committed; local builds cannot prove them.
- GitHub Action SHA pinning: SHAs must be resolved from each action's
  release tag at review time; tag refs are not immutable.
- Host firewall/iptables and Tailscale ACLs: not visible from the repo.
