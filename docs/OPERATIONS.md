# Operations and recovery

Reviewed **2026-11-14**. This is the routine diagnosis/recovery entry point.
Use [03_DEPLOYMENT.md](03_DEPLOYMENT.md) for host/container setup,
[05_ORACLE_DEPLOY_GUIDE.md](05_ORACLE_DEPLOY_GUIDE.md) for the Oracle path, and
[10_SECURITY.md](10_SECURITY.md) for incidents and credential rotation.
Commands below are examples to run in the appropriate environment, not
evidence that production was checked.

## Service and state ownership

| Component | Responsibility | Persistent storage |
|---|---|---|
| `api` | Dashboard, REST/WebSocket, authorized ingestion and SQL maintenance | Data/log volumes; live authority mounted read-only. |
| `live` | Candidate cycles, exit scanner, signing, ledger and reconciliation | Live state, data, logs and backups; wallet secret mount read-only. |
| `searxng` | Optional local web-search transport | Configured sidecar settings; no signer authority. |

Keep one API worker per state directory. Limits, some revocations/bot state
and caches have in-process assumptions; adding replicas needs an explicit
shared-state design. Keep the SQL mirror and execution authority distinct
during maintenance.

## Routine observation

```bash
docker compose ps
curl --fail --silent --show-error http://127.0.0.1:8000/api/health
docker compose logs --since 30m live
docker compose logs --since 30m api
```

`/api/health` returns only `{"status":"ok"}`. It proves API responsiveness,
not a healthy live cycle, provider quorum, current balances or successful
fills. Check engine heartbeats, system status, safety and recent execution
outcomes separately. Read private endpoints using an operator session/token
or direct loopback; do not inject credentials into a reverse proxy or URL.

For current private consensus telemetry on the Compose deployment:

```bash
docker compose exec live python /app/scripts/consensus_status.py
```

For a local/copy directory:

```bash
.venv/bin/python scripts/consensus_status.py --state-dir /tmp/trading-state-copy
```

The snapshot is `live-process` scope, includes written/boot timestamps and
resets on live restart. Exit 0 means fresh/valid, 1 invalid/stale, 2 unreadable
or missing. Freshness uses twice the supplied cycle interval; zero counters
do not establish that simulation ran. Cross-check structured logs
`SIM_CONSENSUS_ATTEMPT` / `SIM_CONSENSUS_REFUSED` for history across restarts.

## Symptom-to-action map

| Symptom | Inspect | Next step |
|---|---|---|
| Health is OK but tape stopped | Live process, heartbeat age, stage logs | Diagnose the runner/provider stage; restarting API alone cannot resume cycles. |
| Feed continues, no fills | `/api/funnel`, refusals, live commits and records | Separate gate refusals, model vetoes and execution failures before changing strategy parameters. |
| Private panels show 403 | Session expiry, ADMIN_TOKEN validity, forwarding headers, frontend origin | Log in again or correct auth/proxy settings; do not expose the book to fix login. |
| Repeated 429 | Rate tier, client IP trust, polling load, provider errors | Honor retry/backoff and correct configuration; avoid rapid retry loops. |
| Cash or equity unavailable | `cash_status`, `equity_status`, `missing`, RPC/price reads | Restore observations; preserve null values rather than entering fabricated balances. |
| Position differs from wallet | Chain-scan freshness, ledger, fill signatures, manual wallet activity | Reconcile confirmed chain evidence and accounting; the SQL mirror is not repair authority. |
| Sell remains pending | Stored attempt/signature, age/blockhash and endpoint observations | Resolve the existing attempt; do not clear its marker or submit a replacement manually. |
| Guard/consensus refuses | Refusal category, payer/intent identity, independent RPC observations | Correct the cause while retaining policy/simulation checks. |
| State unreadable or invalid | Read-only state validator on copied files | Preserve original history and repair through documented validated tools. |
| Backup/rotation fails | Local logs, mount ownership/space, backup key separation | Fix persistence before relying on restart or restore. |

Reports such as `scripts/gate_funnel_report.py`, `scripts/perf_report.py` and
`scripts/spend_projection.py` provide bounded diagnosis. Review their `--help`
and point them at the intended database/configuration. Neither a public refusal
rate nor a past backtest is evidence that a control should be weakened.

