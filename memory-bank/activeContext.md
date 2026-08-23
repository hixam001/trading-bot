# Active Context — trading-bot

**As of 2026-08-23 (omo-mimicry rebuild in progress).** Repo:
`/home/hixam/Downloads/Projects/trading-bot/`.

## Current focus: rebuilding the bot on omotrades' logic
Trigger: DB forensics showed −60% portfolio ($1,000 → $113 cash): 25 closed
trades, 16% win rate, stops realizing −40% avg on a −20% config, and DONT
re-entered 15×/stopped out 15× for −$709. User approved FULL mimicry of
omotrades' logic including the think→gate intersection (qwen3 verdict = a
necessary veto layer). Source-level study: omo's fomo.server.ts,
fomo-auth.server.ts, pipeline.server.ts, audit.server.ts, market.server.ts,
execute.server.ts, blocklist.ts, PROCESS.md.

## DONE
### Exit engine (81b9898) — rule_engine/exits.py
omo's exit set ported: stop −20%, trail 50%-activation/40pp give-back vs
persisted HWM, liquidity break <$8k, invalidation −25%&1.4×sells, stale
14d, TP ladder +100/300/900 trims 33/33/50%. Sell risk gate ($25 clip,
30-min/mint cooldown, ≤8 exits/24h; risk-off BYPASSES gate — documented
deviation). E8/E9 partial closes via trim_position (atomic).
main(): dedicated 15s price-only exit scan loop alongside tick. DB
migration: trades.high_water_usd + trades.tranches_taken.

### Old logic purged + entry gate = omo verbatim (ebdd1a1)
liq ≥$15k, vol ≥$8k, newborn 24h/−15%, strict already_held (scale-ins
REMOVED entirely), not_on_break liveness (rule_engine/liveness.py),
crowd_heat act-band [36,100]. exposure_cap + volume_mcap_ratio_ok deleted
with constants/tests. Old TAKE_PROFIT_PCT removed (ladder governs).

### Crowd conviction feed LIVE-VALIDATED (a14be2c…50232ed)
data_providers/crowd.py reads the REAL fomo.fun board:
prod-api.fomo.family/feed/token/thesis via Privy session with
AUTO-RENEWING auth — rotated refresh tokens captured from every mint
response and persisted to gitignored .fomo_privy.json (state file beats
stale .env bootstrap). Transport: full browser header set (origin/referer/
x-supported-chains were missing in our first attempt → that's why direct
reads 403'd), sequential queue w/ 220ms gap, TTL dedupe, junk filter,
board-total extraction (olderThesis+newerThesis+page). Stealth-scrape
FAILOVER CHAIN when challenged: firecrawl → scrapingbee → scrapingdog →
zenrows → scrapeops (quota-exhausted providers benched 30 min; firecrawl
API host is firecrawl.DEV now — .app host TLS-dead). Enrichment runs ONLY
when DATA_BACKEND=live (mock hermeticity: a real feed answering for mock
mints once flipped all e2e verdicts). Live proof: mint F8hVFDi8… → 40
board theses → heat 100 [fomo] with author positions parsed.
NOTE: privy refresh tokens ROTATE on use — if logs show privy[...]401,
re-extract fresh from a fomo.family re-login (dedicated browser profile).

## REMAINING phases (each its own commit)
2. Loop reorder manage-first + decision_commits seal/reveal audit table
3. Think stage: qwen3 pre-trade {thesis, invalidation, verdict}; trade
   requires verdict==buy AND all rules pass; Ollama-down ⇒ fail-closed
   model_unavailable refusal; template thinker for hermetic tests
4. Discovery rotation: DexScreener keyword-pool scanner (~45 queries),
   fake-chart filter, boost flags
5. Conviction sizing min(cash×15%, cap)×conviction + daily notional cap
6. Local proof.json/exits.json endpoints + replay harness over historical
   snapshots (incl. DONT corpus)

## Key evidence to remember
- DONT: 15 entries, 15 stop-outs, −$708.92; holds ~25s; re-entry every tick
- Stops realized −40.2% avg vs −20% config: fixed by fast scan loop
- Winners existed: 4 TPs avg +59.6% — old +50% TP capped them; ladder fixed

## Watch-outs
- ⚠ TERMINAL: login shell is FISH — no `$?`, no heredocs; `bash -c` quoting
  breaks silently. Write bash scripts to files; read outputs from /tmp files.
- pytest canonical: cd backend && ../.venv/bin/python -m pytest tests/ -q
- Suite now: backend 136, combined root 184 (root pytest.ini asyncio_mode)
- isolation grep (backend must not mention live_execution) must stay clean
- .env.example documents ALL env fields incl. crowd-feed keys; user has
  FOMO_PRIVY_REFRESH_TOKEN + FIRECRAWL_API_KEY filled

