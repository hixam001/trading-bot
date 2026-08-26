# P0 Implementation Report - trading-bot vs omotrades/omo

**Date:** 2026-08-25  **Scope:** P0 items from the omo-parity gap analysis
**Status:** IMPLEMENTED AND TESTED - real execution remains DISARMED by design

---

## 1. Executive summary

This batch closes the two P0 gaps identified in the omo comparison:

1. **Devnet drill capability** - a safe, repeatable way to exercise the full
   execution plumbing (keypair, address verification, chain reads, build,
   sign, broadcast, confirm) against devnet with zero token exposure.
2. **Sell sealing** - sells are now committed (sha256-sealed) BEFORE they
   execute, in both the paper book and the live executor, exactly as entries
   already were. Every sell can be traced to its pre-action commit row.

Additionally, an audit revealed that live mode still HARD-FAILED without a
Birdeye API key. That dependency is now removed: Birdeye is optional,
discovery runs fully on Dexscreener plus new-listings, and security checks
fall back to free on-chain RPC authority reads. The bot now runs keyless
(omo parity on data dependencies).

Verification: **208 tests passing** (154 backend + 54 live_execution), plus
an import/syntax pass over all 11 touched or created files, plus a live
UNARMED cycle smoke run against real endpoints.
---

## 2. Dexscreener vs Birdeye capability audit (findings)

The question: is Dexscreener now used to the same capability as Birdeye?
Audit result BEFORE this batch:

| Capability | Source | Status |
|---|---|---|
| Discovery: trending | Birdeye memepool | MANDATORY - stack refused to start without key |
| Discovery: keyword rotation | Dexscreener search x45 queries | OK |
| Discovery: boosts / profiles | missing | MISSING |
| Pair data: liquidity, vol, txns, age, socials | Dexscreener | OK (keyless) |
| Breadth fields: chg 5m/6h/24h, fdv, 6h txns | missing | MISSING |
| Cross-pool aggregates (pools, top share) | missing | MISSING |
| Security: mint/freeze authority | Birdeye only (401 on free tier) | BROKEN in practice |
| Security: honeypot, metadata | Birdeye only | degraded to UNKNOWN |
| Prices | Jupiter lite-api (keyless) | OK |

Findings and fixes applied in this batch:

- F1: live mode required BIRDEYE_API_KEY or raised RuntimeError. FIXED -
  the trending lens now skips itself when no key is present; discovery = new
  listings + keyword rotation; warning logged once at startup.
- F2: security_clear rule was permanently blind (free-tier 401). FIXED -
  new onchain_security.py reads SPL mint authority flags from public RPC;
  works with zero API keys.
- F3: breadth fields absent. FIXED - Candidate carries chg5m/6h/24h, fdv,
  buys/sells/vol 6h, pool_count, total_liquidity_usd, top_pool_share,
  boosted; dexscreener.py extracts them; research.py aggregates cross-pool.
- F4: no second-pass research. FIXED - research.py enriches the board head
  (RESEARCH_PER_TICK cap) exactly as omo researches names it cares about.

Remaining Birdeye-only value (accepted): honeypot heuristic and mutable-
metadata flags. These stay None (= unknown) without a key - never guessed.

## 3. What was implemented (file by file)

### 3.1 Data pipeline (backend/)

- **models.py** - Candidate gained 13 optional fields (None = unknown,
  never fabricated): price_change_5m_pct, price_change_6h_pct,
  price_change_24h_pct, fdv_usd, buys_6h, sells_6h, volume_6h_usd,
  pool_count, total_liquidity_usd, top_pool_share, boosted, plus
  social_interest / social_note for the realtime read. All exported in
  to_dict(). Existing fields and semantics unchanged.
- **data_providers/dexscreener.py** - _extract_pair_fields now also parses
  m5/h6/h24 changes, h6 txn counts, h6 volume and standalone FDV; the
  enrichment loop applies every new field when present.
- **data_providers/research.py** (NEW) - port of omo researchToken:
  aggregate_pairs() is a PURE function summing every Solana pool per mint
  (pool count, total liquidity, top-pool share, 6h windows) and is unit-
  tested; enrich_with_research() applies it to the board head with bounded
  concurrency, filling only missing single-pair values.
- **data_providers/discovery.py** - rebuilt board composition with omo slot
  guarantees: flow-ranked core, NEWBORN_SLOTS=3 (young + socials/site),
  MOVER_SLOTS=2, RESERVED_ROTATION_SLOTS=5 rotating from the remainder,
  BOARD_CAP=16; token-boosts top/latest feeds set the boosted flag;
  fake-chart filter retained.
- **data_providers/onchain_security.py** (NEW) - keyless authority checks
  via getAccountInfo(jsonParsed) across ONCHAIN_RPC_URLS with failover;
  parse_mint_authorities() is pure and unit-tested.
