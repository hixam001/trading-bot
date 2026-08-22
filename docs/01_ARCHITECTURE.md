# 01 — Architecture

## 1. Core principle: separate deciding from explaining

The system has two clearly separated responsibilities that must never be
merged into one step:

- **The rule engine decides.** A pure, deterministic, fully unit-tested
  set of Python functions evaluates every candidate against a fixed set of
  named checks. Given the same inputs, it always produces the same
  verdict. It runs in microseconds, costs nothing, and requires no LLM call
  to function.
- **The LLM explains.** Its only job is to turn an already-computed
  decision into a short, specific, grounded natural-language thesis. It
  never overrides the rule engine's verdict, and it is structurally
  prevented from asserting anything not present in the data it was handed.

This split is what makes the system's output trustworthy: a reader of the
trade journal can always trace an "enter" or "reject" back to specific,
checkable numbers, and the LLM's prose can never introduce an unverified
claim into that trail.

## 2. The rule engine

### 2.1 Data contracts

```python
@dataclass(frozen=True)
class RuleResult:
    rule_id: str            # stable, short, snake_case identifier
    passed: bool
    detail: str              # human-readable, must include the real number(s) behind it
    value: float | int | bool | None = None   # raw underlying value, for logging/calibration

@dataclass(frozen=True)
class GateDecision:
    candidate: "Candidate"
    rules: list[RuleResult]
    all_passed: bool
    failed_rule_ids: list[str]

RuleFn = Callable[["Candidate", "PortfolioState", "MarketRegime"], RuleResult]
```

### 2.2 Evaluation

```python
def evaluate_gate(
    candidate: Candidate,
    portfolio: PortfolioState,
    regime: MarketRegime,
    rules: list[RuleFn],
) -> GateDecision:
    results = [rule(candidate, portfolio, regime) for rule in rules]
    failed = [r.rule_id for r in results if not r.passed]
    return GateDecision(candidate, results, all_passed=(len(failed) == 0), failed_rule_ids=failed)
```

Every rule in `rules` is evaluated independently and unconditionally —
there is no short-circuiting. This matters: even if an early rule fails,
every later rule's result is still computed and logged, so a rejected
candidate's *full* profile is visible in the journal, not just the first
reason it failed.

### 2.3 The rule set

| `rule_id` | Logic | Notes |
|---|---|---|
| `liquidity_floor` | `liquidity_usd >= config.MIN_LIQUIDITY_USD` | Pool must be deep enough to realistically exit the position size being considered |
| `volume_alive` | `volume_1h_usd >= config.MIN_VOLUME_1H_USD` | Rejects dead/stale tape |
| `buy_pressure` | `buys_1h > sells_1h` | Raw transaction-count comparison, not dollar volume |
| `not_newborn_fade` | `NOT (age_hours < config.NEWBORN_AGE_HOURS AND price_change_1h_pct <= -config.NEWBORN_FADE_PCT)` | A joint rule — young age alone or a dip alone doesn't fail it; the combination does. Protects against buying a fresh launch that's already crashing. |
| `public_presence` | `has_twitter OR has_telegram OR has_website` | Any one channel is sufficient |
| `market_regime_ok` | `regime.regime_ok` | Shared across every candidate evaluated in the same tick — see §3 |
| `cash_available` | `portfolio.cash_usd >= intended_position_size_usd` | |
| `exposure_cap` | `portfolio.held_usd_in_mint(candidate.mint_address) < config.MAX_EXPOSURE_PER_MINT_USD` | Gates both first entries (starts at $0 exposure) and scale-ins into an existing position uniformly |
| `security_clear` | `NOT (mint_authority_revoked == False) AND NOT (is_likely_honeypot == True)` | **Only asserts when the underlying field is known.** `None` (unknown) always passes this rule — unknown is not the same claim as unsafe. |
| `volume_mcap_ratio_ok` | `(volume_24h_usd / max(market_cap_usd, 1)) >= config.MIN_VOLUME_MCAP_RATIO` | Volume meaningfully below market cap is a manipulation/bundling signal |

