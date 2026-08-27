## Memory-bank update - 2026-08-27 (omo-audit code queue session: A7/A6/A3/A2/A4)

- **Task**: close the five parity gaps surfaced by the 2026-08-27 audit of
  `omotrades/omo` (docs/09_OMO_AUDIT_COMPARISON.md), per operator instruction. Reference files fetched
  raw: market.server.ts (isFakeChart), blocklist.ts, wallet.server.ts
  (readViaRpc/getWalletSnapshot/fetchFillVenue), fomo.server.ts (readOwnBasis),
  execute.server.ts.
- **A7**: `backend/rule_engine/fake_chart.py` — all 13 thresholds ported;
  `Candidate.volume_5m_usd` added (models/dexscreener/discovery); wired into
  `main.run_tick` READ stage before think/gate. Deviation: unknown age/fdv
  skip rather than fail (documented §29).
- **A6**: `blocklist.py` gains `BLOCKED_SYMBOLS` (omo's exact list) +
  `is_blocked_symbol()`; enforced in `filter_candidates()`.
- **A3**: `live_execution/venue.py` (pure parser + fail-soft fetch);
  `decision_commits.venue` (SQLite + PG self-heal + `004_fill_venue.sql`);
  `bind_commit_venue()` both db layers; journaled in `run_live_cycle` after
  fills; `/api/binding.json` shows venue on every pair.
- **A2**: `solana.get_token_balances()` (legacy + token-2022; empty-vs-
  unreadable distinction) + `live_execution/reconcile.py` (pure). Chain =
  authority on quantities; journal = authority on cost; ledger never mutated
  by a chain read; exit sizing clamped to chain; vanished positions excluded
  + flagged; unjournaled holdings flagged, never added. Cycle outcome gains
  `chain_reconciliation`. Deliberate deviation from omo's full re-derivation:
  our §5.1 atomic journal stays the money authority (safer at micro scale).
- **A4**: `crowd.py` refactored — shared cached `_thesis_payload()`;
  `fetch_fomo_theses` contract unchanged; new `read_own_basis()` (raw-board
  match, no substantive filter on our own row, invested floored at 0, cap 10).
  `FOMO_OWN_HANDLE` env (default disabled). `_crosscheck_basis()` in
  run_live_cycle logs mismatches (tolerance max(5%,$0.50)), never applies
  them. Cycle outcome gains `basis_crosscheck`.
- **Tests**: +65 since REF-R11 → **444 combined passing** (test_fake_chart,
  test_reconcile ×10, test_token_balances ×7, test_own_basis ×9, test_venue,
  extended churn guards). Isolation grep clean. Live smoke disarmed:
  verify/binding/disclosure/proof all 200, `venue: null` on unbound pairs,
  `armed=False`, 0 tracebacks.
- **Docs**: handoff §28 code queue marked complete + new §29 implementation
  record; §27 (enable live execution) untouched and STILL the final task.

## Memory-bank update - 2026-08-27 (REF-R11 on-chain precommit memo + micro-bootstrap session)

- **Task**: implement the last implementable reference-parity gap — REF-R11
  on-chain precommit memo (commit–reveal) — operator-approved, using
  `omotrades/omo` (`precommit.server.ts`/`verify.server.ts`) as reference. Also
  folded in the micro-bootstrap accommodations so the live book can start from
  0.03 SOL (fee reserve) + $3–5 USDC (capital) and compound.
- **What shipped**: `live_execution/memo.py` (memo build + fail-closed
  `publish_commit_memo`); `commit_log.py` `sealed→published→bound` +
  `record_memo()`/`fail()`; `executor.py` order = guards→wallet→SOL reserve→
  USDC funding→seal→memo→CONFIRM memo→quote→build→send→confirm→bind (memo
  precedes the quote so the quote→fill window is unchanged); `solana.py`
  `get_usdc_balance()`; `run_live_cycle.py` real-USDC cash + journals seal+memo
  into `decision_commits`. Verifier surface: `memo_signature`/`memo_slot`
  columns (SQLite + PG self-heal + `migrations/supabase/003_commit_memos.sql`),
  `bind_commit_memo()`/`get_commit_id_by_hash()` in db.py+db_pg.py,
  `/api/verify.json` memo checks (hash-on-chain + slot ordering; unknown never
  pass), `/api/disclosure.json` `commit_memo` block. Sizing floor threaded
  through `compute_ticket`/`compute_risk_budget` (paper bit-identical). Devnet
  drill now sends a real memo.
- **Fail-closed guarantee (tested)**: a memo that cannot be confirmed BLOCKS the
  fill — the fill send is never attempted. USDC insufficient/unreadable and SOL
  below reserve all refuse BEFORE any on-chain commitment.
- **Deviations from the reference (documented handoff §26)**: fail-closed
  blocking (reference publishes async), immediate reveal, single signer = the
  trading wallet, de-branded `commit:v1:` prefix.
- **Bug found + fixed**: solders 0.29 requires `Hash.from_string()`; `drill.py`
  had a latent incompatibility (it had never run because solders was absent).
  Installed solders 0.29.0 into `.venv`.
- **Tests: 379 combined passing** (backend 308 + live_execution 71; +41 new,
  all offline/hermetic with hand-computed hash fixtures). Isolation grep clean.
  Live smoke (disarmed): verify/binding/disclosure all 200, `armed=False`,
  `paper_only=True`, 0 tracebacks.
- **Docs**: handoff §26 (implementation) + §27 (FINAL TASK: enable live
  execution — operator-only arming checklist); §22 status flipped; §8 next-steps
  points to §27; file map + test counts updated. memory-bank activeContext /
  progress / decisionLog (#41) / session-log updated.
- **Still DISARMED**: `LIVE_TRADING_ENABLED=False` + `REQUIRE_MANUAL_CONFIRMATION=True`
  untouched. Arming is the operator's final manual task (§27) — no session may
  arm before every other task is done.

## Memory-bank update - 2026-08-27 (fresh scraper keys session)

- **Operator added new keys** to the repo-root `.env` (what `load_dotenv()`
  resolves from `backend/`): Firecrawl, ScrapingBee, ScrapingDog, ScrapeOps.
  ZenRows unchanged (still 402-exhausted). Backend restarted to load keys +
  clear in-memory benches.
- **Code**: `_scrape_scrapingdog` was wired but did NOT forward the Privy
  bearer (prod-api requires it). ScrapingDog docs confirm `custom_headers=true`
  + headers-on-request (same as ScrapeOps keep_headers, no extra cost). Now
  appends `&custom_headers=true` and passes `fwd_headers=dict(headers)`.
- **Live-verified**: Firecrawl (new key) 15× 200 OK = real crowd heat restored;
  ScrapeOps (new key) 1× 200 OK failover (caught a transient firecrawl 500);
  0 tracebacks; no 15-min stalls.
- **Per-provider**: Firecrawl ✅ primary · ScrapeOps ✅ failover · ScrapingBee
  ⚠ ReadTimeout (can't forward bearer) · ScrapingDog ⚠ 403 (plan/Cloudflare;
  backup, fails soft) · ZenRows ❌ 402 exhausted.
- **Tests: 338 combined passing** (backend 290 + live_execution 48; +1
  `test_scrapingdog_forwards_privy_bearer`).

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
