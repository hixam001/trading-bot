# API contract

Reviewed against repository source on **2026-11-14**. This describes the current
FastAPI application; it does not promise compatibility for an unreleased API.
See the [documentation index](README.md) and [data model](DATA_MODEL.md).

## Transport and conventions

- The default API origin is `http://localhost:8000`. Production uses HTTPS;
  non-loopback HTTP requests are redirected with 308 when enforcement is enabled.
- Paths are unversioned (`/api/...`). The application metadata version `1.0.0`
  is not a negotiated contract version.
- Requests with bodies use `Content-Type: application/json`. There is no trade
  placement, position-close, wallet-upload, or arming HTTP endpoint.
- UTC timestamps are ISO 8601 strings in database-backed responses. Live JSON
  records also contain Unix timestamps in seconds; those fields are identified
  in [DATA_MODEL.md](DATA_MODEL.md). USD values are JSON numbers, not cents.
- `null` means unknown/unavailable or not applicable. It must not be converted
  into a measured zero. An empty collection is a valid response.
- `limit` and `offset` are integers. Invalid bounds return 422. Offset pagination
  describes a changing dataset; consumers should deduplicate by stable IDs.
- Production (`DEBUG=false`) disables `/docs`, `/redoc`, and `/openapi.json`.
  Admin routes are excluded from OpenAPI even when debug documentation is on.

Source: [main.py](../backend/api/main.py),
[route handlers](../backend/api/routes/),
[frontend types](../frontend/src/types/index.ts).

## Authentication and request limits

| Access label | Requirement |
|---|---|
| Public | No operator credential required. |
| Local/operator | Direct loopback without forwarding headers, or operator authentication. Any forwarding header disables the loopback shortcut. |
| Live-book | Local/operator, or public access explicitly enabled by `LIVE_BOOK_PUBLIC=true`. |
| Operator | Valid `X-Admin-Token` header or signed, unexpired HttpOnly session cookie. Disabled when a valid `ADMIN_TOKEN` is not configured. |

The default cookie is `admin_session`, with a 3,600-second absolute lifetime.
Polling does not extend it. Cookies use `Path=/`, HttpOnly, Secure on HTTPS,
and SameSite=Lax normally. An HTTPS dashboard on a different hostname uses
SameSite=None. Browsers must send credentials on protected requests.

A cookie-bearing unsafe request under `/api/` must also send
`X-Requested-With: XMLHttpRequest`; a supplied Origin/Referer must match the
effective origin or a configured `FRONTEND_ORIGIN`. Login and challenge paths
are exempt. This check still applies when a header-authenticated request also
carries a session cookie. Logout without a cookie is an idempotent exception.

The in-process limiter shares each tier's budget per client IP, across its
paths: auth defaults to **5/min**, paid-service to **20/min**, general API to
**120/min**. The paid tier includes ingestion, system status, safety, holdings,
stats, and verification. Static assets are exempt. Limiter responses expose
`Retry-After` and `X-RateLimit-*` headers; separate credential-guess lockouts
can also return 429. One API worker per state directory is the supported
deployment assumption. Configure trusted proxies before exposing the API.

