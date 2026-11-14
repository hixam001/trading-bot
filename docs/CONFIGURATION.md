# Configuration reference

Reviewed **2026-11-14**. Copy [.env.example](../.env.example) for the complete
operator variable catalog. This guide explains ownership, safe combinations
and path behavior; defaults ultimately come from
[backend/config.py](../backend/config.py),
[live config](../backend/live_execution/config.py), and
[Compose](../docker-compose.yml).

## Configuration layers

1. Source constants own risk-critical rules, exits and arming. They cannot be
   changed by adding an environment variable with the same name.
2. Backend configuration calls `load_dotenv()` and reads process environment.
   Existing exported variables take precedence over dotenv values by default.
3. Compose loads the root `.env` via `env_file` and explicitly overrides role,
   paths and wallet channels per service. Read the rendered deployment shape
   privately; it can contain secrets.
4. Vite substitutes `VITE_*` values at **build time**. The supported frontend
   setting is a public API origin, never a credential.

Changing an environment file generally requires restarting the affected
backend process. Changing the frontend API origin requires rebuilding the
frontend. There is no operator settings API for changing risk controls.

## Source-owned controls

| Location | Current representative controls |
|---|---|
| `backend/live_execution/config.py` | `LIVE_TRADING_ENABLED=True`, `REQUIRE_MANUAL_CONFIRMATION=False`, `SIMULATION_REQUIRED=True`; per-trade $50, 3 open positions, $150 total exposure, $300 daily deployment and $75 realized-daily-loss breaker. |
| `backend/config.py` | Entry-rule thresholds, numeric exits, sell gates, risk/sizing formulas and 15-second exit scanning. |
| `backend/api/websocket.py` | Global 32-client and per-IP 4-client default limits. The current backend configuration does not parse a `MAX_WS_PER_IP` environment variable. |

