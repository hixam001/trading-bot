## Memory-bank update - 2026-08-27 (dead-provider fail-fast session)

- **Reference fomo-path audit** (verbatim from its source): primary = Privy
  bearer → direct `fetch` (9s, 2 attempts); fallback = Firecrawl stealth-proxy
  behind their own gateway (`proxy:"stealth"`, `rawHtml`, 25s) — the identical
  payload we already send, same credits. No free scrape mechanism exists.
- **crowd.py**: `_CONSECUTIVE_ERRORS` + `_transport_error()`/
  `_transport_success()` — 2 consecutive transport failures (timeout/connect)
  bench a provider exactly like a 402; any completed response resets the
  streak. `_scrape_firecrawl` wrapped in try/except (was uncaught → would
  crash the chain). `_FIRECRAWL_TIMEOUT(45s)` → `_STEALTH_TIMEOUT(25s)` on
  both stealth paths. `_direct_get` now 2 transport attempts (never retries a
  real HTTP response, even 403).
- **Root cause of ~15-min ticks**: ScrapingBee ReadTimeouts were caught +
  logged but never benched → every candidate re-tried it (~20 × 45s).
- **Tests: 337 combined passing** (backend 289 incl. 6 new in test_crowd.py +
  live_execution 48). Live-verified after restart: ScrapingBee benched after
  exactly 2 timeouts, crowd stage degrades in seconds, 0 tracebacks.
- **Operator action pending**: refill Firecrawl credits to restore REAL crowd
  heat (chain self-heals, no code change). ZenRows renewal optional.

## Memory-bank update - 2026-08-27 (REF-R8 + REF-R9 session)

- **config.py**: SIZING_MODE gains "risk_budget" (default stays "fixed");
  hardcoded PER_ORDER_FRACTION=0.035, DAY_MULTIPLE=4,
  HARD_ORDER_CEILING_USD=3000, HARD_DAILY_CEILING_USD=12000 (never
  env-overridable).
- **paper_trading_engine.py**: RiskBudget + compute_risk_budget() (verbatim
  computeBudget() port, Math.round half-up parity, fail-closed min ticket);
  portfolio_equity_and_unrealized() (at-cost marks when unpriced);
  compute_ticket() risk_budget branch = budget x conviction clamped [25,3000].
- **calibration.py** (NEW): compute_calibration() verbatim computeCalibration()
  port - expectancy, raw scale (+10%->+20% / -10%->-40%), confidence
  min(n/12,1), factor clamp [0.6,1.2], FLAT 1.0 fail-closed.
- **api/db.py + api/db_pg.py**: get_daily_stats() + patch_daily_stats()
  key-merge into daily_stats.stats_json (JSONB || on PG); no migration.
- **main.py**: per-tick budget+calibration compute -> log -> persist; per-
  candidate equity recompute, conviction-scaled ticket, derived daily-ceiling
  refusal (risk_budget mode only; static cap unchanged otherwise).
- **learning_loop.py**: calibration computed + persisted (merge, advisory log).
- **api/routes/disclosure.py**: risk_budget + calibration blocks
  (persisted-first, cost-basis recompute fallback, fail-closed minimums).
- **run_live_cycle.py** (DISARMED): freshest marks captured in _manage;
  risk_budget-mode sizing + derived daily ceiling vs deployed_today_usd().
- **Tests: 331 passing** (backend 283 incl. 42 new hand-computed +
  live_execution 48). Live smoke: disclosure serves both blocks; tick
  persisted real budget (equity $991, $35/$140) + FLAT calibration.

## Memory-bank update - 2026-08-25 (session 3)

- **Rules**: gate now uses EXACTLY the reference bot 9 rules
  (market_regime_ok and security_clear retired from active set;
  regime still computed/logged as observability).
- **web_research.py** (NEW): Firecrawl search evidence for thinker,
  WEB_SEARCH_PER_TICK cap, fail-soft, keyless-off.
- **social.py** (NEW): rigid provider-agnostic social read
  (Groq/Grok/OpenRouter via SOCIAL_LLM_* env).
- **live_execution**: solana.py multi-RPC + confirm; commit_log.py
  CommitLog seal/bind; executor.py place_order buy+sell with the reference statuses;
  wallet verify_expected_address; ledger open_token_amounts/
  deployed_today_usd/reduce_position.
- **run_live_cycle.py** (NEW root runner): autonomous manage->read->think->
  gate->execute cycle; --drill flag for devnet self-transfer drill.
- **Refusals public**: get_refusal_events in db.py/db_pg.py;
  /api/refusals.json; refusals embedded in /api/proof.json.
- **Discovery**: slot-composed board (newborn/movers/rotation slots),
  boost feeds set Candidate.boosted.
- **Candidate breadth fields**: chg5m/6h/24h, fdv, buys/sells/vol 6h,
  pool_count, total_liquidity_usd, top_pool_share, boosted, web/social lines.
- **Tests: 212 passing** (backend 158 + live_execution 54).

- **Sell sealing (paper)**: scan_and_execute_exits writes decision_commits
  row (verdict=sell, payload=trade_id/rule/fraction/price) BEFORE close or
  trim executes. /api/refusals.json + refusals in /api/proof.json.
- **Live commit log**: live_execution/commit_log.py CommitLog - seal intent
  sha256(nonce|payload) before broadcast, bind signature on confirm;
  wired into place_buy and place_sell via _broadcast_and_confirm.
- **Devnet drill**: run_live_cycle.py --drill runs wallet->balance->
  decimals->blockhash->sign->send->confirm dust self-transfer, devnet only.
