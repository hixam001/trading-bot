# 11 — DEPLOYMENT RUNBOOK

**Target architecture (all free-tier viable):**

| Piece | Host | Why |
|---|---|---|
| Engine (API + WebSocket + live cycle) | Always-on VM — **Oracle Cloud Always Free** (ARM Ampere A1: **2 OCPU / 12 GB** for an always-on VM — the verified allowance of 1,500 OCPU-hours + 9,000 GB-hours/month. The 4-OCPU/24-GB shape EXCEEDS Always Free: on a free tenancy such instances are disabled then deleted 30 days after trial end. Keep the VM at 2/12 — or upgrade to Pay-As-You-Go, where the Always Free allowance stays free and only usage above it is billed. Plus persistent disk) or any Docker host | The engine is a long-running stateful daemon (60s cycle, 15s exit scan, WS feed). Serverless/PaaS free tiers sleep after idle — a sleeping bot misses exit scans on real money. Persistent disk is MANDATORY for the live-execution state (kill switch, breaker, idempotency ledger). |
| Dashboard (static SPA) | **Vercel** / Cloudflare Pages (free) | Perfect fit for a Vite static build. (Note: Vercel Hobby is nominally non-commercial; CF Pages has no such clause.) |
| Database | SQLite on a mounted volume (default) or **Supabase Free Postgres** (`USE_SUPABASE_DB=1`) | Both paths already implemented (`api/db.py` / `api/db_pg.py`); Supabase free tier pauses only after a week of inactivity — the bot writes every tick. |

Single-origin alternative: skip Vercel and let the engine serve the built SPA
(the image bakes it in at `/app/frontend/dist`) — one URL, zero CORS.

---

## 1. Build & run the engine (Docker)

```bash
docker build -t trading-bot .

# State, data, and log volumes are NOT optional for the live engine:
docker volume create bot_state bot_data bot_logs

docker run -d --name trading-bot \
  --env-file .env \
  -p 127.0.0.1:8000:8000 \
  -v bot_state:/app/backend/live_execution/state \
  -v bot_data:/app/data \
  -v bot_logs:/app/logs \
  -e DB_PATH=/app/data/trading_bot.db \
  -e LOG_DIR=/app/logs \
  trading-bot
```

The dashboard binds to loopback only; reach it through an SSH tunnel
(`ssh -L 8000:127.0.0.1:8000 user@host`) or a TLS-terminating reverse proxy.
`docker compose` already enforces `127.0.0.1:${API_PORT:-8000}:8000`.

or `docker compose up -d --build` (compose file wires the volumes + env_file
+ the §51 `searxng` sidecar — the keyless self-hosted search hop; set
`SEARXNG_URL=http://searxng:8080` in `.env` so the engine reaches it over
the compose network. The sidecar is NOT published to the host; the
standalone `docker run` above simply leaves the SearXNG hop unconfigured
and the chain degrades to Brave → Firecrawl — fail-soft either way. See
`docs/12_FREE_SEARCH_STACK.md`).

#### SearXNG Image Tag Pinning & Secret Wiring (§78 D1–D2)
- **Tag Pinning**: The sidecar is pinned in `docker-compose.yml` to verified release `searxng/searxng:2026.10.9-f4822b3fc`. To pin to the immutable sha256 manifest digest for absolute reproducibility, inspect the manifest:
  ```bash
  docker manifest inspect searxng/searxng:2026.10.9-f4822b3fc
  ```
  and configure `image: searxng/searxng@sha256:85492ba800a861e3e8ff6014c430e81369fe6ca766a9990595f616d1ce95b272`.
- **Secret Wiring**: `SEARXNG_SECRET` is passed natively through `environment:` in `docker-compose.yml`. SearXNG's settings loader overrides any `server.secret_key` in `settings.yml` with the environment variable. `deploy/searxng/settings.yml` intentionally omits `secret_key` to rely on this native environment injection. Template available in `deploy/searxng/settings.yml.example`.

### Process Separation (ROLE=api vs ROLE=live)

