# System Patterns — trading-bot

## Architecture
Single-process app: FastAPI serves API + WS + built React dashboard on :8000;
`run_live_cycle.py` drives the live trading loop as a separate async process.
SQLite in WAL mode (default) or Supabase Postgres via asyncpg.
`./start.sh` orchestrates the app; `./stop.sh` reverses.

## The core pattern: separate deciding from explaining
- rule_engine/rules.py — 10 pure functions → RuleResult(id, passed, detail
  with real numbers, value)
- rule_engine/gate.py evaluate_gate() — runs ALL rules unconditionally (no
  short-circuit); all_passed = AND = the entire eligibility decision
- llm/thinker.py — runs ONLY on candidates that passed the gate; required
  post-gate veto: entry = all_rules_pass AND model `buy`. A provider failure
  forces `pass` (no entry). The reference-style brain (`llm_brain.py`) maps
  onto the same veto.
- llm/narrator.py — post-close reflection and grounded thesis validation;
  prompt forbids inventing checks; flags recorded

## Key patterns
1. **Atomicity:** the live `ExecutionLedger` rewrites its JSON through a
   temp file + `os.replace()` (atomic on POSIX) and refuses to trade on a
   corrupt/unreadable file; idempotency keys are checked before any send.
   The trades table mirror is observability only — the ledger is the money
   authority. Operator CLIs are expected to run with the bot stopped; there
   is no cross-process lock on the ledger (documented, not accidental).
2. **Fail closed:** provider fields missing → None → numeric rules FAIL
   ("unavailable"); security None → PASS (unknown ≠ unsafe). Never coerce.
3. **Once-per-tick regime:** compute_market_regime() over the whole batch,
   one market_regime row per tick. `market_regime_ok` is retired from the
   active gate; the verdict is journaled as market context, not an entry
   blocker.
4. **Provider discipline:** one shared httpx.AsyncClient; bounded retries;
   distinct 429 backoff + counters; 401/403 non-retryable ProviderAuthError;
   token_security session-disables itself after first auth failure.
5. **Decimals-aware pricing:** Jupiter quoted with 10**decimals raw units;
   refuses without decimals (regression-tested against a real 1000× bug).
6. **Grounding validation:** thesis checked against rule-derived vocabulary,
   invented rule-ids, and number echo; flags recorded on feed events.
7. **Read-only boundary:** promotion gate + portfolio/safety API endpoints
   report state. The only mutations are auth login/logout, KB ingestion, and
   `POST /api/admin/reset` (requires `X-Admin-Token`); no endpoint can open,
   close, or alter a trade.
8. **Exit engine (§5.2):** 6 rules — stop (−20%), HWM trail (50%/40pp),
   liquidity break (<$8k), invalidation (−25%&1.4×sells), stale (14d),
   TP ladder (+100/300/900 trims). Dedicated 15s fast exit scanner to avoid
   stop-loss gap risk.
9. **Failure contract discipline (§82 Phase A):** when changing a function's failure
   contract (e.g. from returning `None` on failure to raising an exception such as
   `SendRejected`), grep every single caller in the codebase to ensure all sites handle
   the new exception type. Enforced statically via AST guard tests (`test_every_send_call_site_handles_send_rejected`).
10. **Hermetic network isolation (§82 Phase G):** tests must never depend on real
    network calls or external RPC endpoints. Enforced session-wide via an autouse socket
    patch in `conftest.py` that blocks non-loopback traffic unless explicitly marked with
    `@pytest.mark.network`. All tests mock `solana.rpc`, `is_blockhash_valid`, and balance queries.
11. **Empty env means unset (§83 Phase A):** never resolve path/identity env vars with
    `os.getenv(key, default)` — a set-but-empty value returns `""`, defeats the default,
    and `Path("")` is `.` (the repo root). Always use `os.getenv(key) or <default>`
    (the §35 `STATE_DIR` lesson generalized). Pinned by `test_state_path_colocation.py`
    including a static guard that `.env.example` ships no blank state overrides.
