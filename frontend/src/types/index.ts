// Shared API response types (mirror of the backend models).

export interface RuleResultRow {
  rule_id: string
  passed: boolean
  detail: string
  value: number | string | boolean | null
  // §43: false = the rule was deliberately not evaluated for this candidate
  // (the metered crowd feed is only queried for candidates that cleared every
  // other rule). Absent on rows written before §43 — treat missing as true.
  evaluated?: boolean
}

export interface FeedEventRow {
  id: number
  ts: string
  symbol: string
  mint_address: string
  candidate_snapshot: Record<string, unknown>
  verdict: 'pass' | 'fail'
  thesis: string
  rule_breakdown: RuleResultRow[]
  failed_rule_ids: string[]
  regime_ok: boolean
  grounding_flags: string[]
  narration_source: string
  led_to_trade_id: string | null
}

export interface RegimeRow {
  computed_at: string
  candidate_count: number
  pct_candidates_green_1h: number
  median_volume_1h_usd: number
  avg_buy_sell_ratio: number
  regime_ok: boolean
  regime_detail: string
}

export interface LlmUsageRow {
  id: number
  ts: string
  task: string
  provider: string
  model: string
  status: string
  latency_ms: number | null
  input_tokens: number | null
  cache_hit_tokens: number | null
  output_tokens: number | null
  total_tokens: number | null
  estimated_cost_usd: number | null
  is_peak_window: number
  degradation_reason: string | null
}

export interface SystemStatusResponse {
  paper_trading_only: boolean
  data_backend: string
  main_llm_reachable: boolean
  main_llm_provider: string
  narration_mode: string
  provider_calls_today: {
    provider: string
    day: string
    call_count: number
    error_count: number
    rate_limit_429_count: number
    last_call_at: string | null
  }[]
  llm_usage_recent: LlmUsageRow[]
  tick_interval_seconds: number
}

/** GET /api/stats — the book-level performance summary (§58: rendered by
 *  the Performance panel; every value verbatim, null stays `—`). */
export interface StatsResponse {
  initial_cash_usd: number
  cash_usd: number
  equity_usd: number
  open_positions: number
  closed_trades: number
  win_rate: number | null
  profit_factor: number | null
  max_drawdown_pct: number | null
  total_pnl_usd: number
  realized_pnl_usd: number
  unrealized_pnl_usd: number | null
  total_spend_usd: number
  equity_curve: { closed_at: string; equity_usd: number }[]
  paper_trading_only: boolean
}

export interface LivePositionRow {
  mint_address: string
  symbol: string
  cost_usd: number
  tokens: number
  entry_price_usd: number | null
  current_price_usd: number | null
  value_usd: number
  unrealized_pnl_usd: number | null
  opened_at: string | null
}

export interface LivePortfolioResponse {
  enabled: boolean
  reason?: string
  armed?: boolean
  manual_confirmation?: boolean
  wallet?: string
  cash_usd?: number | null
  sol_balance?: number | null
  equity_usd?: number | null
  open_value_usd?: number
  unrealized_pnl_usd?: number | null
  realized_pnl_usd?: number
  deployed_today_usd?: number
  closed_trades?: number
  /** §60 wallet scan: chain truth on quantities, TTL-cached (see live_book). */
  chain_scan?: { checked: boolean; stale: boolean; at_utc: string | null }
  /** Journal positions the chain says are gone (sold out-of-band). */
  chain_excluded?: { mint: string; tokens: number; cost_usd: number }[]
  positions?: LivePositionRow[]
  count?: number
  generated_at_utc?: string
}

/** One sealed order decision from the live CommitLog (state/commits.json).
 *  Lifecycle: sealed -> published (memo on-chain) -> bound (fill confirmed),
 *  or failed (+ fail_reason) when the fill phase could not complete. */
export interface LiveCommitEntry {
  kind: string
  nonce: string
  payload: {
    kind?: string
    mint?: string
    symbol?: string
    usd?: number
    fraction?: number
  }
  hash: string
  sealed_at: number
  signature: string | null
  status: 'sealed' | 'published' | 'bound' | 'failed'
  memo_signature: string | null
  memo_slot: number | null
  memo_published_at: number | null
  fail_reason?: string
}