Per §77 B2 security architecture, `docker-compose.yml` splits the engine into two isolated services from the same image:
- **`api` service (`ROLE=api`)**: Serves the web dashboard and REST/WebSocket API. It mounts the state directory **read-only** (`:ro`) and **never receives any wallet secrets** (`WALLET_KEYPAIR_*` are explicitly stripped). The dashboard process can now run on an unprivileged host with zero key material. Health check: `GET /api/health`.
- **`live` service (`ROLE=live`)**: Runs the autonomous execution cycle (`run_live_cycle.py`). It mounts the state directory read-write (`rw`), loads the wallet signing key, and exposes **no ports** to the host or internet. Restart policy: `on-failure` (§78 F1). If armed without a wallet, the container outputs a clear message and exits cleanly (`exit 0`), preventing Compose restart loops. Health check: verifies `heartbeat.txt` freshness (< 300s).

If running a single container without `ROLE`, both run in the same container, emitting a prominent warning that process separation is violated.

The entrypoint starts the API always (under `ROLE=api`), and the live cycle **only if** `ROLE=live` (or single-container fallback), the hardcoded `LIVE_TRADING_ENABLED` in `backend/live_execution/config.py` is True, AND a wallet secret is configured — the same double-gate as `start.sh`.

#### Reverse Proxy & Trusted Subnets (`TRUSTED_PROXY_CIDRS`) (§78 F4)
When running behind a reverse proxy (e.g. Caddy, Traefik, Nginx) on a Docker bridge network, configure `TRUSTED_PROXY_CIDRS` (default: `127.0.0.1,172.16.0.0/12,::1/128`; `FORWARDED_ALLOW_IPS` is accepted as a legacy fallback).
- The API only trusts `X-Forwarded-For` when the direct socket peer is inside these trusted CIDRs. Direct untrusted connections ignore XFF to defeat header spoofing.
- In multi-hop XFF chains, the API selects the rightmost untrusted IP (standard proxy defense) to resolve real client identities for rate limiting and threat tracking.

## 2. Secrets runbook (read before arming on any host)

The image contains **zero secrets**; everything arrives at runtime.

**Solana wallet keypair — set exactly ONE channel:**

1. `WALLET_KEYPAIR_PATH` — **preferred**. A `chmod 600` JSON byte-array file
   OUTSIDE the repo (e.g. `/opt/trading-bot/secrets/wallet_keypair.json`),
   bind-mounted read-only into the container:
   `-v /opt/trading-bot/secrets/wallet_keypair.json:/run/secrets/wallet_keypair:ro`
   `-e WALLET_KEYPAIR_PATH=/run/secrets/wallet_keypair`
   The material never appears in `docker inspect` or process env.
   **Permission check:** `wallet.load_keypair()` enforces strict file permissions (`st_mode & 0o077 == 0`). Group- or world-accessible files (e.g. 0644) are rejected with a fatal error.
2. `WALLET_KEYPAIR_JSON` — the keypair JSON **directly in the environment**,
   for hosts that cannot mount files (Render/Koyeb/Railway). Parsed in-memory
   by `live_execution/wallet.py`; never written to disk, never logged (a log
   redactor masks any accidental dump). Caveat: env vars are readable via
   `docker inspect` / `/proc/<pid>/environ` by root on the host — acceptable
   on a single-operator VM, which is why the file channel is preferred.

**Always also set `EXPECTED_WALLET_ADDRESS`** — the loaded keypair must
derive exactly that pubkey or every load refuses (identity pin).

**Solana RPC Configuration (Dual-RPC Consensus §79 Phase A):**
- Configure both `SOLANA_RPC_URL` and `SOLANA_RPC_URL_2` pointing to distinct RPC providers with different hostnames (e.g., QuickNode and Helius).
- Buys strictly require $\ge 2$ agreeing RPC endpoints on distinct hosts (`SIM_MIN_AGREEING_RPCS = 2`). If `SOLANA_RPC_URL_2` is omitted or shares the same host as `SOLANA_RPC_URL`, pre-sign simulation fails closed and buys are blocked.
- Sells allow single-RPC fallback when `SIM_SELL_ALLOW_SINGLE_RPC = True` so position exits are not trapped by an outage of a secondary RPC provider.

**Fail-closed rule:** neither channel set → `WalletError` → the live cycle
never starts. There is deliberately NO env bypass for the ARM flags; the
keypair channels are infrastructure, like `SOLANA_RPC_URL`.

**All other secrets** (`BIRDEYE_API_KEY`, `DEEPSEEK_API_KEY`/`GROQ_API_KEY`,
scraper keys, `FOMO_PRIVY_REFRESH_TOKEN`, `ADMIN_TOKEN`, `SUPABASE_DB_URL`)
are already env-based: put them in the host's `.env` (root-owned, 600) passed
via `--env-file`, or in the PaaS secret UI. **Never** in any `VITE_*` var —
those are inlined into the public JS bundle.

