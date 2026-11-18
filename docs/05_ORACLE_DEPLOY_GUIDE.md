# 05 — Oracle deployment guide

This runbook deploys the API/dashboard, live engine and internal SearXNG
sidecar on one Ubuntu ARM VM. Read [deployment architecture](03_DEPLOYMENT.md)
and [security controls](10_SECURITY.md) first. Commands are for the operator;
run them on the VM unless marked **laptop**.

The repository's trading switch is already armed. Starting `live` with its
wallet configured starts real execution. Keep exactly one engine per wallet,
including laptops, old VMs and maintenance containers. This guide does not
change the human-controlled arming or confirmation flags.

## 1. Provision the VM and restrict ingress

Use Ubuntu 24.04 ARM64 on `VM.Standard.A1.Flex`, initially **2 OCPUs / 12 GB**.
Oracle's [Always Free documentation](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
currently lists 1,500 OCPU-hours and 9,000 GB-hours monthly, a 47 GB minimum
boot disk and 200 GB total free block storage. Verify the tenancy's remaining
allowance and Console cost estimate before provisioning; other instances and
volumes count too. Free capacity is regional and can be unavailable.

In the instance's VCN security list or attached NSG:

- Permit TCP 22 from your administration IP/CIDR.
- Permit TCP 80 and 443 for the HTTPS reverse proxy.
- Keep TCP 8000, SearXNG 8080 and database ports closed externally.
- Apply equivalent IPv6 policy if the VM has public IPv6.

The VM firewall must allow the same intended traffic. Inspect the existing
rules before changing them; keep the current SSH connection open and verify
a second SSH connection before closing it. On Oracle Ubuntu images using
iptables and `netfilter-persistent`, add the web rules before a rejecting
INPUT rule and save them. Do not flush the ruleset or rely on a fixed rule
number copied from another VM. Docker publishes the API on **127.0.0.1 only**;
keep that binding even when using a host firewall.

## 2. Install the supported host tools

```bash
ssh -i <ssh-private-key> ubuntu@<public-ip>
sudo apt-get update
sudo apt-get -y upgrade
sudo apt-get install -y ca-certificates curl git python3 python3-cryptography
```

