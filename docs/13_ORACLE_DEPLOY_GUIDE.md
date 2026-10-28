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
FRONTEND_ORIGIN=https://bot.example.com          # after Step 7; before that use http://127.0.0.1:8000 via SSH tunnel
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
- `docker-compose.yml` splits the system into two isolated services from the same image (§77 B2):
  - **`api`**: Runs the dashboard and REST API. Mounts state `:ro` and has **zero wallet secrets**. The dashboard process can run on an unprivileged host with zero key material.
  - **`live`**: Autonomous trade execution engine (`run_live_cycle.py`). Mounts state `rw` and holds the signing key. No ports exposed.
- The keypair must be bind-mounted read-only into the `live` container — under the
  `live` service in `docker-compose.yml`, add:
    `- ./secrets/wallet_keypair.json:/run/secrets/wallet_keypair:ro`
  (the commented block in the compose file already shows this exact mount). Ensure `chmod 600 ./secrets/wallet_keypair.json` (loose permissions are rejected at startup).
- The entrypoint starts the API under `ROLE=api`, and the live cycle under `ROLE=live` because the repo
  is ARMED and the wallet is configured — same double-gate as `start.sh`.

**Verify:**

```bash
docker compose ps                    # api, live, and searxng all healthy
docker compose logs -f live --tail=50     # watch the live cycle: ARMED banner, cycle lines
curl http://127.0.0.1:8000/api/health      # returns {"status":"ok"}
```

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
# On the LAPTOP — one armed cycle only, ever:
./stop.sh

# On the VM — encrypted state + DB backup via the in-image script.
# It writes into the engine_backups volume (mounted at /app/backups) with
# retention pruning and refuses to write an incomplete archive:
docker compose exec live python scripts/backup_state.py --backup-dir /app/backups

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

## Quick troubleshooting

| Symptom | Fix |
|---|---|
| `docker compose build` fails on a pip wheel | switch Dockerfile base to `python:3.13-slim` (Step 6 note) |
| Dashboard unreachable but VM pingable | you skipped ONE of the two firewalls — both the Security List (Console) AND iptables (Step 3) |
| Live cycle "NOT started (no wallet)" | `WALLET_KEYPAIR_PATH` must be `/run/secrets/wallet_keypair` (in-container path) and the compose bind-mount must exist |
| fomo token stops working after a rebuild | `FOMO_PRIVY_STATE_FILE` not pointed at `/app/data/...` (Step 5) |
| Both laptop and VM trading | **stop the laptop one immediately** — exactly one armed cycle may run |
