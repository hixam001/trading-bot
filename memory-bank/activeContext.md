# Active Context — trading-bot

**As of 2026-08-23 (omo-mimicry rebuild UNDERWAY).** Repo:
`/home/hixam/Downloads/Projects/trading-bot/`.

## Current focus: rebuilding the bot on omotrades' logic (7-phase plan)
Motivation: DB forensics showed the book lost −60% ($1,000 → $113 cash):
25 closed trades, 16% win rate, stops realizing −40% avg on a −20% config,
and DONT re-entered 15×/stopped out 15× for −$709. Full comparison +
forensics delivered this session; plan approved by user ("full mimicry"
of omo's think→gate intersection).

## DONE — Phase 1 (commit 81b9898): exit engine + fast scan loop
rule_engine/exits.py: 6-rule engine ported from omo PROCESS.md §5
(stop −20%, trail 50%-activation/40pp give-back vs persisted HWM,
liquidity break <$8k, invalidation −25%&1.4×sells, stale 14d,
TP ladder +100/300/900 trims 33/33/50%) + sell risk gate ($25 clip,
30-min/mint cooldown, ≤8 exits/24h; RISK-OFF BYPASSES gate — documented
deviation). E8/E9 partial closes via trim_position (atomic).
main(): dedicated 15s price-only exit loop alongside tick. DB migration:
trades.high_water_usd + trades.tranches_taken. Tests: backend 119/119,
combined root 167/167.

## REMAINING phases (each its own commit)
2. Loop reorder manage-first + decision_commits seal/reveal table
3. Think stage: qwen3 pre-trade {thesis, invalidation, verdict};
   trade requires verdict==buy AND all rules pass; Ollama-down ⇒
   model_unavailable refusal (fail-closed); template thinker for tests
4. Entry rules verbatim: liq ≥$15k, vol ≥$8k, newborn 24h/−15%,
   strict already_held (NO scale-ins), not_on_break liveness,
   crowd_heat proxy (20+8×signals)
5. Discovery: rotating DexScreener keyword pool (~45 queries), fake-chart
   filter, boost flags, blocklist module (auto-grow on 2 consecutive stops)
6. Conviction sizing min(cash×15%, cap)×conviction + daily notional cap
7. Local proof.json/verify.json/exits.json endpoints + replay harness
   (backtest new pipeline over historical snapshots incl. DONT corpus)

## Key evidence to remember
- DONT: 15 entries, 15 stop-outs, −$708.92; holds ~25s; re-entry every tick
- Stops realized −40.2% avg (config said −20%): fixed by fast scan loop
- Winners exist: 4 TPs avg +59.6% in 3.7h — old +50% TP capped them;
  ladder lets winners run now

## Watch-outs
- ⚠ TERMINAL: login shell is FISH — no `$?`, no heredocs, `bash -c` quoting
  breaks silently. Write scripts to files or run simple commands.
- pytest canonical: cd backend && ../.venv/bin/python -m pytest tests/ -q
- live_execution/state/ gitignored; isolation grep must stay clean
- Old +50% TAKE_PROFIT_PCT still in config but unused by the new engine
  (ladder replaced it) — remove during calibration cleanup

