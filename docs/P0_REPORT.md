# P0 Report

## 1. Gate rules

Gate: exactly omotrades 9 entry rules
liquidity_floor, volume_alive, buy_pressure, not_newborn_fade,
public_presence, crowd_heat, cash_available, already_held, not_on_break.

## 2. What was implemented
- models.py: Candidate gained 13 optional breadth fields (chg 5m/6h/24h, fdv, buys/sells/vol 6h, pool_count, total_liquidity_usd, top_pool_share, boosted)
- dexscreener.py: extracts all breadth fields from pair payloads
- research.py (NEW): second-pass cross-pool aggregates - pool count, total liquidity, top_pool_share, 6h windows; wired into main.py read stage
- discovery.py: slot-guaranteed board composition (newborn/movers/rotation slots, boost feeds, cap 16) + wash-trade fake-chart filter
- refusals API: get_refusal_events() in both db.py/db_pg.py; /api/refusals.json + refusals embedded in /api/proof.json
- social.py (NEW): provider-agnostic OpenAI-compatible client for social evidence; rigid system - switching provider = env change only
- live_execution/: solana.py (multi-RPC failover, confirm, decimals via getTokenSupply), commit_log.py (seal-before-broadcast intents), executor.py place_order buy+sell with omo statuses, wallet verify_expected_address, ledger reduce_position pro-rata trims
- OLLAMA_NUM_PREDICT knob (default 512) in thinker+narrator
- run_live_cycle.py root runner for autonomous cycles (backend never imports live_execution)
- 212 tests passing (backend 158 + live_execution 54)
- web_research.py (NEW): Firecrawl search for last-24h web context per candidate; rigid evidence-only contract; FIRECRAWL_API_KEY env

## 3. Honest risk note on the 9-rule swap

security_clear was retired from ACTIVE_RULES to match omotrades exactly.
Consequence: a token with live mint/freeze authority can now PASS the gate
(omo accepts this risk; we inherit it). onchain_security.py still fills
authority fields from free RPC and they are logged + surfaced in proof.json
for post-hoc audit, but nothing blocks the buy. Re-enable = one line in
ACTIVE_RULES if you want the stricter stance back.

## 4. Live execution details

- DISARMED by default: LIVE_TRADING_ENABLED=False hardcoded in live_execution/config.py
- place_order() returns omo-compatible statuses: unarmed/blocked/failed/filled
- Guards: price-impact floor 2.5 pct, SOL reserve 0.05, 300 USD daily deploy cap, idempotent buys (no double-entry on same mint)
- commit_log.py: sha256(nonce + canonical payload) seal-before-broadcast; bind signature after on-chain confirm
- wallet.py: verify_expected_address() refuses to operate on wrong keypair
- ledger: reduce_position() pro-rata trims for partial TPs; open_token_amounts() and deployed_today_usd() for guards
- drill.py: devnet self-transfer drill exercises keypair, address check, chain reads, build, sign, broadcast, confirm - zero token exposure

## 5. Test evidence

212 tests passing (backend 158 + live_execution 54). New tests this batch:
- commit-log roundtrip (seal then bind, tamper detection)
- authority parser (onchain_security free-RPC mint/freeze reads)
- social stage: parse (organic/peaked/unclear + reject), disabled without key, request shape
- web stage: disabled without key, last-24h filter, request shape
- gate: 9-rule set active, regime/security retired from ACTIVE_RULES
- research aggregates, discovery slot guarantees, breadth extraction
- refusals API shape, paper sell sealing, ledger reduce_position
- executor guards: unarmed/blocked paths, price-impact floor, daily cap, idempotency
- wallet verify_expected_address; drill devnet self-transfer (mocked in CI)

## 6. What omo still has that we lack

- Real armed trading history (we are disarmed; no on-chain PnL yet)
- Birdeye-only security signals: honeypot heuristic, mutable-metadata flag
- omo realtime social firehose (we poll LLM-summarized evidence instead)
- omo multi-wallet / sub-account infrastructure
- Longer runtime soak: our live stack has only UNARMED smoke cycles so far

## 7. Verification

Run: pytest backend/tests + live_execution/tests - 212 pass, 0 fail.
UNARMED live smoke: run_live_cycle.py against real endpoints, no key required.
