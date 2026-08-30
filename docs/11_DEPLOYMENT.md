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

# State volume is NOT optional for the live engine:
docker volume create bot_state bot_data

docker run -d --name trading-bot \
  --env-file .env \
  -p 8000:8000 \
  -v bot_state:/app/backend/live_execution/state \
  -v bot_data:/app/data \
  -e DB_PATH=/app/data/trading_bot.db \
  trading-bot
```

or `docker compose up -d --build` (compose file wires the volumes + env_file).

The entrypoint starts the API always, and the live cycle **only if** the
hardcoded `LIVE_TRADING_ENABLED` in `backend/live_execution/config.py` is True
AND a wallet secret is configured — the same double-gate as `start.sh`.

## 2. Secrets runbook (read before arming on any host)

The image contains **zero secrets**; everything arrives at runtime.

**Solana wallet keypair — set exactly ONE channel:**

1. `WALLET_KEYPAIR_PATH` — **preferred**. A `chmod 600` JSON byte-array file
   OUTSIDE the repo (e.g. `/opt/trading-bot/secrets/wallet_keypair.json`),
   bind-mounted read-only into the container:
   `-v /opt/trading-bot/secrets/wallet_keypair.json:/run/secrets/wallet_keypair:ro`
   `-e WALLET_KEYPAIR_PATH=/run/secrets/wallet_keypair`
   The material never appears in `docker inspect` or process env.
2. `WALLET_KEYPAIR_JSON` — the keypair JSON **directly in the environment**,
   for hosts that cannot mount files (Render/Koyeb/Railway). Parsed in-memory
   by `live_execution/wallet.py`; never written to disk, never logged (a log
   redactor masks any accidental dump). Caveat: env vars are readable via
   `docker inspect` / `/proc/<pid>/environ` by root on the host — acceptable
   on a single-operator VM, which is why the file channel is preferred.

**Always also set `EXPECTED_WALLET_ADDRESS`** — the loaded keypair must
derive exactly that pubkey or every load refuses (identity pin).

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

## 3. Dashboard on Vercel (split mode)

1. Import the repo; set Root Directory = `frontend`.
2. Env var: `VITE_API_BASE_URL=https://<engine-host>` (public origin only).
3. Build: `npm run build` (default). Output: `dist`.
4. On the engine: `FRONTEND_ORIGIN=https://<vercel-domain>` (comma-separated
   list is supported; add `http://localhost:5173` for dev).
5. WebSockets connect directly to the engine (`wss://<engine-host>/ws/feed`);
   no Vercel involvement, works on all plans.

Keep-alive (free-tier engines that sleep): an external cron (cron-job.org /
GitHub Actions) pinging `/api/system-status` every 5–10 min prevents
spin-down. On Oracle's always-on VM this is unnecessary.

## 4. Database

- **Default:** SQLite at `DB_PATH` — put it on a mounted volume (compose does).
- **Supabase:** run `migrations/supabase/*.sql` in the SQL editor, then set
  `USE_SUPABASE_DB=1`, `SUPABASE_DB_URL`, `SUPABASE_SERVICE_ROLE_KEY`
  (server-side only, never frontend). `api/db_pg.py` is a drop-in with the
  identical surface.

## 5. Persistence rules (real-money safety)

On ANY host, these must survive restarts or the engine must not be armed:
- `backend/live_execution/state/` — kill switch, daily-loss breaker,
  idempotency ledger, commit log, operator break. A vanished kill-switch file
  reads as "clear"; a vanished idempotency ledger weakens replay protection.
- The SQLite book (or Supabase).
Free PaaS filesystems are ephemeral — that is exactly why the recommendation
is an always-on VM with a persistent disk/volume.

**One directory, one truth.** Every live-state path resolves from
`backend/config.py` (`LIVE_STATE_DIR`, `BREAK_STATE_FILE`,
`KILL_SWITCH_FILE`) and `live_execution/config.py` (`STATE_DIR`, via
`LIVE_EXECUTION_STATE_DIR`). If you relocate state on a host, override the
env vars — never hand-edit paths in code. A stale path does not error; it
silently forks a second state directory, and an operator-tripped kill switch
in the wrong directory is a kill switch nothing reads.
`backend/tests/test_state_path_colocation.py` pins this.

**Container supervision.** The entrypoint runs the API *and* (when armed +
wallet configured) the live cycle, and exits non-zero if EITHER dies, so the
platform's restart policy recovers the whole engine. An armed deployment must
never keep serving a green dashboard while the decision cycle is dead.

## 6. Pre-flight checklist

- [ ] `pytest` fully green from the repo root
- [ ] Image contains no `.env`, no keypair, no `state/` (check `.dockerignore`)
- [ ] `EXPECTED_WALLET_ADDRESS` set and matching the funded keypair
- [ ] State dir on a persistent volume
- [ ] `ADMIN_TOKEN` set (mutating endpoints disabled without it — by design)
- [ ] `FRONTEND_ORIGIN` = the actual dashboard origin (CORS fail-closed)
- [ ] Dashboard reachable; `/api/live/portfolio` shows `enabled` only if armed
