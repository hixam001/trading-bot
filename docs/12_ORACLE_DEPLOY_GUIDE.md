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
sudo usermod -aG docker ubuntu && exit
ssh -i <your-key> ubuntu@<PUBLIC_IP>   # re-login to pick up the docker group
docker run --rm hello-world           # sanity check
```

## Step 3 — Open the network (BOTH layers — the classic Oracle trap)

Oracle VMs have TWO firewalls. Opening only one gets you timeouts.

1. **Console side:** Networking → VCN → your VCN → Security Lists → Default
   Security List → **Add Ingress Rules**:
   - `0.0.0.0/0`, TCP **8000** (engine/dashboard)
   - `0.0.0.0/0`, TCP **443** (for HTTPS in Step 7)
   - (22 should already exist for SSH)
2. **VM side (Ubuntu images ship blocking iptables rules):**

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8000 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

## Step 4 — Get the code + secrets onto the VM

```bash
# On the VM:
mkdir -p /opt/trading-bot/secrets && cd /opt/trading-bot

# EITHER clone (if the repo is reachable from the VM):
git clone <your-repo-url> app && cd app

# OR from the laptop (excludes venv/logs; .env handled next step):
#   rsync -av --exclude .venv --exclude logs --exclude .run \
#     --exclude 'trading_bot.db' ./ ubuntu@<PUBLIC_IP>:/opt/trading-bot/app/
```

Secrets (NEVER commit these; they live only on the VM):

```bash
# From the laptop — the .env and the wallet keypair:
scp .env ubuntu@<PUBLIC_IP>:/opt/trading-bot/app/.env
scp /home/hixam/.config/solana/mainnet-wallet.json \
    ubuntu@<PUBLIC_IP>:/opt/trading-bot/secrets/wallet_keypair.json
# then on the VM: chmod 600 both files
```

## Step 5 — Edit the VM's `.env` for server mode

Four lines differ from your laptop's `.env` (edit on the VM):

```ini
FRONTEND_ORIGIN=http://<PUBLIC_IP>:8000        # or https://bot.example.com after Step 7
WALLET_KEYPAIR_PATH=/run/secrets/wallet_keypair  # the container mount, not the host path
FOMO_PRIVY_STATE_FILE=/app/data/.fomo_privy.json # rotated Privy token must survive
DB_PATH=/app/data/trading_bot.db                 # already forced by compose, keep anyway
```

`FOMO_PRIVY_STATE_FILE` matters: Privy **rotates** the refresh token and the
bot persists the fresh one; on the laptop it wrote to the repo root — in a
container that filesystem dies on every rebuild. Point it at the
`engine_data` volume (`/app/data`) so the auth chain survives rebuilds.

## Step 6 — Run the engine

```bash
cd /opt/trading-bot/app
docker compose up -d --build
```

- The image builds ON the VM (ARM). Expect 5–10 min the first time.
- The keypair must be bind-mounted read-only into the container — under the
  engine service in `docker-compose.yml`, add:
    `- ./secrets/wallet_keypair.json:/run/secrets/wallet_keypair:ro`
  (the commented block in the compose file already shows this exact mount).
- The entrypoint starts the API always, and the live cycle because the repo
  is ARMED and the wallet is configured — same double-gate as `start.sh`.

**Verify:**

```bash
docker compose ps                    # both healthy
docker compose logs -f --tail=50     # watch the first tick: ARMED banner, cycle lines
curl http://127.0.0.1:8000/api/system-status | head -c 400
```

Then from your laptop: `http://<PUBLIC_IP>:8000/` — the dashboard.

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
#       reverse_proxy 127.0.0.1:8000
#   }
sudo systemctl reload caddy
```

Point a DNS A record at the VM first. Set
`FRONTEND_ORIGIN=https://bot.example.com` in `.env` and
`docker compose up -d` again. (Once Caddy fronts it, remove the public 8000
ingress rule and keep only 443.)

**§55 — what Caddy changes about authorization.** Once traffic arrives
through the reverse proxy, every visitor's socket address is loopback
(`127.0.0.1`), so "loopback-only" no longer means "just me". The engine
handles this automatically: any request carrying a forwarding header
(`X-Forwarded-*` / `Forwarded`) is treated as PROXIED, and the real-wallet
surfaces — `/api/live/*`, `/api/holdings`, `/api/stats` — then require the
`X-Admin-Token` header (fail-closed if `ADMIN_TOKEN` is unset; the panels
render empty until the token is sent). The public research surface
(feed/proof/disclosure) is unaffected. To deliberately serve the live book
publicly instead, set `LIVE_BOOK_PUBLIC=true` in `.env`.

## Step 8 — Where the FRONTEND lives

**You have two options; the default needs zero extra work:**

1. **Single-origin (recommended, default):** the Docker build already
   compiles the React dashboard and bakes it into the image; the engine
   serves it at `http://<PUBLIC_IP>:8000/` (same origin, no CORS, WS just
   works). **Nothing to deploy separately.**
2. **Split deploy (optional):** Vercel Hobby (free) — import the GitHub
   repo, Root Directory = `frontend`, env `VITE_API_BASE_URL=https://<host>`
   (the engine URL), build command `npm run build`, output `dist`. Then set
   the engine's `FRONTEND_ORIGIN=https://<vercel-domain>` and rebuild.
   Notes: Vercel Hobby is nominally non-commercial — Cloudflare Pages is
   the clause-free alternative (same settings). WS always connects straight
   to the engine; Vercel never proxies it.

Since the bot is a single-operator research tool, option 1 is the sane
default: one URL, one service, zero moving parts. Use option 2 only if you
want a vanity domain decoupled from the engine.

## Step 9 — Day-2: stop the local instance, backups, updates

```bash
# On the LAPTOP — one armed cycle only, ever:
./stop.sh

# On the VM — state backup (Supabase already holds the book remotely;
# these volumes hold the live-execution state + rotated Privy token):
docker run --rm -v trading-bot_engine_state:/s1 -v trading-bot_engine_data:/s2 \
  -v /opt/trading-bot:/out alpine tar czf /out/state-backup-$(date +%F).tgz /s1 /s2

# Updates:
cd /opt/trading-bot/app && git pull
docker compose up -d --build
```

Kill switch / operator break still work from the VM shell (files under the
`engine_state` volume). Oracle's idle-reclaim can never trigger — the bot
produces CPU + network activity every 15–60 seconds, permanently.

## Quick troubleshooting

| Symptom | Fix |
|---|---|
| `docker compose build` fails on a pip wheel | switch Dockerfile base to `python:3.13-slim` (Step 6 note) |
| Dashboard unreachable but VM pingable | you skipped ONE of the two firewalls — both the Security List (Console) AND iptables (Step 3) |
| Live cycle "NOT started (no wallet)" | `WALLET_KEYPAIR_PATH` must be `/run/secrets/wallet_keypair` (in-container path) and the compose bind-mount must exist |
| fomo token stops working after a rebuild | `FOMO_PRIVY_STATE_FILE` not pointed at `/app/data/...` (Step 5) |
| Both laptop and VM trading | **stop the laptop one immediately** — exactly one armed cycle may run |