Every rule is a pure function with no side effects, tested independently
with hand-built fixtures covering both its pass and fail branch — this is
non-negotiable given these functions are the actual trading
decision-makers, not advisory input to something else.

### 2.4 What happens with the result

- **`all_passed == True`** → the candidate proceeds to
  `paper_trading_engine`. If no open position exists for this mint, this is
  a new entry (`open_position`); if one exists, this is a scale-in
  (`scale_into_position`) — see §5.
- **`all_passed == False`** → the candidate is rejected. It is still logged
  as a full feed event with every `RuleResult` attached (not just the
  first failure), and still sent to the LLM narrator (§4.1), so the
  journal shows exactly why every rejection happened, in the same detail
  as every acceptance.

## 3. Market regime gate

### 3.1 Purpose

Individual-candidate rules alone are not sufficient — a rule engine that
only ever looks at one token in isolation has no way to recognize when the
*overall* market is in a state worth avoiding entirely (broad, simultaneous
euphoria across many tokens at once often precedes a broad reversal; a
totally dead, directionless tape across the whole candidate universe is a
weak environment for entries regardless of any single token's stats). This
gate makes "is now a good time to be trading at all" an explicit, logged,
first-class decision — not an emergent side effect of per-token filtering.

### 3.2 Design

Computed **once per tick**, from that tick's full candidate batch, before
any individual candidate is evaluated:

```python
@dataclass(frozen=True)
class MarketRegime:
    computed_at: str
    pct_candidates_green_1h: float
    median_volume_1h_usd: float
    avg_buy_sell_ratio: float
    regime_ok: bool
    regime_detail: str


def compute_market_regime(candidates: list[Candidate]) -> MarketRegime:
    if not candidates:
        return MarketRegime(
            computed_at=_now_iso(), pct_candidates_green_1h=0, median_volume_1h_usd=0,
            avg_buy_sell_ratio=0, regime_ok=False, regime_detail="no candidates this tick",
        )

    pct_green = sum(1 for c in candidates if c.price_change_1h_pct > 0) / len(candidates)
    median_vol = statistics.median(c.volume_1h_usd for c in candidates)
    avg_ratio = statistics.mean(c.buys_1h / max(c.sells_1h, 1) for c in candidates)

    regime_ok = (
        config.REGIME_MIN_PCT_GREEN <= pct_green <= config.REGIME_MAX_PCT_GREEN
        and median_vol >= config.REGIME_MIN_MEDIAN_VOLUME_USD
    )
    detail = f"{pct_green:.0%} green, median 1h vol ${median_vol:,.0f}, avg buy/sell ratio {avg_ratio:.2f}"

    return MarketRegime(
        computed_at=_now_iso(), pct_candidates_green_1h=pct_green,
        median_volume_1h_usd=median_vol, avg_buy_sell_ratio=avg_ratio,
        regime_ok=regime_ok, regime_detail=detail,
    )
```

### 3.3 Threshold calibration

`REGIME_MIN_PCT_GREEN`, `REGIME_MAX_PCT_GREEN`, and
`REGIME_MIN_MEDIAN_VOLUME_USD` start as placeholder values (documented in
`config.py` as explicitly needing calibration) and are tuned from real
paper-trading data during the 10-day window — see `03_GANTT_CHART.md`. A
reasonable starting intuition: reject the regime if an unusually high
fraction of the entire candidate universe is simultaneously green (more
consistent with a broad, possibly manipulated pump than organic
per-token strength) or if overall volume across the universe is
suspiciously thin.

### 3.4 Logging

Stored once per tick in its own table (`market_regime`), independent of
individual candidate/trade records — this lets regime history be reviewed
on its own timeline, separate from trade-level analysis.

## 4. The LLM's role

### 4.1 Narration (every gate decision)

Given an already-computed `GateDecision`, the LLM's only task is producing
a short thesis grounded strictly in the rule results it was handed:

```
You are narrating a trading decision that has ALREADY been made by a
deterministic rule system. Do not second-guess the verdict. Do not invent
information not present below. Write 1-2 sentences explaining the decision
using ONLY the rule results and numbers given.

Verdict: {"ENTER" if gate_decision.all_passed else "REJECT"}
Rules checked:
{for each RuleResult: "- {rule_id}: {'PASS' if passed else 'FAIL'} ({detail})"}

If REJECT: name the specific failing rule(s), using their actual numbers.
If ENTER: state which factors support entry, using actual numbers.
Do not mention any check not listed above.
```

Validation on the response is minimal by design, since there's no
pass/fail field to parse anymore (the verdict is already known): confirm
the returned text is non-empty, and run a lightweight check that it
doesn't reference terms tied to rules absent from this candidate's actual
rule list (e.g. flag if "honeypot" or "mint authority" appear in the
thesis but `security_clear` wasn't in the rules passed to the prompt).

This design is what prevents the earlier class of grounding failure
structurally rather than through instruction alone: the LLM is narrating
data it was handed, not producing an open-ended risk assessment from
scratch, so there's no gap for it to fill with plausible-sounding but
unsupported claims.

### 4.2 Optional advisory deep-dive (later phase, not part of initial build)

For candidates that already passed the deterministic gate, an optional
second LLM pass can add judgment on top — cross-referencing the ingested
knowledge base for patterns the rule engine doesn't capture, or (in a
future extension) live web search for recent news/context. This step can
only **add** a risk flag and lower displayed confidence; it can never flip
a rule-engine rejection into an acceptance, and it can never force-close a
position the rule engine's exit checks didn't flag. This preserves the
deterministic gate as the sole source of truth for what actually happens
to the paper portfolio, while still allowing room for softer judgment to
be visible in the record.

## 5. Trading engine: unified entry and scale-in

One function handles both new entries and adding to existing winners,
differentiated only by what `exposure_cap` (§2.3) checks against:

```python
async def decide_and_act(
    candidate: Candidate,
    portfolio: PortfolioState,
    regime: MarketRegime,
    conn: aiosqlite.Connection,
) -> GateDecision:
    gate = evaluate_gate(candidate, portfolio, regime, ACTIVE_RULES)

    if gate.all_passed:
        existing = portfolio.get_open_trade_for_mint(candidate.mint_address)
        if existing is None:
            await engine.open_position(conn, candidate, gate)
        else:
            await engine.scale_into_position(conn, existing, candidate, gate)

    return gate  # always logged, regardless of outcome
```

### 5.1 Atomicity and idempotency (applies to every state-changing function here)

Every function that both changes cash balance and writes/updates a trade
row must do so in a way that survives a crash between the two writes
without corrupting the record:

- Write the state-changing row first, using a conditional write whose
  `WHERE` clause makes a repeated attempt a no-op (e.g. closing a trade
  updates `WHERE trade_id = ? AND is_open = 1` and returns the affected row
  count).
- Check whether the write actually affected a row. If it affected zero
  rows, the operation already happened (a retry or race) — log this and do
  **not** re-apply the cash change.
- Only touch cash after confirming the state write actually took effect.

This applies identically to `open_position`, `close_position`, and the new
`scale_into_position` — all three combine a cash change with a trade-state
change, and all three must follow this pattern without exception. This is
the single most consequential correctness property in the system: a
mistake here silently fabricates or destroys paper money, which corrupts
every downstream statistic, the promotion gate, and the daily learning
loop.

### 5.2 Exit conditions

Checked each tick against every open position, independent of the entry
rule engine:

- **Take-profit**: unrealized gain ≥ `config.TAKE_PROFIT_PCT`.
- **Stop-loss**: unrealized loss ≥ `config.STOP_LOSS_PCT`.
- **Timeout**: held longer than `config.MAX_HOLD_HOURS`.

All three checks use `compute_unrealized_pnl()`, which itself accounts for
simulated round-trip slippage and fees so the displayed unrealized P&L
reflects a realistic estimate of what would actually be received on exit,
not a naive mark-to-market number.

### 5.3 Partial position scaling — elevated priority (revised)

A closer read of the reference system's live decision stream shows that
the majority of its ongoing reasoning is about *existing* positions —
trimming a fraction on a structural breakdown, adding more to a winner on
a confirmed volume/trend reclaim — not new entries. This was originally
scoped as low-priority future work in this project; that scoping is
revised here, because it's clearly more central to how a mature version of
this system actually behaves day-to-day than a binary open/close model
captures.

This does **not** need to be built in the initial pass (§5.2's binary
model is a reasonable, simpler starting point, and the calibration window
should run against something stable before adding this complexity), but
the trading engine should be designed with the seam for it from the start
rather than retrofitted later:

```python
async def scale_out_partial(
    conn: aiosqlite.Connection,
    trade: Trade,
    fraction: float,          # e.g. 0.33 to trim a third
    exit_price: float,
    exit_reason: str,
) -> Trade:
    """
    Closes `fraction` of an open position, leaving the remainder open with
    an adjusted position_size_usd/quantity. Follows the exact same
    atomicity discipline as close_position() (§5.1): the state write
    (reducing the trade's size) must be confirmed before any cash is
    credited, and a repeated call for an already-trimmed fraction must be
    a safe no-op, not a double-credit.
    """
```

Two new triggers become possible once this exists, both driven by the same
rule-engine philosophy as everything else in this document — fixed,
numeric, checkable conditions, not LLM judgment calls:

- **Structural trim**: reduce a position by a configured fraction if price
  closes below a tracked recent local low (a "shelf" level) while still
  showing an overall unrealized gain — locking in some profit without
  fully exiting a thesis that may still be intact.
- **Confirmed add**: increase an existing position (still gated by
  `exposure_cap`, §2.3) if volume reclaims a configured level alongside a
  positive short-term trend confirmation.

Both require tracking a short rolling price/volume history per open
position (not currently part of the data model) — this is new scope, and
should be estimated and scheduled deliberately rather than assumed free.
Recommended sequencing: build this only after the binary-exit version has
run through a full 10-day calibration cycle and produced a real track
record to compare against — introducing partial-exit complexity before the
simpler model has a baseline makes it impossible to tell whether a change
in outcomes came from the new logic or from normal market variance.

### 5.4 Future scaling note: direct RPC/mempool subscriptions

The reference system's own notes mention establishing direct validator RPC
subscriptions for lower-latency order/mempool tracking, rather than relying
solely on periodic polling of REST APIs. This is real infrastructure
sophistication appropriate for a system running significant real capital
at meaningful frequency — it is **not appropriate or necessary for this
project's current scope** (local, paper-trading, free-tier REST APIs,
tick interval measured in tens of seconds to minutes). Documented here as
a known future scaling path, in the same category as
`05_VERIFICATION_APPENDIX.md`'s commit-reveal mechanism: relevant once
real capital and real latency sensitivity are in play, not before.

