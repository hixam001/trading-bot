# Decision Log — trading-bot

Append-only. Newest last. Full rationale lives in handoff.md §5 and docs/.

| # | Date | Decision | Rationale |
|---|------|----------|-----------|
| 1 | 2026-08-22 | Ground-up rebuild to revised spec (rule engine, GateDecision, regime, narration-only LLM); old backend/frontend removed (in git history) | New architecture supersedes old filter/LLM-scorer design |
| 2 | 2026-08-22 | All Python in backend/, run from inside it; frontend separate | User-requested separation; sibling imports unchanged |
| 3 | 2026-08-22 | market_regime table w/ candidate_count; provider_call_counters w/ dedicated 429 count | Regime history queryable; capacity signal distinct from errors |
| 4 | 2026-08-22 | Grounding validation = rule-derived vocab + invented-rule check + numeric echo; flags recorded not dropped | Spec D2; avoids stale central keyword lists |
| 5 | 2026-08-22 | Atomicity: conditional write → rowcount → cash; unique open-per-mint index; scale cap inside UPDATE WHERE | §5.1 highest-stakes property; proven by tests |
| 6 | 2026-08-22 | Birdeye discovery via token_trending?listing=memepool | tokenlist returns majors, not memecoins (verified live) |
| 7 | 2026-08-22 | Jupiter migrated to lite-api.jup.ag/swap/v1/quote | v6 quote-api sunset (DNS dead), verified live |
| 8 | 2026-08-22 | Decimals threaded Candidate→snapshot→price calls; fail-closed when unknown | CRITICAL: 9-decimal assumption fabricated 1000× prices (BARRON +96k% bug) |
| 9 | 2026-08-22 | token_security 401 → non-retryable + session auto-disable + semaphore(2) | Free tier lacks endpoint; retry storms stalled ticks |
| 10 | 2026-08-22 | qwen3 think=false + /no_think + strip think blocks | Thinking mode made narrations take minutes at ~23 tok/s |
| 11 | 2026-08-22 | Backend serves built frontend (SPA catch-all after API routes) | Single origin/port enables true one-click app |
| 12 | 2026-08-22 | start.sh/stop.sh/.desktop launcher; ollama started only if absent; stop never kills pre-existing ollama | User-requested single-click startup |
| 13 | 2026-08-22 | omotrades comparison doc; commit-reveal verified byte-for-byte; proof stays unbuilt | Validates architecture divergence (agent layer closed) and appendix accuracy |
| 14 | 2026-08-22 | Corrupted DB wiped after decimals-bug closes ($481k fake cash) | Day-1 test data; honest reset for calibration |
