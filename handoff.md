# HANDOFF — trading-bot

**Last updated:** 2026-08-22 · **Branch:** main (`8316109`) · **Status:** LIVE
(real market data, simulated funds) · **App:** http://localhost:8000

Read this top-to-bottom before touching anything. It contains everything a
new session needs: state, decisions, bugs fixed, invariants, and next steps.

---

## 1. What this project is

A local **paper-trading research system** for Solana memecoins. Every tick
(~60s) it fetches real candidates (Birdeye memepool trending), enriches them
(Dexscreener pairs), computes a market-wide regime snapshot, and evaluates
each candidate against **ten deterministic rules**. The AND of all rules is
the entire entry decision. Exits come only from three fixed numeric checks.
A local LLM (qwen3:8b via Ollama) **narrates decisions already made** — it
never decides, scores, or overrides anything. Everything is logged to SQLite
and visible in a React dashboard served by the backend itself.

**Non-negotiable:** the paper-trading pipeline (`backend/`) is paper only —
no wallet, no transaction construction anywhere there.
`PAPER_TRADING_ONLY = True` is hardcoded in `backend/config.py` and
runtime-asserted inside every position-opening function. The separate
`live_execution/` package at the repo ROOT (never imported by backend/) is
the only real-execution code; it ships DISARMED — hardcoded
`LIVE_TRADING_ENABLED = False`, mandatory manual confirmation, kill switch,
daily-loss breaker — and must never be armed without a human editing its
config.py and testing the full flow on Solana devnet first. If a task ever
seems to require real execution inside backend/ — stop and flag it.

## 2. How to run / stop / test

```bash
./start.sh        # one click: builds frontend, starts ollama serve IF not
                  # already up, starts backend+tick loop on :8000 (serves
                  # dashboard), opens browser. Idempotent.
./stop.sh         # stops backend; leaves pre-existing ollama alone
cd backend && ../.venv/bin/python -m pytest tests/ -q   # 145 tests, ~1s
.venv/bin/python -m pytest -q                           # 193 incl. live_execution
```

- Dashboard/API: http://localhost:8000 (single origin; backend serves the
  built frontend from `frontend/dist`)
- Logs: `logs/{backend,ollama,frontend-build}.log`; pids in `.run/`
- Dev frontend hot-reload: `cd frontend && npm run dev` (:5173, proxies /api,/ws)
- Knowledge ingest: `cd backend && ../.venv/bin/python scripts/ingest_directory.py <dir>`

## 3. File map (what lives where)

| Path | Purpose |
|---|---|
| `backend/config.py` | ALL thresholds + safety flag + provider keys via env |
| `backend/models.py` | Candidate (incl. `decimals`), Trade, FeedEvent, RuleResult, GateDecision, PortfolioState |
| `backend/rule_engine/` | `rules.py` (11 omo-parity entry rules), `exits.py` (omo exit engine: stop/trail/liquidity-break/invalidation/stale/TP-ladder + sell risk gate), `gate.py` (no-short-circuit AND), `regime.py`, `liveness.py` (not_on_break) |
| `backend/data_providers/crowd.py` | fomo.fun board reader (Privy session, auto-renewing rotated refresh tokens, Firecrawl stealth fallback, junk filter) → feeds crowd_heat |
| `backend/paper_trading_engine.py` | money math + atomic open/close/scale_in + exits + decide_and_act |
| `backend/api/db.py` | schema + repository; atomic conditional writes return rowcounts |
| `backend/api/main.py` | FastAPI app; serves built frontend; TICK_LOOP_IN_PROCESS env runs tick loop in-process |
| `backend/data_providers/` | base(protocol,retry,counters), birdeye, dexscreener, jupiter, live(stack), mock |
| `backend/llm/` | narrator.py (prompt, Ollama client, template fallback, reflection), grounding.py |
| `backend/knowledge_base/loader.py` | static KB, digest-at-ingest, budgeted get_context |
| `backend/main.py` | run_tick(): regime once/tick → per-candidate gate+narrate → exit checks |
| `backend/promotion_gate.py` | READ-ONLY 5-criteria readiness report. Never writes. Ever. |
| `live_execution/` | REAL-MONEY execution package at repo ROOT (never imported by backend/). Ships DISARMED: hardcoded LIVE_TRADING_ENABLED=False, REQUIRE_MANUAL_CONFIRMATION=True, kill switch + daily-loss breaker, confirmation queue w/ fail-closed expiry, idempotency ledger, caps. Operator CLI: `python -m live_execution.scripts.confirm_trade list|approve|deny|kill|resume`. Zero live-network test coverage — devnet + throwaway keypair REQUIRED before any mainnet use |
| `frontend/src/` | dashboard panels (feed WS, holdings, journal, stats, regime, gate, KB, status) |
| `docs/00..07` | blueprint, architecture, feature list (+status), gantt, verification appendix, omotrades comparison, project report |
| `.env` (root, gitignored) | operator settings ONLY: keys + DATA_BACKEND=live |


