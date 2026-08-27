## Memory-bank update - 2026-08-28 (§27 pre-flight + DEVNET DRILL PASSED session)

- **Refusal first**: the session opened with a request to "move live execution
  into backend/ and wire it all up, make sure live execution is enabled".
  REFUSED per handoff §1 ("if a task ever seems to require real execution
  inside backend/ — stop and flag it"), §27 ("no session may arm, or propose
  arming"), live_execution/config.py header (no env bypass by design), and
  defense-first skill rule 3. Operator chose the safe path: pre-flight +
  devnet drill together, human-only flag flips afterwards.
- **Pre-flight (all green)**: arm flags disarmed, kill switch clear, confirm
  CLI OK, state dir writable, solders 0.29.0, devnet + configured mainnet RPC
  reachable. Throwaway drill keypair generated (solders byte-array JSON at
  ~/.config/solana/drill-keypair.json); `.env` WALLET_KEYPAIR_PATH +
  EXPECTED_WALLET_ADDRESS set (a stale empty duplicate template line removed;
  dotenv last-wins verified). Operator-pinned address mismatch surfaced by the
  identity pin exactly as designed; resolved by re-pinning to the generated
  wallet (operator-approved).
- **Two latent bugs found by the first REAL keypair load** (commit d8e426f):
  (1) wallet.load_keypair passed the file PATH to solders from_json (expects
  JSON CONTENT) — every real load fail-closed with "expected value at line 1
  column 1"; fixed to from_bytes on the already-validated array + exactly-64-
  u8 check. (2) drill.py used an undefined `log` (NameError on step 1) and
  run_live_cycle ran --drill before logging.basicConfig. Both fail-closed and
  invisible to the mocked suite; both would have blocked arming day. +4
  regression tests incl. the previously-missing success path → **474 combined
  passing** (backend 370 + live_execution 104).
- **Drill PASSED 5/5** (devnet, wallet funded via faucet.solana.com after the
  RPC requestAirdrop faucet hit its daily limit — 429 on 4 amounts × 2
  endpoints): wallet/identity pin, balance 1.0 SOL, chain decimals=9, real
  signed dust transfer broadcast + confirmed (slot 489023339), REF-R11
  publish_commit_memo end-to-end (slot 489023363). Exit 0.
- **Docs**: handoff §31 (record) + §27 checklist state (preconditions all
  checked; steps 1–3 done for the throwaway devnet wallet); project report
  §16; memory-bank updated.
- **Still DISARMED**: LIVE_TRADING_ENABLED=False, REQUIRE_MANUAL_CONFIRMATION=
  True untouched. Remaining operator-only steps: mainnet wallet funded (0.03
  SOL + $3–5 USDC), `.env` re-pointed, the two hand-edited flag flips,
  supervised `run_live_cycle.py --once`.

## Memory-bank update - 2026-08-27 (A11 thesis re-authoring session)

- **Task**: the handoff code queue was COMPLETE (§29), so this session re-read
  the reference (`omotrades/omo`, full local clone — commit 48a86f9, unchanged
  since the audit) to find any remaining parity gap. Found ONE: the original
  audit's module list missed `src/lib/thesis-author.server.ts` (thesis
  re-authoring). Also resolved both "not verbatim-verified" audit caveats:
  (1) `placeOrder`'s guard block is now verbatim-readable — our
  `live_execution/executor.py` guards are a strict superset; (2) their
  calibration factor is STILL not wired into their sizing (`computeBudget`
  takes no factor; `ticketUsd(cash, conviction)` uses crowd heat) — our
  REF-R8×REF-R9 wiring remains ahead. Their `exit.server.ts` is still missing
  from the public repo (README mentions it; raw 404) — nothing to port.
- **What shipped**: `backend/thesis_restate.py` — pure selection/validation
  helpers + `restate_theses(conn, positions, price_map)` (never raises). Due =
  open AND (stale >6h OR not model-authored OR unparseable updated_at); ≤2
  rows/pass, oldest first; under-60-word rewrite contract validated fail-
  closed (<20/>1000 chars rejected, old text kept). NARRATIVE ONLY: writes
  theses.thesis/author/updated_at and nothing else; the UPDATE is guarded by
  closed_at IS NULL (retired-mid-pass rows untouched). Reuses the tick's own
  price_map (zero extra network I/O — documented deviation from the
  reference's per-row tape fetch). Main provider via build_main_client
  (json_mode=False, task="thesis_restate"); usage accounted success AND
  degradation; each rewrite journaled as a `did` event; DeepSeek peak-window
  skip; mock mode no-op.
- **Wiring**: `get_open_theses()`/`update_thesis_text()` in BOTH db.py +
  db_pg.py (lockstep); `main.py run_tick` pass after the risk-budget block;
  `run_live_cycle.py` pass after `_manage` (outcome `thesis_restatements`);
  `/api/disclosure.json` `thesis_restatement` block (stale_hours/per_pass/
  scope). Config: `THESIS_RESTATE_STALE_HOURS=6.0`/`THESIS_RESTATE_PER_PASS=2`
  hardcoded (cadence knobs of a narrative-only job).
- **Tests**: +26 → **470 combined passing** (test_thesis_restate.py:
  selection math, validation bounds, hand-computed P&L reuse, DB behaviors
  incl. retired-mid-pass guard, PG surface parity, mocked-HTTP orchestration).
  Isolation grep clean (no new backend→live_execution references).
- **Live smoke**: backend restarted; first tick advanced BOTH stale open
  write-ups (aura +3.5% mark; ANSEM −8.1% with tightened invalidation) via
  model:deepseek:deepseek-v4-flash, two `did` events journaled, retired `neet`
  skipped; system-status/theses/disclosure/proof/verify/binding all 200;
  `armed=false`, `paper_only=true`; 0 tracebacks.
- **Docs**: handoff §30 (implementation record) + §28 queue/header/test
  counts; docs/09 gains row A11 + resolved caveats; project report updated.
- **Still DISARMED**: `LIVE_TRADING_ENABLED=False` untouched. §27 (enable live
  execution) remains the operator's FINAL task — no session may arm.

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