## 6. Data provider abstraction

### 6.1 Interface

```python
class MarketDataProvider(Protocol):
    async def get_candidates(self, limit: int) -> list[Candidate]: ...
    async def get_current_price(self, mint_address: str) -> float: ...
    async def get_security_info(self, mint_address: str) -> SecurityInfo: ...
```

`SecurityInfo` carries `mint_authority_revoked: Optional[bool]`,
`freeze_authority_revoked: Optional[bool]`, `is_likely_honeypot:
Optional[bool]` — always `None`, never `False`, when a source didn't
actually return a value.

### 6.2 Provider split

No single provider is asked to do everything — each is used for what it's
actually strong at:

| Need | Provider | Notes |
|---|---|---|
| Candidate discovery, liquidity, volume, price, market cap | Birdeye | Free tier, generous for candidate discovery |
| Buy/sell transaction counts (`buys_1h`, `sells_1h`) | Dexscreener | Exposes `txns.h1.{buys,sells}` directly on its pair endpoint; no API key needed for basic use |
| Security checks (mint/freeze authority, honeypot) | Birdeye `token_security` endpoint | |
| Live execution-quality price for open positions | Jupiter quote API | Reflects real swap pricing, not just a displayed aggregator price |

### 6.3 Treating every free tier as temporary

