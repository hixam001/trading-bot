# Active Context — trading-bot

**As of 2026-08-22.** Full detail: `handoff.md` (root) — read it first.

## Current focus
**Live calibration window, day 0–1.** The app runs against real market data
(DATA_BACKEND=live) with simulated funds ($1,000 fresh book after the data
corruption reset). The gate is correctly keeping the book flat during
low-volume periods (median 1h vol < $20k placeholder floor).

## Recent changes (chronological, most recent last)
1. Ground-up rebuild to revised architecture; backend/ + frontend/ split
2. Live providers wired and verified (memepool trending, Dexscreener
   enrichment, Jupiter lite-api); token_security free-tier auto-disable
3. CRITICAL FIX: Jupiter decimals bug (BARRON 6-dec token priced 1000× →
   fake +96k% closes, cash $481k) — decimals now threaded end-to-end,
   fail-closed without them, regression tests added; corrupted DB wiped
4. One-click launcher (start.sh/stop.sh/.desktop); backend serves dashboard
5. omotrades.com reference comparison (docs/06) — commit-reveal hashes
   verified byte-for-byte; agent-vs-rules divergence documented
6. Project report (docs/07) with every rule detailed

## Next steps
1. Let calibration run; check dashboard + logs/backend.log daily
2. On first entries/exits: sanity-check realized P&L (decimals fix proof)
3. Daily learning-loop review → tune ONE config threshold at a time
4. Post-calibration backlog: E8/E9 partial scaling, D7 advisory layer,
   commit-reveal proof only if going public

## Watch-outs for the next session
- App may already be running (start.sh is idempotent; stop.sh to stop)
- Birdeye free tier: token_security 401s are EXPECTED (auto-disabled);
  security fields stay UNKNOWN — do not "fix" by coercing to False
- Narration latency: ticks 40–90s for 20 candidates is normal
- If prices look absurd again: decimals are the first suspect
  (see handoff.md §5.8)
