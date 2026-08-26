# Progress — trading-bot

## Works (all verified)
- [x] OMO-R2 FOMO crowd intel upgrade: theses WITH author P&L (2026-08-26)
- [x] OMO-R5 durable events + weighted memory recall, prompt context,
      stage event hooks, `/api/events.json` (2026-08-26)
- [x] OMO-R4 Self-regulating break system wired into thinker and gate loops (2026-08-26)
- [x] OMO-R3 Durable thesis book hooked into live and paper cycles + /api/theses.json (2026-08-26)
- [x] 10 deterministic rules, both branches tested, no short-circuit (B1–B12)
- [x] Candidate model with all fields incl. decimals + None semantics (B13)
- [x] Market regime: computed once/tick, own table, API endpoint (C1–C5)
- [x] Atomic paper engine: open/close/scale_in, decide_and_act, exits,
      PAPER_TRADING_ONLY runtime asserts (E1–E7) — double-open/double-close/
      crash-replay proven by tests
- [x] Money math with hand-computed expectations, raise-on-invalid (E5/E6)
- [x] Providers live+mock: Birdeye memepool/security, Dexscreener full
      enrichment, Jupiter decimals-aware, retry/429/counters (A1–A9)
- [x] LLM narration pre-decided verdicts + grounding validation + Ollama
      health + reflections (D1–D6); template fallback for offline runs
- [x] Knowledge base: static + ingest CLI/API + digests + budgeted context
      + bucket stats (F1–F9)
- [x] Learning loop daily stats + rejection breakdown (G1–G3)
- [x] Read-only promotion gate, 5 criteria (G4–G6)
- [x] Full API surface + WS feed broadcaster (H1–H5)
- [x] Dashboard panels incl. persistent safety banner (I1–I10; build passes)
- [x] Tests J1–J5 → **145 passing** in backend
- [x] One-click launcher verified start/stop/restart
- [x] omotrades comparison + commit-reveal verification (docs/06)
- [x] Task C: live_execution/ seven-file safety model at repo root,
      offline-tested (**48 passing**; 182 combined via root pytest.ini)
- [x] omo-mimicry rebuild in progress: exit engine + fast scan loop,
      entry gate = omo rules verbatim, old logic purged, crowd conviction
      feed (fomo.fun board) live-validated → **backend 136 / combined 184**
- [x] Security audit: secret scans CLEAN, stale files removed, gitignore
      hardened (*.db-journal, wallet-keypair.json)
- [x] Supabase Postgres backend (optional): migrations/supabase/001_init.sql
      applied; api/db_pg.py asyncpg twin of db.py (identical surface,
      §5.1 atomicity preserved); db.py backend selection with pytest
      SQLite guard; live smoke passed all atomicity checks against real
      Supabase + uvicorn boot serving PG data on all endpoints
- [x] Stealth-scrape chain upgraded: scrapeops keep_headers + zenrows
      custom_headers+premium_proxy forward the Privy bearer — verified
      pulling REAL fomo board data through Cloudflare; scrapingbee
      keyless-only (platform limitation); _json_from_body statusCode≥400
      bugfix (success envelopes were silently discarded)
- [x] Live execution wiring verified: root bridge, Jupiter quote/swap,
      local signing, rotating RPC send/confirm, commit binding, ledger
      journal, live manage/sell path, and fail-closed preflight are connected;
      disarmed by hardcoded `LIVE_TRADING_ENABLED=False`; 45 focused tests
      pass. Funded throwaway-keypair devnet drill remains required.

## Deliberately not built (per spec sequencing)
- E8/E9 partial scaling + rolling history (post-calibration)
- D7 advisory LLM layer (post-calibration)
- Commit-reveal proof mechanism (only if public real-capital track record)
- crowd_heat rule (needs a fomo-index source)

## Known issues / watch items
- Birdeye free tier: token_security 401 → security fields UNKNOWN
- Ticks take 40–90s with 20 candidates (LLM-bound); acceptable
- Regime/rule thresholds are placeholders — calibration will move them
- ScrapingBee fallback is keyless-only (platform consumes Authorization)
- ZenRows premium tier costs ~10–25 credits/request (required for prod-api)
- Supabase pooler cert self-signed → fingerprint pin (.supabase_fp.txt);
  delete the file to re-pin after a legitimate cert rotation

- LLM API migration: DeepSeek V4 Flash thinker and Groq social reads are wired and verified. Usage/outcome accounting, shadow replay, and canary gates are specified in docs/08 and handoff section 14.
- Live execution is wired but remains disarmed; no mainnet execution is
      authorized. Run the funded throwaway-keypair devnet drill before any
      future arming discussion.

## Status
Live calibration day ~2. Fresh $1,000 book. Dashboard v2 shipped
(2026-08-25): ENTER/PASS feed labels, verbatim model answers + contract
address in feed detail, five-number portfolio stats panel; knowledge tab +
paper banner removed. App runnable via ./start.sh (rebuilds frontend/dist).