- Every external call goes through explicit timeout + bounded retry with
  backoff (never an unbounded retry loop, never a bare try/except that
  swallows the failure silently).
- HTTP 429 responses trigger a longer, distinct backoff and a
  `rate_limited` log event, separate from generic failures — this is a
  capacity signal, not just an error.
- A simple per-provider daily call counter (one row per provider per day)
  gives early warning of approaching a free-tier ceiling before it's hit
  mid-session.
- Because every provider sits behind `MarketDataProvider`, outgrowing a
  free tier or a provider changing its API means writing one new class —
  never touching the rule engine, the LLM layer, or the tick loop.

## 7. API layer

FastAPI, entirely read-only with respect to trading decisions and safety
flags — every endpoint reports state, none of them can open, close, or
modify a trade, or change `PAPER_TRADING_ONLY`.

| Endpoint | Purpose |
|---|---|
| `GET /api/feed` | Paginated decision feed — every `GateDecision`, pass or fail, with full rule breakdown and LLM thesis |
| `GET /api/holdings` | Open positions with live current price and unrealized P&L, computed per request |
| `GET /api/journal` | Closed trades with full lifecycle: entry, exit, thesis, reflection, realized P&L |
| `GET /api/stats` | Portfolio summary: cash, win rate, profit factor, drawdown, equity curve series |
| `GET /api/knowledge-base` | Static + ingested knowledge content, plus dynamic win-rate-by-bucket stats |
| `GET /api/promotion-gate` | Current status of every promotion criterion |
| `GET /api/market-regime` | Recent regime history |
| `GET /api/system-status` | Ollama reachability, active model, per-provider daily call counts |
| `WS /ws/feed` | Real-time push of new feed events as ticks happen |
| `POST /api/knowledge-base/ingest` | Add new material to the knowledge base (file upload or batch) |

