# 12 — ORACLE ALWAYS FREE DEPLOY GUIDE (engine + dashboard, ~30–45 min)

**Read `docs/11_DEPLOYMENT.md` first** — it is the architecture runbook (why
a VM, why persistent state). This doc is the concrete, do-this-next recipe
for the Oracle free tier, including the two Oracle-specific gotchas that
bite everyone (iptables + security list).

**Sizing (verified vs Oracle docs 2026-08-30):** create the VM as
**VM.Standard.A1.Flex — 2 OCPUs, 12 GB RAM** (the Always Free allowance:
1,500 OCPU-h + 9,000 GB-h/month ≈ 2 OCPU + 12 GB running 24/7). Do NOT
create 4 OCPU / 24 GB on a free tenancy — oversized A1 instances are
disabled, then **deleted 30 days after the trial ends**. 200 GB block
storage and 10 TB/month egress are free; 47 GB minimum boot volume.

> ⚠️ **Before the new instance goes live: `./stop.sh` on the laptop.** Two
> armed cycles = two books racing to place the same orders from the same
> wallet. Exactly ONE live cycle may run anywhere.

---

## Step 1 — Account + VM (Oracle Console)

1. Sign up at oracle.com/cloud/free (needs phone + credit card; card is
   only verified, not charged on the free tier). **Pick the home region
   carefully — Always Free resources can ONLY live there and it cannot be
   changed later.**
2. Compute → Instances → Create Instance:
   - **Name:** trading-bot
   - Image: **Canonical Ubuntu 24.04** (aarch64 — default for Ampere)
   - Shape: **VM.Standard.A1.Flex**, **2 OCPU**, **12 GB**
   - Boot volume: default ~47 GB is fine
   - SSH key: generate or paste your public key (you need the private key
     to log in)
   - Create. Note the **Public IP** when it finishes.
   - If you hit *"out of host capacity"*: retry later / try another AD —
     free A1 capacity is contended in popular regions.

## Step 2 — SSH in + install Docker

```bash
ssh -i <your-key> ubuntu@<PUBLIC_IP>

sudo apt-get update && sudo apt-get -y upgrade
# Docker (arm64 builds come automatically)
curl -fsSL https://get.docker.com | sudo sh
# get.docker.com normally enables the daemon, but VERIFY: a disabled daemon
# stays down after an Oracle maintenance reboot.
sudo systemctl enable --now docker
systemctl is-enabled docker              # must print: enabled
sudo usermod -aG docker ubuntu && exit
ssh -i <your-key> ubuntu@<PUBLIC_IP>   # re-login to pick up the docker group
docker run --rm hello-world           # sanity check
```

## Step 3 — Open the network (BOTH layers — the classic Oracle trap)

Oracle VMs have TWO firewalls. Opening only one gets you timeouts.

1. **Console side:** Networking → VCN → your VCN → Security Lists → Default
   Security List → **Add Ingress Rules**:
   - `0.0.0.0/0`, TCP **80** (HTTP — required for Caddy automatic HTTPS redirect and ACME challenges)
   - `0.0.0.0/0`, TCP **443** (HTTPS — encrypted dashboard & API)
   - (22 should already exist for SSH)
   *(Note: Do NOT open port 8000 to 0.0.0.0/0. Port 8000 binds to loopback only and is reverse-proxied by Caddy/Nginx over TLS).*
2. **VM side (Ubuntu images ship blocking iptables rules):**

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

> [!IMPORTANT]
> **Reverse Proxy on Oracle VCN Subnets (10.x IPs) (§79 Phase F-3):**
> Oracle Cloud VCNs default to `10.0.0.0/16`. To prevent IP spoofing from neighboring VMs on the VCN, `10.0.0.0/8` was removed from the default `TRUSTED_PROXY_CIDRS` (`127.0.0.1,172.16.0.0/12,::1/128`).
> If your reverse proxy (e.g. Caddy, Traefik, or an Oracle Load Balancer) connects to the bot container from an Oracle VCN private IP (e.g. `10.0.x.x`), you must explicitly configure:
> `TRUSTED_PROXY_CIDRS=127.0.0.1,172.16.0.0/12,::1/128,10.0.0.0/16` (or `FORWARDED_ALLOW_IPS`) in your `.env`.