## 4. The ten entry rules (thresholds are placeholders until calibrated)

Evaluated unconditionally on every candidate — no short-circuiting; every
rejection logs its complete profile. Full details: `docs/07_PROJECT_REPORT.md` §3.

| rule_id | logic | threshold | None behavior |
|---|---|---|---|
| liquidity_floor | liquidity ≥ floor | $10,000 | missing → FAIL closed |
| volume_alive | 1h volume ≥ min | $5,000 | missing → FAIL closed |
| buy_pressure | buys > sells (1h tx counts, strict) | tie fails | either missing → FAIL |
| not_newborn_fade | NOT(age<2h AND chg1h≤−30%) joint | 2h / −30% | either missing → FAIL |
| public_presence | any of twitter/telegram/site confirmed | any 1 passes | unknown ≠ absent; all-unknown fails |
| market_regime_ok | tick regime OK (shared, computed once/tick) | green 15–85% AND median vol ≥ $20k | empty batch → BAD |
| cash_available | cash ≥ intended size | $100 | n/a |
| exposure_cap | held in mint < cap (entries AND scale-ins) | $150 | n/a |
| security_clear | fails only on KNOWN-bad (authority live, honeypot) | — | **None always PASSES** |
| volume_mcap_ratio_ok | v24h/mcap ≥ ratio | 0.80 | inputs missing → PASS neutral |

Exit conditions (checked per open position each tick, in order):
take-profit ≥ +50% → stop-loss ≤ −20% → timeout ≥ 72h (net of 2% slippage +
1% fee).


## 5. Decision log (why things are the way they are)

1. **Ground-up rebuild** — old `backend/+frontend/` implementation was
   deleted (user action); rebuilt to revised docs spec: rule engine,
   GateDecision, regime gate, narration-only LLM. Old code remains in git
   history only.
2. **Layout** — everything Python lives in `backend/` (flat modules +
   packages); `frontend/` separate. Run commands from inside `backend/`.
3. **Schemas added**: `market_regime` (append-only, once/tick,
   candidate_count column) and `provider_call_counters` (per provider/day
   with separate 429 counter). Partial UNIQUE index on
   `trades(mint_address) WHERE is_open=1`.
4. **Groundedness validation**: rule-derived synonym map
   (llm/grounding.py RULE_GROUNDING_TERMS), invented-rule-id check, numeric
   echo check (trailing punctuation stripped). Flags RECORDED on feed
   events, never dropped/rewritten.
5. **Atomicity (§5.1)**: conditional writes return rowcount; rowcount is
   the sole authority for touching cash; scale-in exposure cap enforced
   inside the UPDATE WHERE clause; unique index backstop.
6. **Birdeye discovery**: `/defi/tokenlist` returned majors → switched to
   `/defi/token_trending?listing=memepool`. Field names confirmed against
   real payloads (`volume24hUSD`, `marketcap`, `decimals`).
7. **Jupiter endpoint migrated**: quote-api.jup.ag/v6 is dead (DNS gone);
   now `lite-api.jup.ag/swap/v1/quote`. Pro key rides as x-api-key when set.