/** One money movement from the live ExecutionLedger (state/executions.json). */
export interface LiveExecutionRecord {
  kind: 'buy' | 'close'
  idempotency_key: string
  mint: string
  usd_size: number
  tokens_out: number
  price_usd: number
  signature: string
  status: string
  ts: number
  pnl_usd: number | null
}

export interface LiveExecutionsResponse {
  enabled: boolean
  reason?: string
  generated_at_utc?: string
  commits?: LiveCommitEntry[]
  records?: LiveExecutionRecord[]
  totals?: {
    commits: number
    bound: number
    failed: number
    published_unfilled: number
    buys: number
    closes: number
  }
}


export type Tab = 'dashboard' | 'holdings' | 'journal' | 'market' | 'system'

/** Journal status filter set from the command palette (§63). */
export type JournalFilter = "all" | "bound" | "published" | "failed"

/** Which slice of the decisions tape is shown (§64.3 drill-down). */
export type FeedFilter = "all" | "approved" | "refused" | "gate_refused" | "gate_passed"

/** One §64.1b alert — a symptom that genuinely demands operator attention.
 *  The strip renders NOTHING when the list is empty (that is the feature
 *  working, not wasted space). */
export interface SafetyAlert {
  id: string
  severity: "fail" | "warn"
  message: string
}

export interface BlocklistLatest {
  mint: string | null
  symbol: string
  reason: string
  kind: string
  blocked_at: string
}

/** GET /api/safety (§64.1a) — the read-only safety-state surface behind the
 *  SystemStatus safety section and the alert strip. Display only: no
 *  endpoint behind it can change any of this state. */
export interface SafetyResponse {
  generated_at_utc: string
  armed: boolean
  kill_switch: { engaged: boolean; reason: string }
  daily_loss_breaker: {
    limit_usd: number | null
    realized_pnl_today_usd: number | null
    engaged: boolean
    headroom_usd: number | null
  }
  blocklist: {
    blocks: number | null
    auto: number | null
    manual: number | null
    latest: BlocklistLatest | null
  }
  break: { on_break: boolean; reason: string; break_until_epoch: number | null }
  llm: { main_reachable: boolean; provider: string }
  crowd_chain: { configured: string[]; benched: string[]; exhausted: boolean | null; stale: boolean; observed_at: number | null }
  alerts: SafetyAlert[]
  thresholds: { no_fills_hours: number; no_fills_min_candidates: number }
}

/** One persisted funnel snapshot (§64.2) — the trend series' data points. */
export interface FunnelSnapshot {
  id: number
  ts: string
  candidates_seen: number
  gate_refused: number
  model_refused: number
  gate_passed: number
  model_approved: number
  filled: number
  model_refusal_rate: number | null
}

/** GET /api/funnel/snapshots — stored history, oldest first. */
export interface FunnelSnapshotsResponse {
  snapshots: FunnelSnapshot[]
  count: number
  /** Threshold in the backend's own words when history is too short to trend. */
  note: string
}

/** GET /api/funnel — the §57 refusal funnel (§63): candidates seen ->
 *  gate-passed -> model-approved -> filled. Server-computed with the exact
 *  scripts/perf_report.py classification; a null rate renders `—`, never 0. */
export interface FunnelResponse {
  window: { limit: number; feed_events: number }
  candidates_seen_total: number
  candidates_seen: number
  gate_refused: number
  model_refused: number
  gate_passed: number
  model_approved: number
  filled: number
  model_refusal_rate_of_gate_passers: number | null
  note: string
}

/** GET /api/mint/{mint}/history (§65) — every feed event ever recorded for
 *  one mint, newest first. Rows are the SAME shape as /api/feed (the shared
 *  backend projection), so the drill-down reuses the tape's row rendering. */
export interface MintHistoryResponse {
  mint: string
  total: number
  limit: number
  offset: number
  events: FeedEventRow[]
}