The default JSON request-body cap is **1 MiB**. Ingestion uses a separate
derived cap, `MAX_INGEST_CHARS * 12 + 65536` bytes (2,465,536 bytes at defaults),
to allow UTF-8/JSON escaping. Body overflows return 413. Schema limits remain
independently enforced; request timing also depends on the deployment proxy.
See [security route matrix](10_SECURITY.md#1-route-matrix) for the security
inventory and [configuration](CONFIGURATION.md) for the knobs.

## Read endpoints

Bounds below use `default [minimum..maximum]`. Unlisted query parameters are
not part of this contract.

| Method | Path | Access | Parameters | Successful response / meaning |
|---|---|---|---|---|
| GET | `/api/health` | Public | None | `{"status":"ok"}`; API liveness only. |
| GET | `/api/feed` | Public | `limit=50 [1..500]`, `offset=0 [0..100000]` | `{total,limit,offset,events}`; newest first. |
| GET | `/api/mint/{mint}/history` | Public | Same bounds, `limit=100` | `{mint,total,limit,offset,events}`; valid unknown mint gives an empty page; malformed Solana address gives 422. |
| GET | `/api/events.json` | Public | `limit=100 [1..500]` | `{events}`; durable thought/did/refused/read/trade events, distinct from feed rows. |
| GET | `/api/funnel` | Public | `limit=1000 [1..10000]` | Bounded decision counts, window metadata, refusal rate and explanatory `note`. |
| GET | `/api/funnel/snapshots` | Public | `limit=300 [1..1000]` | `{snapshots,count,note}`; persisted observations, oldest first within the selected window. |
| GET | `/api/holdings` | Local/operator | None | `{cash_usd,cash_status,open_positions,count,paper_trading_only}`. |
| GET | `/api/journal` | Public | `limit=50 [1..500]`, `offset=0 [0..100000]` | `{total,limit,offset,trades}`; closed mirrored trades. |
| GET | `/api/stats` | Local/operator | None | Historical performance and current wallet valuation; see below. |
| GET | `/api/market-regime` | Public | `limit=100 [1..1000]` | `{regimes,count}`; regime history, not a trade instruction. |
| GET | `/api/promotion-gate` | Public | None | Readiness evaluation from closed-trade evidence; never arms the engine. |
| GET | `/api/knowledge-base` | Public | None | `{static_knowledge,ingested,dynamic_stats}`; ingested index exposes filename, digest and ingestion time, not document bodies. |
| GET | `/api/system-status` | Public | None | Provider status, daily call summaries, recent LLM usage, configured cadence and DEX-filter summary. |
| GET | `/api/safety` | Live-book | None | Armed/kill-switch/breaker/break/blocklist state, provider and alert diagnostics, counters and thresholds. |
| GET | `/api/live/portfolio` | Live-book | None | Current live ledger positions with wallet cash and chain-scan diagnostics, or `{enabled:false,reason}`. |
| GET | `/api/live/executions` | Live-book | None | `{enabled,generated_at_utc,commits,records,totals}`, or `{enabled:false,reason}`; recent live intent/ledger history. |
| GET | `/api/proof.json` | Public | None | `{generated_at_utc,commits,fills,refusals,counts}`. |
| GET | `/api/exits.json` | Public | None | `{thresholds,open_position_marks}`; numeric exit configuration and stored marks. |
| GET | `/api/verify.json` | Public | None | Recomputed commit-hash and on-chain memo verification results; may require RPC and use cached results. |
| GET | `/api/binding.json` | Public | None | Commit-to-fill transaction binding checks; may require RPC and use cached results. |
| GET | `/api/refusals.json` | Public | `limit=100 [1..500]` | `{generated_at_utc,count,refusals}`. |
| GET | `/api/theses.json` | Public | `limit=100 [1..500]` | `{generated_at_utc,count,theses}`; durable position write-ups. |
| GET | `/api/disclosure.json` | Public | None | Armed/single-book flags, kill switch, break, configuration/sizing truths and proof-policy disclosures. |
| GET | `/api/reasoning.json` | Public | `limit=50 [1..200]` | `{generated_at_utc,count,reasoning}`. |
| GET | `/api/admin/monitoring/security-events` | Operator | `limit=50 [1..200]`, optional `event_type` (max 50 chars) | `{status,summary,events}`; bounded security-event history. |
| GET | `/api/admin/monitoring/logs` | Operator | None | Log-directory/file status and security-monitor summary; not raw log download. |

### Feed and decision semantics

`FeedEvent` includes `id`, `ts`, `symbol`, `mint_address`, `candidate_snapshot`,
`verdict`, `thesis`, `rule_breakdown`, `failed_rule_ids`, `regime_ok`,
`grounding_flags`, `narration_source`, `led_to_trade_id`, `model_version`,
`prompt_version`, and `risk_flags`.

- `verdict="fail"` with nonempty `failed_rule_ids`: deterministic gate refusal.
- `verdict="fail"` with empty `failed_rule_ids`: model refusal in the funnel
  classification.
- `verdict="pass"`: gate and model approved. Execution can still refuse;
  it does **not** establish a fill.
- Rules carry `rule_id`, `passed`, `detail`, `value`, and `evaluated`.
  `evaluated=false` means the stage was skipped, not a measured passing value.
- Funnel `filled` counts recent decision commits with fill signatures. Its
  commit window is separately bounded; it is not a join proving a conversion
  rate for exactly the feed rows on the current page.

An empty feed response is:

```json
{"total":0,"limit":50,"offset":0,"events":[]}
```

### Valuation and degraded responses

`/api/stats` separates historical `equity_usd` (initial anchor plus closed
realized P&L) from current `wallet_equity_usd` (USDC + SOL mark + open-position
marks). `equity_status` is `complete`, `partial`, or `unavailable`; the wallet
total is null unless complete. `missing` lists unavailable components.
`cash_status` is `live` or `unavailable`. The historical `INITIAL_CASH_USD`
anchor is not spendable wallet cash.

`/api/live/portfolio` has its own `equity_usd` convention (cash plus open
position value, with unpriced positions held at cost); it is not
interchangeable with the stats wallet total.
Its enabled response includes `armed`, `manual_confirmation`, `wallet`,
`cash_usd`, `sol_balance`, `positions`, realized/unrealized values,
`deployed_today_usd`, `chain_scan`, `chain_excluded`, `count`, and generation
time. A disabled response is a successful read of an unavailable/disarmed
book, not proof that the process is healthy or the wallet is empty.

### Proof verification shapes

`/api/verify.json` returns `{algorithm,memo_algorithm,totals,memo_totals,rows}`.
Each row contains decision `id`, symbol/verdict, stored/recomputed hashes,
`match`, and a `memo` verification block. A hash match alone does not establish
memo publication or a fill.

`/api/binding.json` returns `{generated_at_utc,totals,pairs}`. Totals count
`matched`, `mismatched`, `unknown`, and `unbound`; pairs carry decision ID,
symbol, signature, venue, status and named checks. RPC unavailability yields
`unknown`, not a successful match. These read-only reports do not authorize
execution or guarantee third-party chain data availability.

## Authentication endpoints

| Method | Path | Request | Successful response / failure |
|---|---|---|---|
| GET | `/api/auth/challenge` | None | `{nonce,timestamp,difficulty,signature}` for the internal PoW provider. |
| POST | `/api/auth/login` | JSON below | `{status:"authenticated",expires_in:3600}` with Set-Cookie (TTL configurable); 400 bot rejection, 403 disabled/invalid credential, 429 lockout. |
| GET | `/api/auth/session` | Cookie or header if available | `{authenticated,...}`; may include `auth_type`, `expires_in`, or a reason; never returns a credential. |
| POST | `/api/auth/logout` | Cookie + CSRF header when cookie exists | Revokes the session signature and clears the cookie; no-cookie request remains idempotent. |
| POST | `/api/auth/signup` | Registration schema only if enabled | Concealed 404 by default; `ALLOW_SIGNUP=true` reveals an unsupported-registration response and still creates no account. |

Login accepts only:

```json
{"token":"<operator-token>","captcha_token":null,"_hp_website":""}
```

`token` is required (1–512 characters, trimmed); `captcha_token` is optional
(up to 1,024 characters); `_hp_website` is an empty honeypot (up to 256
characters). Unknown fields are forbidden, producing 422. The bot check runs
before credential comparison. When internal CAPTCHA is enabled, solve
SHA-256 of `nonce:solution` with the required leading hex zeros and submit
`nonce.timestamp.signature.solution` as `captcha_token`. Challenges expire
after five minutes and successful solutions are single-use. External CAPTCHA
providers instead require their own supported challenge client.

**Current UI limitation:** [LoginGate.tsx](../frontend/src/components/LoginGate.tsx)
sends only the token; it does not solve or render configured CAPTCHA challenges.
An enabled CAPTCHA therefore needs a compatible client/UI implementation.

## Operator mutations

### POST `/api/knowledge-base/ingest`

Operator authentication is required. Cookie clients also supply the CSRF
header. This is a JSON ingestion API, not multipart file upload.

```json
{"documents":[{"filename":"strategy-notes.md","content":"Research notes."}]}
```

- At most 50 documents; an empty list returns 400.
- Filename length 1–255; content length 1–500,000 with the independent
  `MAX_INGEST_CHARS` cap (default 200,000 characters) and derived whole-body cap.
- Extensions: `.md`, `.txt`, `.json`, `.csv`. Binary executable signatures,
  executable shebangs and null bytes are rejected. Filenames are sanitized
  and stored under the configured ingestion directory.
- Unknown top-level/document fields return 422 (`extra="forbid"`).
- Response: `{ingested:[...],errors:[{filename,error},...]}`. A 200 can include
  individual failures; inspect both arrays. Schema failures reject the request
  before per-document processing.

### POST `/api/admin/reset`

Operator authentication plus `confirm=yes` is mandatory. `mode` accepts:

| Mode | Effect |
|---|---|
| `reset_book` (default) | Reset SQL book tables and the historical portfolio cash row. |
| `wipe_paper` | Legacy name: clear SQL feed/trades and reset the historical cash row. |
| `prune_only` | Trim feed and regime rows to configured retention limits. |

Returns timestamps, mode, deletion counts and `paper_trading_only:false`.
Missing confirmation/invalid mode returns 400. This route is destructive to
SQL history; it does not liquidate tokens or reset the live execution JSON
authority. Run only as deliberate maintenance with backups and reconciliation.

## WebSocket and static surface

`WS /ws/feed` is public. A supplied Origin must be in the frontend allowlist.
The server polls new SQL feed rows every two seconds and sends **one JSON feed
row per text message**, without a wrapper or initial history dump. Capacity is
32 clients globally and 4 per IP by source defaults. Policy/capacity
rejection closes with 1008; slow sends can close with 1013. The server can send
empty binary liveness frames after 30 seconds without incoming text; ignore
non-event frames. There is no acknowledged replay cursor.

Hydrate with `/api/feed`, reconnect with backoff, and hydrate again after
reconnect. The shipped client merges by ID, sorts newest first and retains
200 rows. A 50-row hydration cannot guarantee recovery of every event after
a long outage; use paginated REST history for deeper recovery.

`GET /` serves the built SPA or a JSON banner when no build exists. `/assets`
is mounted when build assets exist. The SPA fallback `/{full_path:path}` blocks
secret/dotfile/source-map probes. `GET /api` and `/api/{full_path:path}` return
JSON 404 instead of falling through to the SPA. These are frontend routing
surfaces, not additional business APIs.

## Errors and compatibility

| Status | Meaning |
|---|---|
| 400 | Bot rejection, empty ingestion, missing reset confirmation or bad mode. |
| 403 | Missing/invalid operator authorization or CSRF failure. |
| 404 | Unknown API path, concealed signup or blocked sensitive path. |
| 413 | Request body exceeds its route's cap. |
| 422 | Request/path/query schema validation failed; sanitized `detail` array. |
| 429 | Rate limit or credential-guess lockout; honor Retry-After when present. |
| 5xx | Generic `detail` and opaque `error_id`; correlate with server logs. |

Most application errors use `{"detail":...}`. Validation items omit raw input;
5xx responses hide production internals. Middleware can reject before a route
runs. Clients should tolerate additive response fields and preserve unknown
statuses as unavailable. Changes to meaning, units, required fields, access
rules or pagination require coordinated handler, frontend type, test and
documentation updates; no automatic compatibility guarantee is implemented.
