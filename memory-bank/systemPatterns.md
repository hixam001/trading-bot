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
  short-circuit); all_passed = AND = the entire entry decision
- llm/thinker.py — evaluates each candidate BEFORE rules; entry requires
  model buy AND all-rules-pass (think→gate intersection)
- llm/narrator.py — receives only the GateDecision; prompt forbids
  second-guessing; output validated for groundedness; flags recorded

## Key patterns
1. **Atomicity (§5.1):** conditional state write FIRST (WHERE clause makes
   retry a no-op) → rowcount decides → cash adjusted only after rowcount==1.
   Rowcount is the sole authority. Backstops: partial UNIQUE index
   (one open position per mint); scale-in cap enforced inside UPDATE WHERE.
2. **Fail closed:** provider fields missing → None → numeric rules FAIL
   ("unavailable"); security None → PASS (unknown ≠ unsafe). Never coerce.
3. **Once-per-tick regime:** compute_market_regime() over the whole batch,
   one market_regime row per tick, shared verdict for every candidate.
4. **Provider discipline:** one shared httpx.AsyncClient; bounded retries;
   distinct 429 backoff + counters; 401/403 non-retryable ProviderAuthError;
   token_security session-disables itself after first auth failure.
5. **Decimals-aware pricing:** Jupiter quoted with 10**decimals raw units;
   refuses without decimals (regression-tested against a real 1000× bug).
6. **Grounding validation:** thesis checked against rule-derived vocabulary,
   invented rule-ids, and number echo; flags recorded on feed events.
7. **Read-only boundary:** promotion gate + all API endpoints report state;
   nothing outside run_live_cycle.py may change trade/cash state.
8. **Exit engine (§5.2):** 6 rules — stop (−20%), HWM trail (50%/40pp),
   liquidity break (<$8k), invalidation (−25%&1.4×sells), stale (14d),
   TP ladder (+100/300/900 trims). Dedicated 15s fast exit scanner to avoid
   stop-loss gap risk.

## Anti-patterns explicitly rejected
LLM pass/fail fields; .get(key, default) fabrication on external data;
silent except-pass near money math; short-circuit gates; auto-applied
threshold changes; agent layers between gate and action (the reference bot does
this — we deliberately don't).

## Mandatory Pre-Commit and Pre-Push Verification Gate
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

