// src/types/index.ts — shared TypeScript types matching API response shapes

export interface FeedEvent {
  id: number | null;
  ts: string;
  symbol: string;
  mint_address: string;
  candidate_snapshot: CandidateSnapshot;
  verdict: "pass" | "fail";
  confidence: number | null;
  risk_flags: string[];
  entry_condition: string | null;
  invalidation_condition: string | null;
  thesis: string | null;
  led_to_trade_id: string | null;
}

export interface CandidateSnapshot {
  symbol: string;
  mint_address: string;
  price_usd: number;
  liquidity_usd: number;
  volume_24h_usd: number;
  holder_count: number;
  top_holder_pct: number;
  age_hours: number;
  market_cap_usd: number;
  source?: string;
}

export interface Holding {
  trade_id: string;
  symbol: string;
  mint_address: string;
  opened_at: string;
  entry_price_usd: number;
  position_size_usd: number;
  quantity: number;
  invalidation_condition: string;
  thesis: string;
  current_price_usd: number | null;
  unrealized_pnl_usd: number | null;
  unrealized_pnl_pct: number | null;
}

export interface JournalEntry {
  trade_id: string;
  symbol: string;
  mint_address: string;
  opened_at: string;
  closed_at: string | null;
  entry_price_usd: number;
  exit_price_usd: number | null;
  position_size_usd: number;
  realized_pnl_usd: number | null;
  realized_pnl_pct: number | null;
  exit_reason: string | null;
  thesis: string;
  entry_condition: string;
  invalidation_condition: string;
  confidence: number | null;
  risk_flags: string[];
  reflection_text: string | null;
  candidate_snapshot: CandidateSnapshot;
}

export interface EquityPoint {
  timestamp: string;
  equity_usd: number;
  pct_return: number;
}

export interface StatsResponse {
  cash_balance_usd: number;
  total_realized_pnl_usd: number;
  open_positions: number;
  total_closed_trades: number;
  win_count: number;
  loss_count: number;
  win_rate: number | null;
  profit_factor: number | null;
  max_drawdown_pct: number;
  avg_pnl_usd: number | null;
  avg_win_usd: number | null;
  avg_loss_usd: number | null;
  equity_curve: EquityPoint[];
  initial_cash_usd: number;
}

export interface LearningWindow {
  days_elapsed: number;
  days_target: number;
  trades_closed: number;
  trades_target: number;
  window_started: boolean;
  window_complete: boolean;
}

export interface PromotionCriterion {
  name: string;
  passed: boolean;
  actual: number | null;
  required: number;
  detail: string;
}

export interface PromotionGate {
  all_criteria_met: boolean;
  criteria: PromotionCriterion[];
  summary: string;
  note: string;
}

export interface KnowledgeBase {
  static_knowledge: string;
  ingested_files: { filename: string; size_bytes: number; chars: number }[];
  dynamic_stats: {
    total_closed: number;
    win_rate_overall: number | null;
    win_rate_by_liquidity_bucket: Record<string, { trades: number; win_rate: number | null }>;
    win_rate_by_age_bucket: Record<string, { trades: number; win_rate: number | null }>;
  };
}

export interface SystemStatus {
  paper_trading_only: boolean;
  data_backend: string;
  ollama: {
    ollama_reachable: boolean;
    model_name: string;
    model_loaded: boolean;
    available_models: string[];
    ollama_url: string;
    error?: string;
  };
  config: Record<string, unknown>;
}