## 8. Frontend

React dashboard, dark theme, dense/terminal-style layout:

- **Live feed** — every decision, newest first, expandable to show the
  full rule breakdown and thesis; auto-updates via WebSocket.
- **Holdings** — open positions with live price and color-coded unrealized
  P&L.
- **Journal** — closed trades, filterable/sortable, each showing its
  original thesis next to its actual outcome and reflection.
- **Stats dashboard** — equity curve, win rate, profit factor, drawdown,
  learning-window progress.
- **Market regime panel** — recent regime history, so "why did the bot go
  quiet for an hour" is answerable from the dashboard itself.
- **Promotion gate panel** — a status display, never a control.
- **Knowledge base panel** — static + ingested content, dynamic stats.
- **System status** — Ollama connectivity, active model, provider call
  budgets — so a disconnected LLM or an exhausted free tier is visible
  immediately, not a silent failure discovered later.
- A persistent, unmissable "PAPER TRADING — NO REAL FUNDS" indicator,
  present on every view.

## 9. Testing and verification strategy

- **Rule engine**: every rule function gets unit tests for both its pass
  and fail branch, using hand-built `Candidate`/`PortfolioState`/
  `MarketRegime` fixtures — no live data or LLM needed to test the actual
  decision logic.
- **Money math**: `compute_unrealized_pnl`, `compute_realized_pnl`,
  position-sizing functions each get tests with known-correct expected
  outputs, including edge cases (zero/negative inputs raise, not silently
  return zero).
- **Atomicity**: a dedicated test simulates a double-close and a
  double-open call and asserts cash is only ever credited/debited once.
- **API**: each read endpoint tested against a seeded test database for
  correct shape and pagination behavior.
- **End-to-end (mock mode)**: the full tick loop run against synthetic
  candidates, confirmed to produce feed events, open/close positions
  correctly, and populate the journal and stats endpoints — this is the
  standing smoke test that should pass before any live data is trusted.
