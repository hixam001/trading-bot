# Trading-Bot: Static Knowledge Base

This document is the bot's starting curriculum — background knowledge about
Solana memecoins, rug-pull patterns, and evaluation heuristics that inform
the LLM's scoring decisions before it has built up its own trade history.

It is loaded on every tick and injected (up to the character budget defined in
`config.KB_MAX_CONTEXT_CHARS`) into the LLM's scoring prompt.

---

## Rug-Pull Red Flags

**High-risk patterns to watch for:**

- **Dev wallet dump**: Token creator's wallet sells a large portion within 24–48h
  of launch. Red flag if top holder % is high AND the token is very new.
- **Mint authority not renounced**: If the contract still allows minting new
  supply, the dev can inflate and dump at will. Always a hard rejection.
- **LP not locked**: Liquidity pool tokens not locked mean the LP can be
  withdrawn instantly, collapsing the price to near zero.
- **Honeypot**: Tokens where buy transactions succeed but sell transactions
  revert. Not detectable from market data alone — flag if sell volume is
  disproportionately low versus buy volume.
- **Concentrated ownership**: Any single non-LP wallet holding >15–20% is a
  major red flag. Coordinated dump from one wallet is trivially easy.
- **Fake social proof**: Paid Telegram groups, bot-driven Twitter followers,
  and copy-paste whitepapers. These are surface signals, not investible data.

---

## Solana Memecoin Lifecycle Patterns

**Typical memecoin lifecycle:**

1. **Launch phase (0–6h)**: Highest risk. Price discovery is chaotic. Bot
   trading and sniper activity dominate. Avoid unless liquidity is already
   substantial (>$50k).
2. **Distribution phase (6–48h)**: Early buyers distributing to retail. Volume
   often looks healthy but is partially wash-trading. Watch for holder count
   growth trend.
3. **Consolidation (2–7 days)**: If the project survives distribution, a real
   community emerges. Liquidity stabilises. This is the lower-risk entry window.
4. **Decay (7+ days)**: Attention fades. Volume drops. Most memecoins fail here.
   Avoid tokens older than 7 days unless there's a specific catalyst.

---

## Liquidity Interpretation

| Liquidity (USD) | Risk Level | Notes |
|-----------------|-----------|-------|
| < $5k           | Extreme   | Do not trade — any sell moves price significantly |
| $5k – $25k      | Very High | Price impact on entry/exit will be severe |
| $25k – $100k    | High      | Acceptable for small positions, high slippage |
| $100k – $500k   | Moderate  | More realistic execution, lower slippage |
| > $500k         | Lower     | Near-normal DEX execution quality |

**Rule of thumb**: The position size should be no more than 1–2% of pool
liquidity to avoid significant self-inflicted price impact.

---

## Volume / Liquidity Ratio (V/L Ratio)

A high V/L ratio (>5x daily volume vs. liquidity) can indicate organic
interest and active trading. A very low V/L ratio (<0.5x) suggests the token
is stagnant. Extreme ratios (>20x) can indicate wash-trading.

---

## Holder Count Interpretation

| Holder Count | Signal |
|-------------|--------|
| < 100        | Pre-distribution — not suitable for evaluation |
| 100 – 500    | Early stage — high concentration risk |
| 500 – 2,000  | Developing community — watch concentration trend |
| > 2,000      | Broader distribution — generally lower rug risk |

Note: Holder count alone is not sufficient — check concentration alongside it.

---

## Exit Discipline Heuristics

- **Take profit at defined levels** — emotional holding is the primary
  reason paper profits become losses. The invalidation condition set at
  entry should be honored even when the thesis feels directionally correct.
- **Time stops matter**: A position that doesn't move significantly within
  the expected time frame (24–72h for memecoins) is capital tied up
  unproductively. A timeout exit is not a failure.
- **Don't average down on memecoins**: Unlike established assets, memecoins
  do not "mean revert." A falling memecoin is more likely rugging than
  providing a better entry.

---

## What Makes a Good Paper-Trade Thesis

A strong thesis for this system should include:
1. **Specific entry condition**: Not "looks good" — e.g. "volume expanding
   while price consolidates above $0.000X support."
2. **Specific invalidation**: What price action or event would prove the
   thesis wrong — e.g. "price closes below 24h low" or "volume drops >50%."
3. **Time horizon**: Memecoins have short windows. Thesis should be valid
   for hours, not weeks.
4. **Why now**: What changed that makes this the right moment (catalyst,
   momentum shift, volume spike, consolidation break).

---

*This document is hand-curated by the operator. For operator-supplied external
material (rug-pull case studies, tokenomics guides, etc.), see the
`knowledge_base/ingested/` directory.*
