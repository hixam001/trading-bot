## Memory-bank update - 2026-08-25 (session 3)

- **Rules**: gate now uses EXACTLY omotrades 9 rules
  (market_regime_ok and security_clear retired from active set;
  regime still computed/logged as observability).
- **web_research.py** (NEW): Firecrawl search evidence for thinker,
  WEB_SEARCH_PER_TICK cap, fail-soft, keyless-off.
- **social.py** (NEW): rigid provider-agnostic social read
  (Groq/Grok/OpenRouter via SOCIAL_LLM_* env).
- **live_execution**: solana.py multi-RPC + confirm; commit_log.py
  CommitLog seal/bind; executor.py place_order buy+sell with omo statuses;
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