Install Docker Engine and the Compose plugin using Docker's
[Ubuntu apt repository procedure](https://docs.docker.com/engine/install/ubuntu/#install-using-the-apt-repository).
Ubuntu 24.04 and ARM64 are supported. Use the signed package repository;
the convenience installer is intended for testing/development.

```bash
sudo systemctl enable --now docker
systemctl is-enabled docker
docker compose version
```

Use `sudo docker` until you deliberately grant your operator account Docker
access. Membership in the `docker` group grants root-equivalent host access.
If you add `ubuntu` to that group, reconnect before continuing. The commands
below assume that account can run Docker.

```bash
sudo usermod -aG docker ubuntu
# Reconnect the SSH session to apply this deliberate privilege grant.
```

## 3. Install a reviewed release and prepare secrets

```bash
sudo install -d -m 0750 -o ubuntu -g ubuntu /opt/trading-bot
cd /opt/trading-bot
git clone <repository-url> app
cd app
git checkout --detach <reviewed-commit-or-tag>
git rev-parse HEAD
umask 077
cp .env.example .env
mkdir -m 0700 secrets
```

Deploy a commit containing the reviewed fixes, after its CI passes. Record
its commit and the last working image ID outside the container. If cloning
is unavailable, transfer a **tracked-source archive** (`git archive`) of that
release. Do not recursively copy a working directory containing wallet
files, local `.env`, databases, state, logs or browser caches.

Prepare `.env` privately on the VM. Copy the wallet over SSH into the
`secrets` directory; do not paste it into a terminal command or repository.
Then set its container ownership:

```bash
sudo chown 10001:10001 secrets/wallet_keypair.json
sudo chmod 0600 secrets/wallet_keypair.json
sudo chown ubuntu:ubuntu secrets
sudo chmod 0700 secrets
chmod 0600 .env
```

The image runs as uid **10001**. File ownership survives bind mounting, so
host-readable `ubuntu:ubuntu`/0600 wallet files are unreadable inside `live`.
Keep the host directory owned by the operator so host preflight can inspect
file metadata; Docker mounts the file itself into the container. Preflight
does not print or read the wallet contents.

## 4. Configure the deployed environment

Use plain `KEY=value` lines and fill the values in a private editor. Do not
put secrets in `VITE_*` variables: those become public JavaScript.

```ini
DATA_BACKEND=live
FRONTEND_ORIGIN=https://bot.example.com
DEBUG=false
FORCE_HTTPS=true
RATE_LIMIT_ENABLED=true
LIVE_BOOK_PUBLIC=false
WALLET_KEYPAIR_PATH=/run/secrets/wallet_keypair
WALLET_KEYPAIR_HOST_PATH=/opt/trading-bot/app/secrets/wallet_keypair.json
WALLET_KEYPAIR_JSON=
EXPECTED_WALLET_ADDRESS=<expected-public-wallet-address>
SEARXNG_URL=http://searxng:8080
```

Generate **independent** random `ADMIN_TOKEN`, `SESSION_SECRET` and
`SEARXNG_SECRET` values (`openssl rand -hex 32` generates one). Operator and
session credentials require at least 32 characters and 12 distinct characters;
the session secret must differ from the operator token. Generate a separate
Fernet-format `BACKUP_ENCRYPTION_KEY`, store a recovery copy off the VM, and
keep it distinct from `SECRET_STORE_KEY`:

```bash
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Configure the selected main LLM key, two RPC URLs on different provider
hosts, and an alert channel. Never use a predictable public ntfy topic.
Different RPC hostnames are a local sanity check; independently verify that
they actually represent separate providers. Review configured spend caps with
`scripts/spend_projection.py`; external services and API quotas are separate
from the VM allowance.

Optional CAPTCHA: `CAPTCHA_ENABLED=true` supports `internal`, `turnstile` or
`hcaptcha`. External widgets require public `CAPTCHA_SITE_KEY` and private
`CAPTCHA_SECRET_KEY`; use that provider's verification URL (`https://api.hcaptcha.com/siteverify`
for hCaptcha). Register the real dashboard hostname with the provider. The
login form fetches its public configuration and submits the solved token;
server verification remains mandatory. See the
[Turnstile](https://developers.cloudflare.com/turnstile/get-started/client-side-rendering/)
and [hCaptcha](https://docs.hcaptcha.com/) setup instructions.

Compose selects persistent runtime paths explicitly:

| Data | Deployed location | Storage/access |
| --- | --- | --- |
| Execution/commit/kill/break state | `/app/backend/live_execution/state` | `engine_state`; API read-only |
| Rotated Privy sidecar and encryption key | `state/.fomo_privy.json`, `state/.secret_store.key` | signer-only writes; excluded from normal backups |
| Blocklist loss memory | `state/blocklist_state.json` | signer-only writes |
| SQLite book | `/app/data/trading_bot.db` | `engine_data` |
| API session revocations/knowledge/spend | `/app/data` | `engine_data` |
| Logs | `/app/logs` | `engine_logs` |
| Encrypted backup archives | `/app/backups` | `engine_backups`; live service only |

Do not move the Privy sidecar to `/app/data`: Compose deliberately keeps it
with the signer state. Keep separate secure recovery arrangements for the
wallet, `.env` and secret-store credentials; the normal state archive excludes
them. For Supabase, apply **all migrations 001–007** in order before enabling
`USE_SUPABASE_DB=1`, verify the resulting role grants, and arrange database
backups separately. Prefer an independently obtained certificate fingerprint
if using certificate pinning; a changed pin needs operator review.

## 5. Preflight, build and start the dashboard first

```bash
cd /opt/trading-bot/app
python3 scripts/preflight_deploy.py
# Continue only after every FAIL is fixed and every WARN is understood.
docker compose build
# The committed engine is armed: intentionally leave live stopped here.
docker compose up -d searxng api
docker compose ps
curl --fail http://127.0.0.1:8000/api/health
```

Run full preflight **on the host**: it needs `.env`, Compose rendering and
host wallet metadata. It checks rendered service environments, not an assumed
shell override. It does not establish cloud ingress, provider availability,
funding, applied database grants or named-volume state validity. `--skip-compose`
is a debugging option and is not a deployment gate. Do not print unrestricted
`docker compose config` output: the rendered environment contains secrets.

Build natively on ARM. If the optional Chromium layer fails, use
`SKIP_BROWSER=1 docker compose build` and record that browser fallback is
unavailable. Do not change the pinned Python base to another ABI as an ad-hoc
wheel workaround. CI builds ARM64 with the browser layer skipped and checks
actual read-only-root writes; the full browser layer still needs native VM
validation if enabled.

Before a public hostname is ready, access the dashboard from the **laptop**
with an SSH tunnel and a temporary loopback `FRONTEND_ORIGIN`:

```bash
ssh -N -L 8000:127.0.0.1:8000 -i <ssh-private-key> ubuntu@<public-ip>
```

Open `http://127.0.0.1:8000` locally through that tunnel. Operator login sends
the token and establishes a session cookie; public plaintext HTTP exposes
those credentials. Use HTTPS before public operator access.

## 6. Configure HTTPS and verify access control

Point the DNS record at the VM. Install Caddy from an appropriate signed
package source and configure `/etc/caddy/Caddyfile`:

```caddyfile
bot.example.com {
    @blocked path */.* *.map
    respond @blocked 404
    reverse_proxy 127.0.0.1:8000
}
```

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
```

Set `FRONTEND_ORIGIN=https://bot.example.com` and recreate **only** the API:

```bash
python3 scripts/preflight_deploy.py
docker compose up -d api
```

Never configure the proxy to insert `X-Admin-Token`. Preserve Caddy's normal
forwarded-header handling. Application `TRUSTED_PROXY_CIDRS` and Uvicorn
`FORWARDED_ALLOW_IPS` are separate settings; if topology requires overrides,
configure both for the actual proxy peers. Do not trust an entire VCN or all
addresses merely because the VM is on Oracle. The default host-Caddy/Compose
loopback arrangement needs no added `10.0.0.0/8` trust.

From your browser, verify HTTPS, successful login, logout and rejection of
private wallet/book views after logout. Public research/proof/feed endpoints
are intentionally public. Confirm that 8000 and 8080 are unreachable from
outside the host. These are operator deployment checks; the source audit does
not claim they have already happened.

## 7. Handoff existing state before starting the signer

On the **laptop/old host**, stop the old engine and wait for its graceful
shutdown. Create an encrypted backup of its current state/book using
[backup and restore](07_BACKUP_RESTORE.md). A new named volume does not
contain the existing positions, pending trades, loss baselines or kill switch.
Do not start a funded wallet against a fresh book while those records exist.

For a migration, restore the encrypted archive into the new persistent
volumes using that runbook with the engine stopped. Keep the restore-induced
kill switch latched until reconciliation is reviewed. Do not use `down -v`.
Use the same Compose project/directory for later commands so a different
project name does not silently select fresh volumes.

With `live` still stopped, validate its actual mounted state:

```bash
docker compose run --rm --no-deps --entrypoint python live \
  /app/scripts/validate_state.py --state-dir /app/backend/live_execution/state
```

This explicitly replaces the entrypoint and starts no engine. Inspect pending
buys and sells, verify the expected wallet and available reserves, and run the
documented devnet drill with a **separate disposable wallet/configuration**
before using the changed execution code with mainnet funds. The devnet drill
sends devnet transactions; do not run it with the production signing setup.
It does not test real Jupiter swap routes or establish mainnet liquidity.

After those gates, and only when no other engine is running:

```bash
docker compose up -d live
docker compose ps
docker compose logs --tail=100 live
```

Expect `api` and `live` healthy and `searxng` running (the sidecar has no
healthcheck). The first valid live heartbeat must appear; it must remain
less than 300 seconds old. A healthy API alone does not prove live execution
or provider connectivity. If a restore left the kill switch latched, review
the book/chain discrepancy report before explicitly resuming.

## 8. Prove alerts and backups, then supervise

These commands intentionally contact the configured notification service:

```bash
docker compose exec -T live python /app/scripts/send_test_alert.py
# Observe the test notification at the intended recipient.
docker compose exec -T live python /app/scripts/backup_state.py --backup-dir /app/backups
umask 077
mkdir -p /opt/trading-bot/encrypted-backups
docker compose cp live:/app/backups/. /opt/trading-bot/encrypted-backups/
```

Only encrypted archives belong in normal offsite replication. Do not make an
unencrypted tar of the state/data volumes: they include runtime credentials
and private data. Perform a throwaway-directory restore drill with the saved
backup key and compare restored book/state before trusting scheduled backups.
The default backup wrapper is `deploy/backup.sh`; keep its cron output private
and rotated. Supabase requires its own database recovery procedure.

During initial supervised operation, watch heartbeat age, pending reservations,
provider/auth failures, exit failures, refusal counts, available SOL/USDC,
spend caps and backup outcomes. An unresolved signature retains its reservation;
never mark it failed simply to release capacity. Use `resolve_pending_buy.py`
or `resolve_pending_sell.py` only with the engine stopped and according to
[operations](OPERATIONS.md). Inspect help before using an operator override.

Test graceful stop before unattended operation:

```bash
time docker compose stop live
docker compose logs --tail=50 live
# Require "graceful shutdown complete"; investigate a forced 150-second stop.
docker compose start live
```

Test one supervised VM reboot and confirm Docker and the intentionally running
services return. Oracle may reclaim idle Always Free instances according to
its documented utilization thresholds; periodic network traffic is no
availability guarantee. Keep encrypted offsite backups and monitor the VM.

## 9. Upgrade and roll back without losing authority

Prepare a reviewed target commit and retain the current image before updating.
Do not `git pull` an unreviewed moving branch into an armed running deployment.

```bash
cd /opt/trading-bot/app
docker compose stop live
docker compose logs --tail=50 live
# If graceful shutdown did not finish, investigate pending signatures first.
docker compose run --rm --no-deps --entrypoint python live \
  /app/scripts/backup_state.py --backup-dir /app/backups
docker image tag trading-bot:latest trading-bot:previous
git fetch --tags
git checkout --detach <reviewed-target-commit-or-tag>
python3 scripts/preflight_deploy.py
docker compose build
docker compose run --rm --no-deps --entrypoint python live \
  /app/scripts/validate_state.py --state-dir /app/backend/live_execution/state
docker compose up -d searxng api
# Verify dashboard/auth and review state compatibility before starting live.
docker compose up -d live
docker compose ps
```

For rollback, stop `live`, preserve the current state and encrypted backup,
return the source to the last reviewed compatible release, and restore its
retained image tag to `trading-bot:latest`. Start with `docker compose up -d --no-build`.
If schemas changed, follow the backup/restore procedure rather than letting
older code reinterpret newer state. State volumes survive container recreation;
never remove them as a troubleshooting shortcut.

## Deployment gate

Deployment readiness requires passing CI and host preflight, mounted-state
validation, reviewed migration/one-engine handoff, HTTPS/access checks,
provider/RPC verification, a separate devnet drill, observed alert delivery,
an encrypted backup/restore drill, and supervised stop/reboot checks. The
repository supplies these controls; their successful execution on your Oracle
VM must be observed before unattended real-money operation.