8. **DECIMALS BUG (critical, fixed)**: Jupiter was quoted with amount=10⁹
   assuming 9 decimals for every mint. BARRON has 6 decimals → quoted 1000
   tokens as "one" → price inflated exactly 1000× ($0.00054 → $0.539) →
   take-profit booked five fake ~+96,407% closes, cash hit $481,783. Fix:
   Candidate.decimals from Birdeye threaded through protocol → exit loop &
   holdings pass decimals from trade snapshot; Jupiter refuses to quote
   without decimals (fail closed). Regression tests pin it
   (tests/test_jupiter_decimals.py). Corrupted DB wiped; fresh book at $1,000.
9. **token_security on free tier**: 401 (tier lacks it). 401/403 are
   non-retryable ProviderAuthError; BirdeyeProvider disables security
   enrichment for the session after first 401 (semaphore(2) bounds burst).
   Security fields remain UNKNOWN → security_clear passes them (never
   coerced to False).
10. **omotrades reference check** (docs/06): their revealed payloads show
    verdict:"pass" WITH failed rules + side:null + refusal note → an agent
    layer decides between rules and action. Our architecture closes that
    gap deliberately. crowd_heat (fomo index) has no equivalent here;
    already_held is binary vs our scale-in cap (intentional). Commit-reveal
    mechanism verified byte-for-byte locally
    (scripts/verify_reference_commit.py, both hashes MATCH), documented in
    docs/05 — deliberately NOT built.
11. **One-click app**: start.sh/stop.sh/trading-bot.desktop; backend serves
    built frontend (SPA catch-all registered AFTER api routes); ollama
    started only if not already up; stop.sh never kills pre-existing ollama.
12. **Test hermeticity**: tests pin DATA_BACKEND=mock and tmp DB paths so
    operator .env (live) can't leak into them (bit us twice).


## 6. Bugs found & fixed (all regression-tested)

- tokenlist → majors not memecoins (fixed: memepool trending)
- Jupiter v6 URL dead (fixed: lite-api swap/v1/quote)
- 9-decimals assumption → 1000× price fabrication on 6-decimal mints
  (fixed: decimals-aware quoting + fail-closed guard)
- token_security 401 retry storms stalling ticks (fixed: non-retryable auth
  errors + session disable + semaphore(2))
- qwen3 thinking mode → multi-minute narrations (fixed: think=false +
  /no_think + strip `<think>` blocks)
- numeric echo false positive on "0.80." trailing period (fixed: strip
  sentence punctuation from number tokens)
- tests leaked ingested files into real KB dir / depended on operator .env
  (fixed: monkeypatched paths + DATA_BACKEND)

## 7. Known limitations (accepted, documented)

- Birdeye free tier: no token_security → security fields always UNKNOWN
  right now (upgrade tier → auto re-enables on restart).
- Regime thresholds are placeholders pending calibration data.
- LLM narrations make ticks take ~40–90s for 20 candidates; fine at the
  60s interval, reduce MAX_CANDIDATES_PER_TICK if needed.
- Post-calibration scope, deliberately unbuilt: partial scaling (E8/E9),
  advisory LLM layer (D7), commit-reveal proof (docs/05).

## 8. Next steps

1. **Calibration window (10 days)** is underway: let it run; review daily
   via dashboard + learning-loop logs; tune ONE threshold at a time from
   rejection-breakdown evidence (manual edits to config.py).
2. Watch first entries/exits; verify realized P&L sanity on close.
3. Optional upgrades: Birdeye tier with token_security; fomo-index source
   for a crowd_heat-style rule (docs/06 §3.2).
4. After calibration: E8/E9 partial scaling, D7 advisory layer, proof
   mechanism only if going public with real capital.

## 9. Invariants checklist (before any change)

- [ ] No real-execution path added; PAPER_TRADING_ONLY untouched/hardcoded
- [ ] promotion_gate.py still read-only, no promote/activate functions
- [ ] Rules stay pure; no short-circuiting; None semantics preserved
- [ ] Money-touching changes keep atomic write→rowcount→cash ordering
- [ ] External fields validated via require_type; None never coerced
- [ ] Tests hermetic (tmp DBs, mock narration) and passing (80)
- [ ] New rule ⇒ add vocab in llm/grounding.py + both-branch tests