## Stop controls

A **manual** kill-switch trip blocks buys and sells. The recognized automatic
realized-daily-loss breaker blocks new entries while permitting de-risking
exits. Entry breaks and spent research/model budgets are separate controls.
The dashboard reports these states; it does not clear or set them.

Use [kill_switch.py](../backend/live_execution/kill_switch.py) and the
[security incident runbook](10_SECURITY.md#6-incident-runbook-suspected-compromise)
for the intended operator action. Before clearing a latch, reconcile pending
operations and confirm the reason is resolved. Do not edit hardcoded flags as
part of routine automated recovery. For process stops, preserve the live
150-second and API 30-second Compose grace windows so accounting can finish.

## Pending transaction recovery

1. Preserve ledger/commit files and the known signature; inspect a copied state
   directory before mutating accounting.
2. Query the stored attempt across the required independent endpoints.
   Unknown/timeout/disagreement is unresolved, not failed.
3. Let the normal resolver adopt a confirmed fill or release an expired attempt
   only when its positive-evidence requirements pass. Sell expiry release
   requires endpoint absence, finalized expiry, signing age and a delayed
   second observation; legacy missing metadata cannot be guessed.
4. For operator recovery, use the purpose-built tools. They do not sign/send a
   replacement trade. Read help first:

```bash
.venv/bin/python scripts/resolve_pending_buy.py --help
.venv/bin/python scripts/resolve_pending_sell.py --help
```

`resolve_pending_sell.py list` is the read-only inspection command (it can
query RPC). `confirm`/`release` mutate accounting/attempt state and require
explicit identity plus an interactive typed action. Release additionally
requires the never-landed attestation. Buy expiry/adoption has its own
evidence and confirmation rules; do not transfer sell flags to the buy tool.

Already-accounted closes settle without duplicating proceeds. Refused or
repairable accounting keeps history/pending state intact. Detailed behavior
and operator constraints are in the
[unresolved-exit runbook](10_SECURITY.md#94-recovering-an-unresolved-exit) and
[behavior-change map](12_BEHAVIOUR_CHANGES_SINCE_3FEA445.md).

## Upgrade and rollback

Before replacing an image/source version:

1. Record the deployed commit/image and preserve a validated encrypted backup
   of matching JSON/SQL state. Preserve a rollback copy outside tracked source.
2. Run the required [test gates](TEST_PLAN.md), deployment preflight and
   read-only validation of a copied state directory:

```bash
.venv/bin/python scripts/preflight_deploy.py
.venv/bin/python scripts/validate_state.py --state-dir /tmp/trading-state-copy
```

3. Correct invalid state/configuration before recreating services. Run the
   deployment guide's keypair-free guard diagnostic in the intended network
   environment and verify writable-volume/read-only-root contracts.
4. After restart, inspect role health, heartbeat, state readability, pending
   signatures, private portfolio, alerts and current guard refusals.

A code rollback is not an accounting rollback. Do not replace a newer ledger
with an older copy after transactions may have landed. Reconcile chain truth
and retain schema/state compatibility evidence before resuming operation.

## Backups and restore

Current [backup_crypto.py](../scripts/backup_crypto.py) writes a **v2 scrypt +
AES-256-GCM** authenticated envelope. Restore also accepts legacy Fernet
archives. Early sections of [07_BACKUP_RESTORE.md](07_BACKUP_RESTORE.md) retain
historical Fernet terminology; consult the current crypto implementation and
the [security backup notes](10_SECURITY.md#7-backup-encryption).
Keep backup encryption material distinct from secret-store material and off
the repository. Normal archives exclude wallet/secret-key/sidecar material;
they are not wallet-key recovery backups.

Use [backup_state.py](../scripts/backup_state.py),
[restore_state.py](../scripts/restore_state.py) and the deployment wrapper as
documented. Perform restores into scratch targets first: verify authentication,
archive paths, JSON validation, SQLite integrity and reconciliation. Restore
forces a kill-switch latch; it is not automatic authorization to trade.
For a Supabase book, maintain a separate remote-database backup/restore process;
the local SQLite archive is not a backup of hosted Postgres.

No production restore success, measured RPO/RTO or offsite delivery is implied
by local tests. Record actual drill results and current outstanding checks.