- **data_providers/live.py** - Birdeye trending lens now optional (_trending
  returns [] without a key); security enrichment tries Birdeye when keyed,
  then always fills remaining None authority flags on-chain.

### 3.2 Social read stage (backend/llm/social.py, NEW)

The omo realtime role as a first-class, RIGID, provider-agnostic stage.
Groq, xAI Grok, OpenRouter and Cerebras all speak the OpenAI-compatible
/chat/completions protocol, so the client is generic and the provider is
three env values:

    SOCIAL_LLM_BASE_URL  (groq: https://api.groq.com/openai/v1)
    SOCIAL_LLM_API_KEY   (empty = stage fully disabled)
    SOCIAL_LLM_MODEL     (llama-3.3-70b-versatile | grok-3-mini | ...)

Switching from Groq to Grok tomorrow is a .env edit - zero code changes.

Contract: the read is EVIDENCE ONLY. Output is interest in
{organic, peaked, unclear} plus one grounded sentence. It never returns a
verdict and can never flip one. parse_social() rejects any interest outside
the allowed set or an empty note. Disabled without a key; dead endpoint,
HTTP error or unparsable output = skipped (fail-soft like every feed).
Capped at SOCIAL_READ_PER_TICK (8) with concurrency 4. Injected into the
thinker prompt as an evidence line via {social_line}; absent when no read.

### 3.3 Real execution (live_execution/) - complete, DISARMED

- **solana.py** (NEW) - JSON-RPC with rotating failover across
  ONCHAIN-style RPC_URLS; send_raw_transaction broadcasts across all
  endpoints (preflight ON, deliberately stricter than omo);
  confirm_signature polls getSignatureStatuses every 2s up to timeout - a
  send that never confirms is never journalled; get_sol_balance;
  get_mint_decimals via getTokenSupply (None = refuse, the decimals lesson);
  latest_blockhash for hand-built transactions.
- **executor.py** (NEW) - place_order(side, mint, symbol, usd/fraction) is
  the single entry point with omo result statuses:
    unarmed - LIVE_TRADING_ENABLED False; nothing runs at all
    blocked  - a guard refused BEFORE any network call
    failed   - network attempted but no confirmed fill
    filled   - confirmed on-chain and journalled (only then)
  Buys: USD-sized, idempotent replay, daily deploy cap $300, SOL reserve
  0.05, price-impact floor 2.5 pct, exposure/position caps via preflight.
  Sells: fraction-of-position with chain-read decimals (UNKNOWN REFUSES),
  impact floor, manual-confirm gate when enabled.
- **commit_log.py** (NEW) - CommitLog: seal(kind, payload) records
  sha256(nonce + | + canonical payload) BEFORE broadcast together with the
  plaintext so anyone can recompute; bind(hash, signature) attaches the
  confirmed signature. Atomic tmp+replace writes; corrupt file fails loudly.
- **wallet.py** - verify_expected_address(): derived pubkey MUST equal
  EXPECTED_WALLET_ADDRESS when set (omo keys.server.ts parity).
- **models.py (ledger)** - open_token_amounts(), deployed_today_usd(),
  reduce_position() for pro-rata TP-ladder trims (mark_close could only do
  full closes and would lose exposure accounting on trims).
- **config.py** - RPC_URLS failover list, EXPECTED_WALLET_ADDRESS,
  MAX_PRICE_IMPACT_PCT=2.5, MIN_SOL_RESERVE=0.05, MAX_DAILY_DEPLOY_USD=300.

ARMING remains manual-edit-only: LIVE_TRADING_ENABLED=False is hardcoded.

### 3.4 Sell sealing in the paper book

scan_and_execute_exits() now seals every gated sell BEFORE execution:
sha256(nonce + | + canonical payload) where payload carries v, kind=exit,
trade_id, symbol, mint_address, rule, action (close_full/trim), fraction,
detail, price_usd and decided_at. The row lands in decision_commits with
verdict=sell via the SAME repository function entries use - no schema
migration needed, both db.py and db_pg.py stay surface-identical.

### 3.5 Refusals as a first-class public artifact
- get_refusal_events() added to BOTH db.py and db_pg.py.
- GET /api/refusals.json returns every rejection with its full rule
  breakdown, newest first (limit clamped 1..500).
- /api/proof.json now embeds refusals and their count alongside commits
  and fills. omo: hundreds of boring nos are what make automation credible.

### 3.6 Devnet drill (run_live_cycle.py --drill)

Steps executed in order, each reported PASS/FAIL with detail:

  1. wallet          - keypair loads, EXPECTED_WALLET_ADDRESS matches
  2. devnet-rpc      - getBalance answers on DRILL_RPC_URL
  3. chain-decimals  - getTokenSupply(SOL mint) returns 9
  4. funds           - balance covers the dust transfer plus fees
  5. blockhash       - getLatestBlockhash returns a fresh hash
  6. build+sign      - real VersionedTransaction self-transfer via solders
  7. broadcast       - sendRawTransaction accepted (preflight ON)
  8. confirm         - getSignatureStatuses reaches confirmed/finalized

Safe by construction: devnet endpoint only, self-transfer of dust,
no Jupiter, no tokens, bypasses no safety flags for mainnet.

## 4. Test evidence

### 4.1 Test runs executed during this batch

| Run | Scope | Result |
|---|---|---|
| Baseline before changes | backend/tests | 145 passed in 1.22s |
| Checkpoint after pipeline/social work | backend/tests | 145 passed in 1.12s |
| Checkpoint after discovery rewrite | backend/tests | 154 passed in 1.16s |
| After executor/commit-log wiring (found import bug, fixed) | live_execution | 54 passed in 0.10s after fix |
| FINAL full suite | root pytest.ini | **208 passed in 1.36s** |

New tests added:
- test_commit_log_seal_bind_roundtrip - seal, partial bind persistence,
  pro-rata reduce math (20 pct trim of 100 cost returns pnl 10), full close,
  realized PnL today, deployed-today cap math vs the $300 default.
- test_quote_impact_fraction_to_percent - Jupiter fraction-to-percent math.
- test_place_order_unarmed_by_default - buy AND sell return unarmed while
  LIVE_TRADING_ENABLED is False; nothing touches the network.
- test_order_result_defaults_and_json - serialization contract.
- test_aggregate_pairs_cross_pool / _no_solana_returns_none - research.py
  aggregation: pool count, total liquidity, top-pool share 0.75, 6h sums.
- test_fake_chart_filter - wash-trade ratio guard both branches.
- test_board_slot_composition_guarantees - dedupe + cap + composition.
- test_parse_social - valid embedded JSON, disallowed interest, no-JSON.
- test_disabled_without_key - zero calls, fields stay None.
- test_read_request_shape_and_parse - httpx.MockTransport asserts the
  request path ends /chat/completions, bearer header, and parses reply.
- test_think_prompt_includes_social_line - evidence line present when a
  read exists, absent otherwise.
- test_onchain_authority_parse - revoked/active/unknown branches of the
  jsonParsed mint account parser.

### 4.2 Non-test verification performed

- ast.parse syntax gate over every created/modified file (all OK).
- Import smoke: live_execution.executor, solana, commit_log, drill import
  cleanly; place_order(sell) returns unarmed while disarmed.
- quote_impact_pct(0.012) == 1.2 verified in-process.
- run_live_cycle.py --help renders; --drill flag registered.
- LIVE UNARMED SMOKE (real endpoints): ran one full cycle with
  DATA_BACKEND=live. Observed: UNARMED disclosure logged; Birdeye trending
  200; keyword rotation 5x200; token-boosts top/latest 200 (new feeds);
  per-mint second-pass research calls; Privy session minted for crowd feed;
  stealth fallback engaged on prod-api 403s exactly like omo documents.

## 5. Findings and decisions recorded during P0

1. The old fail-fast on BIRDEYE_API_KEY contradicted the keyless omo model
   and made security permanently blind on the free tier - removed.
2. Sell sealing reuses decision_commits with verdict=sell instead of a new
   table: zero migration, same tamper-evident hash contract, visible in
   proof.json and exits.json.
3. reduce_position() replaces mark_close semantics for trims: partial sells
   now shrink cost and tokens pro-rata instead of closing the whole buy.
4. Preflight stays ON for broadcasts (omo uses skipPreflight=True): a
   rejected transaction costs nothing, a surprise fill does.
5. Drill bypasses arm flags BY DESIGN but is devnet-only and token-free,
   so it cannot move funds even in principle.

## 6. Known limitations (honest coverage statement)

- ZERO live-network test coverage for the execution path: nothing has ever
  signed or sent against devnet or mainnet. The --drill run IS the required
  first exercise, then devnet with a throwaway keypair, then tiny mainnet.
- Social read currently reasons from tape + crowd heat only; no X/posts
  feed exists yet. append seam is ready when a source lands.
- Live commits are local-file only (no on-chain memo) per operator decision;
  they prove internal consistency, not public immutability.
- Manual confirmation still gates live buys/sells until an operator edits
  REQUIRE_MANUAL_CONFIRMATION in live_execution/config.py.
- Sell risk gate uses paper-side constants; live clip/cooldown tuning is
  post-drill work.

## 7. Next steps (P1 queue)

1. Execute the drill on devnet for real and archive the step report.
2. /api/disclosure.json - armed state, bound vs unbound commits, marks,
   provider status.
3. /api/reasoning.json + theses book with model authorship.
4. Wire a posts feed into social.append_evidence() when one exists.
5. Calibration window review; tune regime placeholders from rejection data.