**Rotation:** generate a fresh keypair, fund it, update
`EXPECTED_WALLET_ADDRESS`, restart the container. Revoke/sweep the old wallet.

**Session Revocation Durability (§78 E3):**
Revocations are stored in memory and reset on process restart. An operator who needs immediate
cluster-wide revocation across restarts should rotate `ADMIN_TOKEN` (which
also invalidates every existing cookie because sessions are derived from it).


## 3. Dashboard on Vercel (split mode)

1. Import the repo; set Root Directory = `frontend`.
2. Env var: `VITE_API_BASE_URL=https://<engine-host>` (public origin only).
3. Build: `npm run build` (default). Output: `dist`.
4. On the engine: `FRONTEND_ORIGIN=https://<vercel-domain>` (comma-separated
   list is supported; add `http://localhost:5173` for dev).
5. WebSockets connect directly to the engine (`wss://<engine-host>/ws/feed`);
   no Vercel involvement, works on all plans.
6. Log in with the operator token on the dashboard. The session cookie is
   emitted `SameSite=None; Secure` when the login Origin differs from the
   API host (HTTPS split mode), and `SameSite=Lax` for same-origin.

Keep-alive (free-tier engines that sleep): an external cron (cron-job.org /
GitHub Actions) pinging `/api/system-status` every 5–10 min prevents
spin-down. On Oracle's always-on VM this is unnecessary.

## 4. Database

- **Default:** SQLite at `DB_PATH` (resolved as `backend/ + $DB_PATH`, i.e. app-relative) — put it on a mounted volume (compose does). `scripts/backup_state.py` and `scripts/restore_state.py` resolve the same path through `backend/config.py` and load `.env`, so a host-run backup can never silently archive a different (or missing) database; a missing DB refuses the backup outright (§83 Phase G).
- **Supabase:** run `migrations/supabase/*.sql` in the SQL editor, then set
  `USE_SUPABASE_DB=1`, `SUPABASE_DB_URL` (direct asyncpg connection to the pooler).
  `api/db_pg.py` is a drop-in with the identical surface.

## 5. Persistence rules (real-money safety)

On ANY host, these must survive restarts or the engine must not be armed:
- `backend/live_execution/state/` — kill switch, daily-loss breaker,
  idempotency ledger, commit log, operator break. A vanished kill-switch file
  reads as "clear"; a vanished idempotency ledger weakens replay protection.
- The SQLite book (or Supabase).
- `logs/` — rotating runtime, error, and security logs (`app.log`, `error.log`, `security.log`). Persistent volume retains threat audit trails across container rebuilds.
Free PaaS filesystems are ephemeral — that is exactly why the recommendation
is an always-on VM with a persistent disk/volume.

**One directory, one truth.** Every live-state path resolves from
`backend/config.py` (`LIVE_STATE_DIR`, `BREAK_STATE_FILE`,
`KILL_SWITCH_FILE`) and `live_execution/config.py` (`STATE_DIR`, via
`LIVE_EXECUTION_STATE_DIR`). If you relocate state on a host, override the
env vars — never hand-edit paths in code. A stale path does not error; it
silently forks a second state directory, and an operator-tripped kill switch
in the wrong directory is a kill switch nothing reads.
**Leave these variables unset unless relocating a file**: an empty value
would otherwise resolve to the repo root, because `os.getenv(k, default)`
returns `""` for a set-but-empty variable and `Path("")` is `.`. Since
§83 Phase A an empty value is treated exactly like unset, and `.env.example`
ships `BREAK_STATE_FILE`/`KILL_SWITCH_FILE` commented out.
`backend/tests/test_state_path_colocation.py` pins all of this.

**Container supervision.** The entrypoint runs the API *and* (when armed +
wallet configured) the live cycle, and exits non-zero if EITHER dies, so the
platform's restart policy recovers the whole engine. An armed deployment must
never keep serving a green dashboard while the decision cycle is dead.

## 6. Pre-flight checklist

