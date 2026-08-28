// Shared API response types (mirror of the backend models).

export interface RuleResultRow {
  rule_id: string
  passed: boolean
  detail: string
  value: number | string | boolean | null
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

export interface HoldingRow {
  trade_id: string
  symbol: string
  mint_address: string
  opened_at: string
  entry_price_usd: number
  position_size_usd: number
  quantity: number
  thesis: string
  current_price_usd: number | null
  unrealized_pnl_usd: number | null
  unrealized_pnl_pct: number | null
}

export interface TradeRow {
  trade_id: string
  symbol: string
  mint_address: string
  opened_at: string
  entry_price_usd: number
  position_size_usd: number
  quantity: number
  thesis: string
  closed_at: string | null
  exit_price_usd: number | null
  exit_reason: string | null
  realized_pnl_usd: number | null
  realized_pnl_pct: number | null
  is_open: boolean
  reflection_text: string | null
}

export interface StatsResponse {
  initial_cash_usd: number
  cash_usd: number
  equity_usd: number
  open_positions: number
  closed_trades: number
  win_rate: number | null
  profit_factor: number | null
  max_drawdown_pct: number
  total_pnl_usd: number
  realized_pnl_usd: number
  unrealized_pnl_usd: number | null
  total_spend_usd: number
  equity_curve: { closed_at: string; equity_usd: number }[]
  paper_trading_only: boolean
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

export interface Criterion {
  name: string
  passed: boolean
  actual: number | null
  required: number
  detail: string
}

export interface PromotionGateResponse {
  all_criteria_met: boolean
  criteria: Criterion[]
  summary: string
  note: string
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
  ollama_reachable: boolean
  model: string
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

export interface KnowledgeBaseResponse {
  static_knowledge: string
  ingested: { filename: string; digest: string; ingested_at: string }[]
  dynamic_stats: {
    by_liquidity_bucket: Record<string, { wins: number; trades: number; win_rate: number | null }>
    by_age_bucket: Record<string, { wins: number; trades: number; win_rate: number | null }>
  }
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
