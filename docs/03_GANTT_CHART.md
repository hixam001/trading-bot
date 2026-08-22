# 03 — Build & Calibration Timeline

Renders natively as a Gantt chart on GitHub (Mermaid syntax). Covers the
initial build (days 1-4) and the live calibration window (days 4-10),
matching the "learn for at least 10 days before trusting the track record"
requirement.

```mermaid
gantt
    title trading-bot: build + 10-day calibration
    dateFormat YYYY-MM-DD
    axisFormat %d
    todayMarker off

    section Foundations
    Rule engine skeleton (RuleResult, GateDecision, evaluate_gate)   :f1, 2026-08-23, 1d
    Individual rule functions + unit tests (all 10 rules)            :f2, after f1, 1d
    Candidate model updated with new fields                          :f3, after f1, 1d

    section Data layer
    MarketDataProvider protocol defined                              :d1, 2026-08-23, 1d
    Birdeye provider (candidates, security)                          :d2, after d1, 1d
    Dexscreener provider (buy/sell counts)                           :d3, after d1, 1d
    Jupiter provider (execution price)                               :d4, after d1, 1d
    Mock provider parity with all new fields                         :d5, after d3, 1d
    Rate-limit handling + per-provider call counters                 :d6, after d5, 1d

    section Market regime
    MarketRegime + compute_market_regime()                           :r1, after f2, 1d
    market_regime table + logging                                    :r2, after r1, 1d
    market_regime_ok rule wired into gate                            :r3, after r2, 1d

    section Trading engine correctness
    Atomicity fix: open_position / close_position                    :t1, 2026-08-23, 1d
    Double-close / double-open tests                                 :t2, after t1, 1d
    scale_into_position() implemented                                :t3, after t2, 1d
    decide_and_act() unified entry point                             :t4, after t3, 1d

    section LLM layer
    Narration prompt rewrite (verdict pre-decided)                   :l1, after f2, 1d
    Groundedness validation + spot-check (15-20 real theses)         :l2, after l1, 1d
    Per-trade reflection wiring (fire-and-forget)                    :l3, after l2, 1d

    section Knowledge base
    Bulk ingestion CLI + API endpoint                                :k1, after f1, 1d
    Digest-at-ingest-time summarization                              :k2, after k1, 1d
    Context truncation fix (whole-document drop, not mid-cut)        :k3, after k2, 1d

    section API + Frontend
    FastAPI read endpoints (feed, holdings, journal, stats, etc.)    :a1, after t4, 2d
    WebSocket live feed                                               :a2, after a1, 1d
    Frontend core panels (feed, holdings, journal)                   :a3, after a2, 2d
    Frontend stats + safety panels (regime, promotion gate, KB)      :a4, after a3, 2d

    section Calibration window (10 days, live paper trading)
    Day 1-2: run against live data, confirm rule engine stable       :c1, 2026-08-29, 2d
    Day 3-4: LLM narration groundedness re-check under live data     :c2, after c1, 2d
    Day 5-6: daily rule-rejection review, first threshold tuning     :c3, after c2, 2d
    Day 6-7: optional advisory deep-dive layer (if core is stable)   :c4, after c3, 1d
    Day 8-9: market regime threshold calibration from real data      :c5, after c4, 2d
    Day 10: promotion gate first full review (informational only)   :c6, after c5, 1d
```

## Notes on sequencing

- **Foundations, data layer, and trading-engine correctness run in
  parallel** where possible — they're largely independent modules. The
  atomicity fix (`t1`/`t2`) should be treated as highest priority within
  its section: every trade closed before this fix is a potential silent
  double-credit, so don't let calibration data accumulate on top of it.
- **The rule engine and data layer must both be done before market regime
  and LLM narration work starts** (`market_regime_ok` needs a working
  gate to slot into; narration needs `GateDecision` objects to narrate).
- **API and frontend work is intentionally last** in the build sequence —
  it has no bearing on whether the underlying trading logic is correct,
  and building it against a stable backend avoids rework from API-shape
  changes made while the core logic is still settling.
- **The calibration window doesn't start until the build is functionally
  complete** — the 10-day clock is about producing a real, trustworthy
  track record against live market data, which requires the full pipeline
  (rule engine, regime gate, narration, atomic trading engine) already
  working correctly in mock mode first.
- **The optional advisory LLM layer (`c4`) is deliberately placed mid-way
  through calibration, not at the start** — don't add a second moving part
  while the core deterministic gate's thresholds are still being tuned;
  isolate what's changing at any given time.
- **Day 10's promotion gate review is explicitly informational** — per
  `promotion_gate.py`'s design, meeting its criteria never triggers
  anything automatically; it only tells you whether the numbers currently
  support a manual, separate decision about live trading, which remains
  entirely the operator's call, made outside this system.