- [ ] `pytest` fully green from the repo root
- [ ] Image contains no `.env`, no keypair, no `state/` (check `.dockerignore`)
- [ ] `EXPECTED_WALLET_ADDRESS` set and matching the funded keypair
- [ ] State dir on a persistent volume
- [ ] Log dir on a persistent volume (audit trail preservation)
- [ ] `ADMIN_TOKEN` set and >= 32 characters (`openssl rand -hex 32`) — mutating and operator endpoints disabled without it
- [ ] `FRONTEND_ORIGIN` = the actual dashboard origin (never wildcard `*` with credentials)
- [ ] WebSocket connections per IP bounded (`MAX_WS_PER_IP=4` default)
- [ ] Dashboard reachable; `/api/live/portfolio` shows `enabled` only if armed
- [ ] Outbound alerts verified with `python scripts/send_test_alert.py`
- [ ] Pre-arm and post-upgrade guard dry-run verified without keypair (§81 Phase G):
  ```bash
  python scripts/dry_run_guard.py --side buy --mint So11111111111111111111111111111111111111112 --usd 10
  ```
  Must exit 0 with `VERDICT: PASSED`. Ensures live Jupiter quote params, bytecode inspection, and multi-RPC pre-sign simulation invariants succeed before arming or after upgrades.

---

## 7. Outbound Alerting Setup (§79 Phase D)

The engine supports multi-channel push notifications to ensure immediate visibility of critical operational events (kill switch trips, daily-loss breaker activation, guard/simulation refusals, low SOL/USDC reserves, stale cycle heartbeats, unexplained wallet balance drops, and brute-force auth bursts).

Both channels are optional. If neither is configured, alert events are logged locally without raising errors or blocking execution.

### Channel 1: ntfy (Recommended — Free & Open-Source)

ntfy allows receiving push notifications on Android, iOS, and Web without account registration.

1. **Choose an unguessable private topic name**: generate with `openssl rand -hex 12` (e.g., `trading-bot-a7f49b12c8e3`).
   *(Anyone who knows the topic name can subscribe on public ntfy.sh. The bot explicitly refuses to broadcast to guessable defaults like `trading-bot` or `alerts` without authentication)*.
2. **Configure `.env`**:
   ```bash
   ALERT_NTFY_URL=https://ntfy.sh           # Default; or your self-hosted URL
   ALERT_NTFY_TOPIC=trading-bot-a7f49b12c8e3 # Private unguessable topic
   ALERT_NTFY_TOKEN=                       # Optional: if using an auth-protected instance
   ```
3. **Subscribe**: Install the ntfy app on your phone and subscribe to your topic name.

### Channel 2: Telegram Bot

1. **Create a bot**: Message `@BotFather` on Telegram, send `/newbot`, and follow prompts to obtain `ALERT_TELEGRAM_BOT_TOKEN` (format: `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`).
2. **Get your chat ID**: Message your new bot, then send a message to `@userinfobot` or check `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your numeric `chat_id`.
3. **Configure `.env`**:
   ```bash
   ALERT_TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
   ALERT_TELEGRAM_CHAT_ID=123456789
   ```

### Verifying Alert Delivery

Run the verification script from the repository root:
```bash
python scripts/send_test_alert.py
```
To test an isolated channel:
```bash
python scripts/send_test_alert.py --channel ntfy
python scripts/send_test_alert.py --channel telegram
```
The script outputs HTTP status codes (`HTTP 200 — SUCCESS`) without ever printing secret tokens or keys.

---

## 8. Diagnostic & Performance Reporting

### Gate Funnel Diagnostic Report (`scripts/gate_funnel_report.py`)

Answers why the bot has had zero gate-passers or closed trades by analyzing candidate attrition through the rule engine and thinker:

```bash
# Analyze candidate decisions from a specific date:
python scripts/gate_funnel_report.py --since 2026-09-01

# Analyze an explicit SQLite database file:
python scripts/gate_funnel_report.py --db backend/trading_bot.db --since 2026-09-01

# Output machine-readable JSON:
python scripts/gate_funnel_report.py --since 2026-09-01 --json
```

The diagnostic computes:
- **Daily candidate funnel**: candidates seen, gate-passed, think-passed, and entered.
- **Per-rule failure rates & sole blockers**: candidates that failed *only* that rule.
- **Counterfactual analysis**: "if rule X were removed, N candidates would have passed the gate".
- **Top 3 blocking rules distribution**: min, median, and p90 of failing metrics vs configured thresholds.
- **Thinker / LLM refusal rate & verdict mix**: proportion of gate-passers declined by the LLM.

### Performance Scorecard (`scripts/perf_report.py`)

Computes track record against reference disclosures from closed trades and decision commits:

```bash
python scripts/perf_report.py --since 2026-09-06
```

