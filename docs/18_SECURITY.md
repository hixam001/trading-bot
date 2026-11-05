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