These values describe committed source; effective orders must satisfy all
applicable controls. Shared sizing constants are not substitutes for live
executor limits. The retired shared `PAPER_TRADING_ONLY` constant is not a
live disarm switch. Setting `DATA_BACKEND=mock` does not change live arming.
Human-only arming procedures remain in the [deployment guide](11_DEPLOYMENT.md)
and [README live-trading section](../README.md#live-trading-operator-armed).

## Environment groups

| Group | Variables / values | Effect |
|---|---|---|
| Market data | `DATA_BACKEND=mock` (default) or `live`; `BIRDEYE_API_KEY` for real discovery | Selects candidate/provider inputs. Mock mode needs no market API key. |
| Main model | `MAIN_LLM_PROVIDER=deepseek` or `groq`; corresponding `DEEPSEEK_API_KEY` / `GROQ_API_KEY` | Cloud reasoning/narration. Missing credentials degrade closed for entries. Model IDs/base URLs are cataloged in `.env.example`. |
| Optional evidence | `SOCIAL_LLM_*`, `FOMO_PRIVY_REFRESH_TOKEN`, `BRAVE_SEARCH_API_KEY`, `SEARXNG_URL`, configured scraper/search fallback keys | Staged social/crowd/search enrichment; no configured chain means the optional stage is off or required evidence remains unknown. |
| LLM/search caps | `LLM_DAILY_CALL_CAP=2000`, `LLM_DAILY_TOKEN_CAP=2000000`, `LLM_DAILY_COST_CAP_USD=5.0`, `SEARCH_DAILY_CALL_CAP=500` | Application budget guards. Zero/negative caps mean zero allowance, not unlimited. Provider billing caps remain separate. |
| Cycle | `TICK_INTERVAL_SECONDS=60`, `MAX_CANDIDATES_PER_TICK` | Cadence and candidate bound. Cadence is not a maximum execution duration. |
| SQL storage | `DB_PATH`; `USE_SUPABASE_DB=1` with `SUPABASE_DB_URL` for Postgres | SQLite by default; all Supabase migrations `001`–`007` required. |
| Historical reporting | `INITIAL_CASH_USD=1000.0` | Historical curve anchor, not an internal accumulator authorizing live cash. |
| Operator auth | `ADMIN_TOKEN`, optional `SESSION_SECRET` | Minimum 32 characters with additional entropy/non-placeholder checks. Invalid configured credentials fail API startup. Use distinct strong secrets. |
| Session | `SESSION_COOKIE_NAME=admin_session`, `SESSION_MAX_AGE_SECONDS=3600`, `SESSION_REVOCATION_FILE` | Cookie identity, absolute TTL and persisted logout revocations. |
| Exposure/privacy | `LIVE_BOOK_PUBLIC=false`, `FRONTEND_ORIGIN` | Explicit public live-book opt-in; comma-separated allowed frontend origins. Holdings/stats still use their local/operator guard. |
| HTTPS/debug | `FORCE_HTTPS`, `DEBUG=false`, `TRUSTED_PROXY_CIDRS` | TLS redirect/security headers, production error/docs behavior and real-client IP trust. Never trust arbitrary forwarding peers. |
| Bot protection | `CAPTCHA_ENABLED`, `CAPTCHA_PROVIDER`, provider secret variables; `ALLOW_SIGNUP=false` | Server bot verification. Enabling signup does not implement registration; current browser login does not solve CAPTCHA. |
| HTTP bounds | `MAX_REQUEST_BODY_BYTES=1048576`, `MAX_INGEST_CHARS=200000` | Default JSON-body byte cap and independent document character cap; ingestion body cap is derived as `MAX_INGEST_CHARS * 12 + 65536` bytes. |
| Rate limiting | `RATE_LIMIT_ENABLED=true`; auth/paid/general per-minute caps 5/20/120 | Shared tier budgets per client IP in the API process. |
| Retention | `FEED_PRUNE_KEEP`, `REGIME_PRUNE_KEEP` | Limits used by explicit pruning; do not imply automatic deletion of all telemetry. |
| Logs/alerts | `LOG_DIR`, `ALERT_NTFY_*`, `ALERT_TELEGRAM_*`, heartbeat settings | Rotating diagnostics and optional delivery channels. Alert credentials stay server-side. |
| Encrypted persistence | `SECRET_STORE_KEY` / `SECRET_STORE_KEY_FILE`, `FOMO_PRIVY_STATE_FILE`, `BACKUP_ENCRYPTION_KEY` | Secret-sidecar encryption and separate backup encryption; never use the same secret for both. |

Jupiter URL/key settings are read by the current client helpers; use the
catalog and [Jupiter migration guide](16_JUPITER_API_MIGRATION.md), rather than
assuming older keyless endpoints still apply. Outbound HTTP callers use bounded
responses (default 8 MiB, total 60 seconds), identity encoding and no redirect
following; these constraints are implemented in code, not an arbitrary proxy
setting. A redirecting provider URL needs correction to its final endpoint.

## Wallet channels and process roles

| Variable | Purpose |
|---|---|
| `WALLET_KEYPAIR_PATH` | Wallet file outside tracked source. Preferred when file mounts are available; wins if both channels are set. |
| `WALLET_KEYPAIR_JSON` | In-memory fallback for file-less hosts. Never print or bake into an image. |
| `EXPECTED_WALLET_ADDRESS` | Required wallet identity pin; mismatch refuses loading. |
| `SOLANA_RPC_URL` | Primary RPC; defaults to the source-defined public mainnet URL. |
| `SOLANA_RPC_URL_2` | Secondary independent RPC endpoint; the configured RPC list is assembled in live config. |

The API role strips both wallet-secret channels in Compose/entrypoint. It can
read public wallet identity and protected book information without loading a
signing key. The live role receives a read-only keypair mount, whose ownership
and mode must pass wallet-loader checks. Use the deployment instructions for
UID and mount setup.

## Mutable path contract

| Data | Local resolution | Compose resolution |
|---|---|---|
| SQL book | `DB_PATH` is joined to `backend/`; default `backend/trading_bot.db`. An absolute path stays absolute. | Both roles use `/app/data/trading_bot.db` on `engine_data`. |
| Live authority | `LIVE_EXECUTION_STATE_DIR` for signer; `LIVE_STATE_DIR` for shared readers; both default to `backend/live_execution/state/`. | Shared `engine_state` location; API mount read-only, live read-write. |
| Kill/break files | Defaults under shared live-state path; explicit file overrides exist. | Must resolve to the same authority as the runner. |
| Logs | Default repository `logs/`, configurable with `LOG_DIR`. | `/app/logs` on `engine_logs`. |
| Session revocations | Default root `state/session_revocations.json`. | API `/app/data/session_revocations.json`. |
| Ingested knowledge | Default `backend/knowledge_base/ingested/`; override `KB_INGESTED_DIR`. | API `/app/data/knowledge_base/ingested`. |
| Spend state | Default under live state unless `SPEND_GUARD_FILE` overrides. | Separate API `/app/data/spend_guard_api.json`; live state has its own counters. |
| Supabase fingerprint | `SUPABASE_PIN_FILE`, default backend fingerprint sidecar. | Writable data-volume path. |
| Backups | Configured backup directory. | Live `/app/backups` on `engine_backups`. |

Set **both** live-state directory variables consistently when relocating state.
Align blocklist, encrypted sidecar and key paths using the deployment shape;
some legacy local defaults live outside the authority directory. Empty-path
handling differs across settings; do not assume an empty string always means
disabled. Container roots are read-only; writable files must map to persistent
volumes or permitted temporary storage. See
[runtime write inventory](18_SECURITY.md#1b-runtime-write-paths).

## Frontend origin

`VITE_API_BASE_URL` defaults to an empty string (same origin). For a split
deployment set it to the public backend origin before building, and allow the
dashboard's exact origin in backend `FRONTEND_ORIGIN`. The client derives
`ws://`/`wss://` from that origin. Credentialed CORS cannot use wildcard origins.
Anything with the Vite prefix becomes public bundle content.

Validate deployment configuration with
`.venv/bin/python scripts/preflight_deploy.py`; inspect failures locally without
sharing rendered secrets. This is a check, not an arming or deployment command.