## Step 4 — Get the code + secrets onto the VM

```bash
# On the VM:
mkdir -p /opt/trading-bot && cd /opt/trading-bot

# EITHER clone (if the repo is reachable from the VM):
git clone <your-repo-url> app && cd app

# OR from the laptop (excludes venv/logs; .env handled next step):
#   rsync -av --exclude .venv --exclude logs --exclude .run \
#     --exclude 'trading_bot.db' ./ ubuntu@<PUBLIC_IP>:/opt/trading-bot/app/
mkdir -p /opt/trading-bot/app/secrets
```

Secrets (NEVER commit these; they live only on the VM):

```bash
# From the laptop — the .env and the wallet keypair:
scp .env ubuntu@<PUBLIC_IP>:/opt/trading-bot/app/.env
scp /home/hixam/.config/solana/mainnet-wallet.json \
    ubuntu@<PUBLIC_IP>:/opt/trading-bot/app/secrets/wallet_keypair.json

# On the VM — the container runs as uid 10001, and a bind mount preserves
# the HOST file's owner/mode. wallet.py rejects group/world bits, and the
# container user must be able to read the file at all:
sudo chown 10001:10001 /opt/trading-bot/app/secrets/wallet_keypair.json
sudo chmod 600 /opt/trading-bot/app/secrets/wallet_keypair.json
sudo chown 10001 /opt/trading-bot/app/secrets
sudo chmod 700 /opt/trading-bot/app/secrets
sudo chmod 600 /opt/trading-bot/app/.env
```

## Step 5 — Edit the VM's `.env` for server mode

Four lines differ from your laptop's `.env` (edit on the VM):

```ini
FRONTEND_ORIGIN=https://bot.example.com          # after Step 7; before that use http://127.0.0.1:8000 via SSH tunnel
WALLET_KEYPAIR_PATH=/run/secrets/wallet_keypair  # the container mount, not the host path
WALLET_KEYPAIR_HOST_PATH=/opt/trading-bot/app/secrets/wallet_keypair.json  # host side of the bind mount (default matches this)
FOMO_PRIVY_STATE_FILE=/app/data/.fomo_privy.json # rotated Privy token must survive
DB_PATH=/app/data/trading_bot.db                 # already forced by compose, keep anyway
```

`FOMO_PRIVY_STATE_FILE` matters: Privy **rotates** the refresh token and the
bot persists the fresh one; on the laptop it wrote to the repo root — in a
container that filesystem dies on every rebuild. Point it at the
`engine_data` volume (`/app/data`) so the auth chain survives rebuilds.

## Step 5.5 — Preflight (do this before the first `up`)

```bash
cd /opt/trading-bot/app
sudo apt-get install -y python3-cryptography   # once, if not already present
python3 scripts/preflight_deploy.py
```

The preflight is read-only, opens **no network sockets**, and never prints a
secret. It prints a PASS/WARN/FAIL table and exits **1** if any check fails:

- `DATA_BACKEND=live`, the selected main-LLM provider key is present.
- `ADMIN_TOKEN` >= 32 chars, `SEARXNG_SECRET` non-empty/non-default.
- `BACKUP_ENCRYPTION_KEY` is a valid Fernet key and differs from
  `SECRET_STORE_KEY`.
- `FRONTEND_ORIGIN` is `https://` for public hosts (WARN while loopback-only).
- `SOLANA_RPC_URL_2` exists and uses a different host from `SOLANA_RPC_URL`.
- An alert channel is configured (`send_test_alert.py` uses the same one).
- `.env` selects exactly one wallet secret channel (`WALLET_KEYPAIR_PATH` or
  `WALLET_KEYPAIR_JSON`); the JSON channel does not require a keypair file.