12. **Config is the single source of truth for derived paths (§83 Phase G):** operational
    scripts must resolve paths the way the app does (`config.DB_PATH`, `LIVE_STATE_DIR`,
    …) and load `.env`; a CWD-relative fallback silently reads/writes the wrong file.
    A backup that cannot find the book must refuse loudly (`database file not found`),
    never produce a success-looking archive without it.
13. **SQLite lineage discipline (§83 Phase H):** `api/db.py` runs WAL mode, so raw-copying
    the main DB file drops committed-but-not-checkpointed rows; snapshot through SQLite's
    `Connection.backup()` instead. When promoting or rolling back a DB file, delete stale
    `-wal`/`-shm`/`-journal` sidecars first — otherwise SQLite may recover a foreign WAL
    against the restored file.
14. **Frontend stream recovery (§83 Phases E–F):** WebSocket consumers re-hydrate REST
    state on every (re)open, not just mount — a backend restart or network blip must not
    silently drop events until a manual reload. Modal overlays (`aria-modal`) take focus
    on mount and trap Tab inside for as long as they are open.
15. **Seal time is the journal truth (§84-A):** `OrderResult.commit_sealed_at` carries
    the CommitLog seal timestamp; `decision_commits.created_at`/`tick_ts` must use it,
    never insert time. A fill stamped later than its own `blockTime` fails the
    independent time-ordering proof.
16. **Unknown is not a retryable expiry (§84-E):** an empty/unparseable previous
    blockhash means the prior signed exit cannot be proven dead — keep the position
    `exit_unresolved` rather than escalating the ladder into a possible double fill.
    Same rule for absent decimals/feeds: resolve or refuse, never synthesize.
17. **Per-attempt provider clients close in `finally` (§84-C):** `run_role` builds a
    fresh client per provider attempt and must close it on both success and failure;
    one provider exception falls through to the next provider. Health verdicts are
    TTL-cached, not cached forever, so a dead/recovered provider surfaces.
18. **Synthetic IDs are never chain bindings (§84-B):** only 80–90-char base58 values
    may be written to `decision_commits.signature`; `live-*`/`ledger-*` mirror keys stay
    unattributed until exact CommitLog binding. Retro matches also require mint
    agreement when both rows expose a mint.

## Anti-patterns explicitly rejected
LLM pass/fail fields; .get(key, default) fabrication on external data;
silent except-pass near money math; short-circuit gates; auto-applied
threshold changes; agent layers between gate and action (the reference bot does
this — we deliberately don't).

## Mandatory Pre-Commit and Pre-Push Verification Gate
**BINDING INVARIANT: no git push on any branch — including documentation-only changes — until the full gate passes locally AND GitHub Actions is green. A red check blocks the push; fix it, never bypass it.**

Before ANY commit or git push to the repository, the automated verification gate must pass cleanly.
Run the canonical verification script from the repository root:

```bash
./scripts/prepush_check.sh
```

This runs the 9 required verification steps in order and stops at the first failure:
1. `env -u ADMIN_TOKEN pytest -q` (hermetic backend test suite)
2. `pip check` (dependency graph integrity)
3. `pip-audit -r backend/requirements.lock` (Python vulnerability audit)
4. AST architectural isolation test (verifies `backend/api/` never touches `load_keypair` or signing code)
5. `npx tsc --noEmit` (frontend TypeScript type check)
6. `npm run build` (frontend Vite production bundle)
7. `npm audit --omit=dev` (production npm dependency vulnerability audit)
8. `npm run test:e2e` (Playwright end-to-end integration tests)
9. `gitleaks git --verbose` (secret scan across repository history; skipped if binary is absent)

To install this gate as an optional automatic git pre-push hook:
```bash
ln -s ../../scripts/prepush_check.sh .git/hooks/pre-push
```

