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