- `WALLET_KEYPAIR_PATH` must be exactly `/run/secrets/wallet_keypair` (the
  in-container path; the host side belongs in `WALLET_KEYPAIR_HOST_PATH`),
  and the JSON channel WARNs if the compose bind source is missing or not a
  regular file. Pre-create that mount source with
  `sudo install -m 600 -o 10001 /dev/null <path>` or set
  `WALLET_KEYPAIR_HOST_PATH=/dev/null`.
- For the file channel, the host bind source exists, is a regular file, mode
  0600/0400, owner uid **10001** — a FAIL prints the exact `chown`/`chmod`
  commands. Readability is tested only when the preflight itself runs as uid
  **10001** or root (another local user cannot see the container's access).
- `docker compose config` parses and the `api` service has no non-empty
  `WALLET_*` value.
- `.env` is not group/world readable (WARN).

**Do not run `docker compose up -d --build` while any line says FAIL.** WARNs
are operator decisions; read them before arming.

> In-image variant (after the image exists):
> `docker compose run --rm --entrypoint python live /app/scripts/preflight_deploy.py`.
> Run the host variant for the wallet ownership/mode check — the container
> sees the bind target (`/run/secrets/wallet_keypair`), not the host file.

## Step 6 — Run the engine

```bash
cd /opt/trading-bot/app
docker compose up -d --build
```

- The image builds ON the VM (ARM). Expect 5–10 min the first time.
- `docker-compose.yml` splits the system into two isolated services from the same image (§77 B2):
  - **`api`**: Runs the dashboard and REST API. Mounts state `:ro` and has **zero wallet secrets**. The dashboard process can run on an unprivileged host with zero key material.
  - **`live`**: Autonomous trade execution engine (`run_live_cycle.py`). Mounts state `rw` and holds the signing key. No ports exposed.
- The keypair is bind-mounted read-only into the `live` container by default
  (parameterised by `WALLET_KEYPAIR_HOST_PATH`, default
  `./secrets/wallet_keypair.json`, target `/run/secrets/wallet_keypair`).
  The image runs as **uid 10001**; a bind mount keeps the host file's uid and
  mode. `wallet.py` refuses group/world bits, so the host file must be owned
  by 10001 and mode 0600 (Step 4). The entrypoint tests `-f && -r` first and
  exits **78** with a `FATAL: wallet keypair not readable by uid 10001`
  message instead of claiming the engine is armed.
- The entrypoint starts the API under `ROLE=api`, and the live cycle under `ROLE=live` because the repo
  is ARMED and the wallet is configured — same double-gate as `start.sh`.

**Verify:**

```bash
docker compose ps                    # api and live healthy; searxng running
docker compose logs -f live --tail=50     # watch the live cycle: ARMED banner, cycle lines
curl http://127.0.0.1:8000/api/health      # returns {"status":"ok"}
```

`searxng` has no healthcheck (it is a fail-soft search sidecar, not on the
order path); `docker compose ps` shows it as running. `api` and `live` are the
health-gated services.

> If pip fails compiling `uvloop`/`httptools` (cp314 aarch64 wheels can
> lag), edit the Dockerfile base `FROM python:3.14-slim` →
> `FROM python:3.13-slim` and rebuild. Nothing in the code needs 3.14.

## Step 7 — HTTPS with Caddy (recommended before long-running real money)

Plain HTTP leaks nothing secret (all keys are server-side, the dashboard is
read-only, the admin token rides only in request headers) — but the admin
endpoints and any future auth should go over TLS. Free + automatic:

```bash
sudo apt-get install -y caddy
# /etc/caddy/Caddyfile:
#   bot.example.com {
#       # Block dotfiles (.git, .env) and sourcemaps (.map) at the edge
#       @blocked {
#           path */.*
#           path *.map
#       }
#       respond @blocked 404
#
#       reverse_proxy 127.0.0.1:8000
#   }
#
# WARNING: Never configure the proxy to add X-Admin-Token. Operators sign in via POST /api/auth/login (HttpOnly cookie).
#
sudo systemctl reload caddy
```

Point a DNS A record at the VM first. Set `FRONTEND_ORIGIN=https://bot.example.com` in `.env`, then `docker compose up -d` again.

**Network port lock-down (default):**
1. By default, keep ONLY ports 80 and 443 open in the Oracle Security List and iptables.
   - **Port 80 (HTTP)**: Caddy automatically receives HTTP requests on port 80
     and issues a 308 permanent redirect to HTTPS on port 443.
   - **Port 443 (HTTPS)**: Encrypted traffic with TLS 1.3 / 1.2 and automated Let's Encrypt certificates.
2. In `docker-compose.yml`, the port mapping binds only to loopback by default:
   `- "127.0.0.1:${API_PORT:-8000}:8000"`
   This prevents external clients from bypassing TLS by connecting directly to port 8000.

**Nginx alternative (if using Nginx instead of Caddy):**
```nginx
# /etc/nginx/sites-available/bot.conf
server {
    listen 80;
    server_name bot.example.com;
    return 308 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name bot.example.com;

    ssl_certificate /etc/letsencrypt/live/bot.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bot.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    # Deny access to .git, .env, and all hidden files/directories
    location ~ /\. {
        deny all;
        return 404;
    }

    # Deny access to source maps in production
    location ~* \.map$ {
        deny all;
        return 404;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

Do not add a `VITE_ADMIN_TOKEN` value; the dashboard is public JS and must not
contain the operator secret. Operators sign in via the dashboard login form
(`POST /api/auth/login`), which issues a secure HttpOnly session cookie.

**§55 — what Caddy changes about authorization.** Once traffic arrives
through the reverse proxy, every visitor's socket address is loopback
(`127.0.0.1`), so "loopback-only" no longer means "just me". The engine
handles this automatically: any request carrying a forwarding header
(`X-Forwarded-*` / `Forwarded`) is treated as PROXIED, and the real-wallet
surfaces — `/api/live/*`, `/api/holdings`, `/api/stats` — then require logging in
via the dashboard (HttpOnly cookie) or sending the `X-Admin-Token` header (fail-closed
if `ADMIN_TOKEN` is unset or shorter than 32 characters; the panels render empty until authenticated).
The public research surface (feed/proof/disclosure) is unaffected. To deliberately
serve the live book publicly instead, set `LIVE_BOOK_PUBLIC=true` in `.env`.

## Step 8 — Where the FRONTEND lives

**You have two options; the default needs zero extra work:**

1. **Single-origin (recommended, default):** the Docker build already
   compiles the React dashboard and bakes it into the image; Caddy serves it
   at `https://bot.example.com/` (same origin, no CORS, WS just works).
   Before the TLS proxy exists, reach the loopback-bound API through an SSH
   tunnel: `ssh -L 8000:127.0.0.1:8000 ubuntu@<PUBLIC_IP>` then open
   `http://127.0.0.1:8000/`. **Nothing to deploy separately.**
2. **Split deploy (optional):** Vercel Hobby (free) — import the GitHub
   repo, Root Directory = `frontend`, env `VITE_API_BASE_URL=https://<host>`
   (the engine URL), build command `npm run build`, output `dist`. Then set
   the engine's `FRONTEND_ORIGIN=https://<vercel-domain>` and rebuild.
   Notes: Vercel Hobby is nominally non-commercial — Cloudflare Pages is
   the clause-free alternative (same settings). WS always connects straight
   to the engine; Vercel never proxies it. The login session cookie is
   issued `SameSite=None; Secure` automatically when the request Origin is
   a different HTTPS host, so credentialed reads work in split mode; the
   default same-origin deployment stays `SameSite=Lax`.

Since the bot is a single-operator research tool, option 1 is the sane
default: one URL, one service, zero moving parts. Use option 2 only if you
want a vanity domain decoupled from the engine.

## Step 9 — Day-2: stop the local instance, backups, updates

```bash
# On the LAPTOP — one armed cycle only, ever.
# stop.sh waits up to 150s for an in-flight order/ledger write before SIGKILL.
./stop.sh

# On the VM — encrypted state + DB backup via the in-image script.
# It writes into the engine_backups volume (mounted at /app/backups) with
# retention pruning and refuses to write an incomplete archive:
docker compose exec live python /app/scripts/backup_state.py --backup-dir /app/backups

# Whole-volume snapshot (compose project prefix included; list the four
# volumes with `docker volume ls | grep engine_`):
docker run --rm \
  -v trading-bot_engine_state:/s1:ro \
  -v trading-bot_engine_data:/s2:ro \
  -v trading-bot_engine_logs:/s3:ro \
  -v trading-bot_engine_backups:/s4:ro \
  -v /opt/trading-bot:/out alpine \
  tar czf /out/state-backup-$(date +%F).tgz /s1 /s2 /s3 /s4

# Updates:
cd /opt/trading-bot/app && git pull
docker compose up -d --build
```

Kill switch / operator break still work from the VM shell (files under the
`engine_state` volume). Oracle's idle-reclaim can never trigger — the bot
produces CPU + network activity every 15–60 seconds, permanently.

## Step 10 — Operator drills and the first-24h watch list

Run these once on the VM **before** funding more than the micro-bootstrap
amount. They verify the behaviours the container/compose changes rely on;
none of them needs a code change.

### 10.1 Reboot drill (A3)

```bash
sudo systemctl enable docker        # get.docker.com normally does this
systemctl is-enabled docker         # must print: enabled
sudo reboot
```

Wait ~2 minutes, SSH back in, then:

```bash
cd /opt/trading-bot/app
docker compose ps                   # api, live, searxng all healthy
docker compose logs live --tail=30  # live cycle resumed (ARMED banner/cycle lines)
```

`live` is `restart: unless-stopped` (§87 A3), so it returns after a daemon
restart. `init: true` + a 150s `stop_grace_period` give the Python loop time to
finish an in-flight order (§87 B). If `live` is missing or unhealthy after
2 minutes, do **not** re-arm: inspect `docker compose logs live` and the
heartbeat first.

### 10.2 Graceful-stop drill (B)

```bash
time docker compose stop live       # must finish in < 150 s
docker compose logs live --tail=40  # last line: "graceful shutdown complete"
docker compose start live
```

The runner catches SIGTERM/SIGINT, finishes the current cycle (including a
sign/send/ledger write), takes `_EXIT_LOCK` so the fast scanner cannot be
cancelled mid-sell, then closes the provider/thinker. A stop that reaches
150 s and prints no `graceful shutdown complete` means the process was
SIGKILLed — treat that as an incident and inspect the last cycle lines.
A stop that arrives while the cycle is sizing also skips any **new** entry
and logs `entry skipped: shutting down`; exits and management still run, and
an order already sent is never cancelled. The local `./stop.sh` gives the
same 150s window and exits non-zero (with an incident warning) if it has to
SIGKILL a process.

### 10.3 Rollback to the last good tag

```bash
cd /opt/trading-bot/app
git fetch --tags
git tag --sort=-creatordate | head
git checkout <last-good-tag>
docker compose up -d --build
docker compose ps
```

Keep the VM's `.env` and the `secrets/` directory untouched. The state volumes
survive the image rebuild; if the rollback crosses a state-schema change,
restore the matching backup (docs/15) instead of guessing.

### 10.4 Kill-switch manual trip / clear

```bash
# Trip (blocks every order path, including exits):
docker compose exec -w /app/backend live python -m live_execution.scripts.confirm_trade \
  kill "manual drill $(date -u +%FT%TZ)"

# Confirm it is latched:
docker compose exec -w /app/backend live python -m live_execution.scripts.confirm_trade list

# Clear ONLY after checking the book and chain reconciliation:
docker compose exec -w /app/backend live python -m live_execution.scripts.confirm_trade resume
```

A tripped switch is deliberately sticky: it survives restarts and must be
cleared by a human (`resume`). `LIVE_TRADING_ENABLED` is still the outer gate.

### 10.5 First-24h watch list

| Signal | Where to look | What it means / action |
|---|---|---|
| Heartbeat age | `docker compose ps` (live healthy) and `docker compose exec live python -c "import os,time; p='/app/backend/live_execution/state/heartbeat.txt'; print(time.time()-os.path.getmtime(p))"` | Must stay < 300 s. A stale heartbeat raises `HEARTBEAT_STALE`; a hung cycle is surfaced this way, never by cancelling an order. |
| Low USDC / low SOL | Alert channel; `LOW_USDC`, `LOW_SOL` | USDC below the minimum ticket floor or SOL below 2× `MIN_SOL_RESERVE` (default 0.01 SOL). Fund or stop; do not raise risk limits. |
| No fills | Alert channel / `ALERT_NO_FILLS_HOURS` | After 6 h with ≥10 candidates and no fill, the bot alerts. Check whether the gate/model is refusing, not whether to loosen rules. |
| Wallet drop | Alert channel; `WALLET_DROP` | Unexplained outflow above `$5` (alert-only unless the operator enabled the kill-switch option). Reconcile before resuming. |
| Pending buys | `docker compose logs live \| grep -E "pending buy\|LATE BUY ADOPTED"` | A pending row is counted against caps/slots and is never priced/exited. Confirmed late land → `LATE BUY ADOPTED`; expiry needs a provably invalid blockhash, **every** RPC endpoint still reporting the signature unknown, and an age ≥ 90 s; unknown stays pending and alerts after 5 min. Resolve manually only with the engine stopped (§10.6). |
| Unexplained holdings | Alert channel; `UNJOURNALED_HOLDING` | On-chain tokens with no ledger row and no pending buy. Never auto-added; inspect and repair manually. |
| Exit failures | `docker compose logs live \| grep -E "backoff\|EXIT_UNRESOLVED\|risk-off exit"` | Per-mint retry backoff is 15/30/60/120/300 s (risk-off capped at 60 s); repeated failures are expected during a collapse, not a reason to disable the stop. Risk-off exits may accept up to 15% price impact by design (`EXIT_MAX_PRICE_IMPACT_PCT`); entries and profit trims stay at 2.5%. |
| Backup + restore | `docker compose logs live \| grep backup`; docs/15 drill | Run one manual backup and one throwaway-directory restore before trusting cron. |

### 10.6 Manual pending-buy resolution

The cycle resolver handles pending buys automatically. If an operator has
conclusive chain evidence and the engine is stopped (never edit the ledger
while the live cycle is running — there is no cross-process ledger lock):

```bash
# 1. Stop the engine so nothing else rewrites the ledger.
docker compose stop live

# 2. List the unresolved keys (read-only).
docker compose run --rm --entrypoint python live \
  -c "from live_execution.models import ExecutionLedger; from live_execution import config; [print(r.idempotency_key, r.mint, r.signature, r.ts) for r in ExecutionLedger(config.STATE_DIR / 'executions.json').pending_buys()]"

# 3a. Expire: the transaction provably cannot land. Releases caps/slots.
docker compose run --rm --entrypoint python live \
  /app/scripts/resolve_pending_buy.py '<key>' --expire

# 3a-alt. ONLY when the chain reads are unavailable (RPC outage):
# --force-expire skips ONLY the blockhash/age gates. Every endpoint must
# still report the signature absent, and the audit line records forced=true.
docker compose run --rm --entrypoint python live \
  /app/scripts/resolve_pending_buy.py '<key>' --force-expire

# 3b. Adopt: the fill landed; SIG may be omitted to use the stored one.
docker compose run --rm --entrypoint python live \
  /app/scripts/resolve_pending_buy.py '<key>' --adopt-from-sig '<signature>' --yes

# 4. Resume only after the tool reports success.
docker compose start live
```

The tool prints the row and requires typing `expire`/`adopt`; `--yes` is
mandatory when stdin is not a TTY. `--expire` re-verifies the chain before
changing anything: the stored blockhash must be provably invalid, **every**
endpoint must report the signature absent, and the row must be at least
`PENDING_EXPIRY_MIN_AGE_SECONDS` (90 s) old. A still-valid or unknown
blockhash, an RPC error, or a young row is refused with exit 1. A signature
found on any endpoint refuses with `the transaction landed (status ...): use
--adopt-from-sig`. `--force-expire` exists only for an RPC outage and skips
**only** the blockhash/age gates — the all-endpoint-absent check still runs;
forced expiries are audited with `"forced": true`. Adoption reads the
**actual** token/USDC deltas from the confirmed transaction and refuses to
invent a cost basis when they are unreadable. Every attempt appends one JSON
line to `/app/backend/live_execution/state/resolve_pending_buy_audit.log` (or
the configured `--audit-log`). A nonzero exit means nothing changed.

## First 48 hours — supervised burn-in

The engine is armed from the first `up`, so treat the first two days as a
supervised burn-in: every anomaly is handled by **stopping and inspecting**,
never by widening a risk limit or clearing a breaker.

- Fund the wallet with only what you accept losing. Do not top it up to "give
  the strategy room" during the burn-in.
- Keep `docker compose logs -f live` open and confirm the **first heartbeat**
  before walking away.
- Run `send_test_alert.py` and confirm the alert reaches the phone, not just
  the command's exit code.
- Do not raise any hardcoded cap until 48 h pass with a clean reconcile.

| Window | Check | Pass condition |
|---|---|---|
| 0–2 h | `docker compose ps` + `docker compose logs live --tail=100` | api/live healthy; ARMED banner and cycle lines; no traceback |
| 0–2 h | `send_test_alert.py` | one test alert arrives on the configured channel |
| 2–24 h | `docker compose logs live --tail=300 \| grep -E "PENDING BUY\|LATE BUY\|ERROR\|Traceback"` | no unexplained pending rows; any pending row has a signature + blockhash and is handled per §10.6 |
| 2–24 h | docs/15 backup + throwaway restore | backup completes; the restored ledger reconciles with no `MISMATCH` |
| 24–48 h | one operator-approved real buy + sell on scratch state | fill lands, ledger row is confirmed, exit closes, commit is `bound` |
| 48 h | repeat the reboot and graceful-stop drills (§10.1–10.2) | live returns; last line is `graceful shutdown complete`; no order cancelled mid-flight |

Stop and inspect (do not "wait it out") if any of these appear:

- three consecutive cycles with no heartbeat, or a heartbeat older than 300 s;
- a kill-switch trip that was not operator-initiated;
- `WALLET_DROP`, `UNJOURNALED_HOLDING`, or a pending buy older than 5 minutes;
- a commit marked `failed` while the same signature is confirmed on-chain;
- an exit retrying beyond the documented ladder.

At the 48-hour mark, record the observed heartbeat, fill count, pending count,
realized P&L, and any alerts in the operator log, then decide explicitly
whether to keep the current caps. Do not raise caps during the burn-in.

## Quick troubleshooting

| Symptom | Fix |
|---|---|
| `docker compose build` fails on a pip wheel | switch Dockerfile base to `python:3.13-slim` (Step 6 note) |
| `docker compose build` fails installing Chromium/patchright (ARM/QEMU) | rebuild without the browser: `SKIP_BROWSER=1 docker compose up -d --build`. The browser is only a fallback transport for the crowd feed (`backend/config.py` documents that the feed works 100% without a browser), so the bot is fully functional without it. |
| Dashboard unreachable but VM pingable | you skipped ONE of the two firewalls — both the Security List (Console) AND iptables (Step 3) |
| Live cycle "NOT started (no wallet)" | `WALLET_KEYPAIR_PATH` must be `/run/secrets/wallet_keypair` (in-container path) and the compose bind-mount must exist |
| fomo token stops working after a rebuild | `FOMO_PRIVY_STATE_FILE` not pointed at `/app/data/...` (Step 5) |
| Both laptop and VM trading | **stop the laptop one immediately** — exactly one armed cycle may run |
