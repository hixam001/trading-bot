# FOMO Integration — wiring a real crowd_heat source

This document is the implementation path for replacing `crowd_heat`'s proxy
with a real conviction feed, mirroring how the reference bot does it.

## 1. What the reference bot' crowd_heat actually is

From their published source (`src/lib/pipeline.server.ts`):

```ts
function crowdHeat(intel, totalTheses) {
  const written = (intel?.theses.length ?? 0) + totalTheses;
  return Math.max(0, Math.min(100, Math.round(20 + written * 8)));
}
```

The "FOMO index" is **not sentiment analysis**. It is a count of written
theses that holders have attached to a token on the fomo.fun app, plus the
board's overall thesis volume. Each thesis carries the holder's position
size and P&L — conviction with money behind it. 20 + 8×theses, clamped
0–100: zero theses = heat 20 (nobody has put reasoning behind the token);
5 theses = heat 60.

Their gate refuses when heat is outside its act band — observed refusals at
heat 20–24 ("fomo reading inside the range i act in" fails), action around
25+. The band bounds are operator-tuned, not sacred.

## 2. Where we stand today — OPTION 1 (FOMO BOARD) IMPLEMENTED & LIVE-VALIDATED

`backend/data_providers/crowd.py` implements the fomo.fun board source with
fail-soft degradation, wired into `main.run_tick`'s read stage
(`crowd.enrich_crowd_heat(candidates)` fills `candidate.fomo_heat` +
`candidate.crowd_heat_source`; `compute_crowd_heat()` prefers that value and
falls back to the presence proxy when the feed didn't answer). The rule
detail is source-tagged in the journal: `heat 100 [fomo] ...` /
`heat 36 [proxy]`.

**Item #3 — dumped-author discount (2026-08-29, omo parity):** the thesis
feed carries each author's live position (`authorTrade`). A thesis whose
author **already closed at a realized profit** (`closedAt` set AND
`realizedPnlUsd > 0`) is exit-liquidity marketing, not live conviction:
`fetch_fomo_theses` still returns it (the thinker sees the author's P&L), but
crowd heat counts it at `config.FOMO_DUMPED_THESIS_WEIGHT` (default **0.0** —
dumped theses contribute nothing to the heat count) via the new
`effective_total` key. `total` stays the board's raw number. KNOWN-data
only: open positions, closed-at-a-loss, and rows without `authorTrade` keep
full credit — unknown is not the same claim as dumped (same B10 discipline
as `security_clear`). The discount applies only to rows we actually saw;
the unseen remainder of the board total keeps full credit (fail-soft).

**Live validation (2026-08-23, operator keys):** direct reads are
Cloudflare-challenged even with a valid bearer; the Firecrawl stealth
fallback returned REAL board data — e.g. mint F8hVFDi8…: **40 theses →
heat 100 [fomo]**, with author positions (@LegendPxiin holding $476,
@themaxxr $743) and thesis texts (nested at `item.comment.comment`).

Setup:
1. **FOMO board**: log into fomo.family once, DevTools → Application →
   Local Storage → copy your Privy refresh token → put it in `.env` as
   `FOMO_PRIVY_REFRESH_TOKEN=...`. The bot exchanges it for a ~1h access
   token automatically (single-flight, cached) and reads
   `prod-api.fomo.family/feed/token/thesis?tokenAddress=<mint>...`.
   NOTE: Privy ROTATES refresh tokens on each use — if logs show
   privy[...]401, re-extract a fresh one after a re-login.
2. **FIRECRAWL_API_KEY** in `.env` (firecrawl.dev) — required for the
   stealth-scrape fallback. API host is firecrawl.DEV (the old .app host
   is TLS-dead).

Feed is optional: unreachable ⇒ proxy heat, tagged `[proxy]`.
(pump.fun comments: evaluated and DEFERRED — see Option B below.)

`backend/rule_engine/rules.py::compute_crowd_heat()` implements the **same
20 + 8×items formula**; when no feed answered it uses named presence
channels (twitter / telegram / website) as the conviction proxy, and
`config.CROWD_HEAT_MIN/MAX` is the act band.

## 3. Implementation options, in order of fidelity

### Option A — read the fomo.fun board directly (what the reference does)

fomo.fun has **no public API** (the reference states this explicitly: "fomo has no api
and does not need one" — it's a read layer over a Solana wallet). But its
web app obviously fetches theses from somewhere. the reference's `fomo.server.ts`
reads that same board server-side.

Steps:
1. Open a token page on fomo.fun in Chrome, DevTools → Network → XHR/Fetch.
2. Find the request that returns the theses list for the mint (JSON with
   thesis text, author wallet, position size, P&L). Note the exact URL
   pattern, required headers, and whether it needs auth cookies.
3. Build `backend/data_providers/fomo.py`:

```python
class FomoProvider:
    async def get_board_intel(self, mint: str) -> dict | None:
        # {"theses": [{"text":..., "size_usd":..., "pnl_usd":...}], "total": n}
        # bounded retries + 15s timeout + 60s cache per mint (parity with
        # our other providers; reuse data_providers.base.fetch_json)
```

4. Change `compute_crowd_heat()` to:

```python
heat = min(100, config.CROWD_HEAT_BASE + config.CROWD_HEAT_PER_SIGNAL * len(intel["theses"]))
```

— the formula is already identical; only the input source changes.

5. **Degradation semantics** (match the reference's "degraded stage" honesty): if the
   feed is unreachable, fall back to the presence proxy AND tag the rule
   detail with `(degraded: fomo feed down)` so the journal shows it. Do NOT
   silently pretend the board was read.

Caveats: internal endpoints change without notice; check fomo.fun's terms
before scraping; cache aggressively (60s+) and never let a FOMO outage stall
the exit loop (it can't — exits never touch this feed).

### Option B — pump.fun comments — DEFERRED

Evaluated and shelved on 2026-08-23: the legacy `frontend-api.pump.fun` host
is dead (HTTP 530), `advanced-api-v2.pump.fun/replies/...` 404s, the moved
`frontend-api-v2.pump.fun/replies/coins/{mint}` route exists but 503s even
through Firecrawl stealth. If pump's backend stabilizes, re-add a small
client in this package (the TTL-cache + fail-soft transport in crowd.py is
reusable as-is) and extend `_heat_for_mint()` with a second branch.

### Option C — Dexscreener boosts (already reachable)

We already read DexScreener. `token-boosts/top/v1` gives paid-boost counts.
Boosts are *paid*, so they measure marketing more than conviction — use as
a secondary input (the reference explicitly flags boosted tokens and often refuses
them), never as the primary heat source.

### Option D — build our own conviction metric

Aggregate from our own journal: how many distinct ticks a mint stayed
interesting, LLM thesis sentiment, social velocity from the new-listing
feed. Weakest signal (self-referential), but zero external dependencies.

## 4. Recommendation

Start with **Option A** (it's what makes our crowd_heat semantically equal
to the reference's), keep the presence proxy as the automatic degraded fallback, and
add Option B as a secondary input only if A proves fragile. The act band
(`CROWD_HEAT_MIN/MAX`) is where calibration happens — tune it from the
rejection breakdown in the dashboard after a week of real data, one
threshold at a time.
