// src/components/StatsDashboard.tsx — FR-18: Equity curve + metrics + learning window

import { useState } from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine,
} from "recharts";
import { useApi } from "../hooks/useApi";
import type { StatsResponse, LearningWindow } from "../types";

function MetricCard({
  label,
  value,
  sub,
  color,
}: {
  label: string;
  value: string | React.ReactNode;
  sub?: string;
  color?: string;
}) {
  return (
    <div className="card text-center">
      <div
        className="metric-value"
        style={{ color: color || "hsl(215,28%,90%)" }}
      >
        {value}
      </div>
      <div className="metric-label">{label}</div>
      {sub && <div className="text-xs text-text-muted mt-1">{sub}</div>}
    </div>
  );
}

function formatDate(ts: string | undefined): string {
  if (!ts) return "";
  return new Date(ts).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
  });
}

const CustomTooltip = ({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ value: number; payload: { timestamp: string; pct_return: number } }>;
}) => {
  if (!active || !payload?.length) return null;
  const d = payload[0];
  const pct = d.payload.pct_return;
  return (
    <div
      className="p-3 rounded-lg text-xs font-mono"
      style={{
        background: "hsl(240,12%,10%)",
        border: "1px solid hsl(240,10%,20%)",
        boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
      }}
    >
      <p className="text-text-muted mb-1">{new Date(d.payload.timestamp).toLocaleString()}</p>
      <p className="text-text-primary font-semibold">${d.value.toFixed(2)}</p>
      <p className={pct >= 0 ? "text-pass-text" : "text-loss-text"}>
        {pct >= 0 ? "+" : ""}{pct.toFixed(2)}% return
      </p>
    </div>
  );
};

export function StatsDashboard() {
  const [equityMode, setEquityMode] = useState<"usd" | "pct">("usd");
  const { data: stats, loading, error } = useApi<StatsResponse>("/api/stats", 30_000);
  const { data: lw } = useApi<LearningWindow>("/api/learning-window", 60_000);

  if (loading && !stats) {
    return (
      <div className="card flex items-center justify-center h-64">
        <span className="text-text-muted">Loading stats…</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="card flex items-center justify-center h-64">
        <p className="text-loss-text">Failed to load stats: {error}</p>
      </div>
    );
  }

  const winRate = stats?.win_rate != null
    ? `${(stats.win_rate * 100).toFixed(1)}%`
    : "N/A";
  const pf = stats?.profit_factor != null
    ? stats.profit_factor.toFixed(2)
    : "N/A";
  const dd = stats?.max_drawdown_pct != null
    ? `${stats.max_drawdown_pct.toFixed(1)}%`
    : "0.0%";
  const totalPnl = stats?.total_realized_pnl_usd ?? 0;
  const isPositive = totalPnl >= 0;

  const chartData = stats?.equity_curve ?? [];

  return (
    <div className="space-y-4">
      {/* ── Metric cards ──────────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
        <MetricCard
          label="Cash Balance"
          value={`$${(stats?.cash_balance_usd ?? 0).toFixed(2)}`}
        />
        <MetricCard
          label="Total P&L"
          value={`${isPositive ? "+" : ""}$${totalPnl.toFixed(2)}`}
          color={isPositive ? "hsl(142,71%,60%)" : "hsl(0,72%,65%)"}
        />
        <MetricCard
          label="Win Rate"
          value={winRate}
          sub={stats ? `${stats.win_count}W / ${stats.loss_count}L` : ""}
          color={
            stats?.win_rate != null && stats.win_rate >= 0.55
              ? "hsl(142,71%,60%)"
              : "hsl(215,28%,90%)"
          }
        />
        <MetricCard
          label="Profit Factor"
          value={pf}
          color={
            stats?.profit_factor != null && stats.profit_factor >= 1.5
              ? "hsl(142,71%,60%)"
              : stats?.profit_factor != null && stats.profit_factor < 1
              ? "hsl(0,72%,65%)"
              : "hsl(215,28%,90%)"
          }
        />
        <MetricCard
          label="Max Drawdown"
          value={dd}
          color={
            stats?.max_drawdown_pct != null && stats.max_drawdown_pct > 20
              ? "hsl(0,72%,65%)"
              : "hsl(215,28%,90%)"
          }
        />
        <MetricCard
          label="Total Trades"
          value={stats?.total_closed_trades ?? 0}
          sub={`${stats?.open_positions ?? 0} open`}
        />
      </div>

      {/* ── Equity curve ──────────────────────────────────────────────────── */}
      <div className="card">
        <div className="card-header">
          <span className="card-title">Equity Curve</span>
          <div className="flex gap-1">
            {(["usd", "pct"] as const).map((m) => (
              <button
                key={m}
                onClick={() => setEquityMode(m)}
                className={`btn-ghost ${equityMode === m ? "!border-border !text-text-primary" : ""}`}
              >
                {m === "usd" ? "$ USD" : "% Return"}
              </button>
            ))}
          </div>
        </div>

        {chartData.length < 2 ? (
          <div className="h-48 flex items-center justify-center">
            <p className="text-text-muted text-sm">
              Equity curve appears after 2+ closed trades.
            </p>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={200}>
            <LineChart
              data={chartData}
              margin={{ top: 4, right: 12, left: 0, bottom: 0 }}
            >
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis
                dataKey="timestamp"
                tickFormatter={formatDate}
                tick={{ fontSize: 10, fill: "hsl(215,15%,45%)" }}
                tickLine={false}
                axisLine={false}
              />
              <YAxis
                dataKey={equityMode === "usd" ? "equity_usd" : "pct_return"}
                tick={{ fontSize: 10, fill: "hsl(215,15%,45%)", fontFamily: "JetBrains Mono" }}
                tickLine={false}
                axisLine={false}
                tickFormatter={(v: number) =>
                  equityMode === "usd" ? `$${v.toFixed(0)}` : `${v.toFixed(1)}%`
                }
                width={60}
              />
              <Tooltip content={<CustomTooltip />} />
              <ReferenceLine
                y={equityMode === "usd" ? stats?.initial_cash_usd ?? 1000 : 0}
                stroke="hsl(240,10%,25%)"
                strokeDasharray="4 4"
              />
              <Line
                type="monotone"
                dataKey={equityMode === "usd" ? "equity_usd" : "pct_return"}
                stroke={isPositive ? "hsl(142,71%,50%)" : "hsl(0,72%,55%)"}
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4, fill: "hsl(142,71%,50%)" }}
              />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* ── Learning window ───────────────────────────────────────────────── */}
      {lw && (
        <div className="card">
          <div className="card-header">
            <span className="card-title">Learning Window</span>
            {lw.window_complete && (
              <span className="badge-pass text-xs">Window Complete</span>
            )}
          </div>
          <div className="grid grid-cols-2 gap-4">
            {/* Days progress */}
            <div>
              <div className="flex justify-between text-xs mb-2">
                <span className="text-text-muted">Days elapsed</span>
                <span className="font-mono text-text-secondary">
                  {lw.days_elapsed} / {lw.days_target}
                </span>
              </div>
              <div className="w-full h-2 rounded-full bg-bg-elevated overflow-hidden">
                <div
                  className="h-full rounded-full transition-all duration-700"
                  style={{
                    width: `${Math.min((lw.days_elapsed / lw.days_target) * 100, 100)}%`,
                    background:
                      lw.window_complete
                        ? "hsl(142,71%,45%)"
                        : "hsl(217,91%,55%)",
                  }}
                />
              </div>
              <p className="text-xs text-text-muted mt-1">
                Day {lw.days_elapsed.toFixed(0)} of {lw.days_target}
              </p>
            </div>

            {/* Trades progress */}
            <div>
              <div className="flex justify-between text-xs mb-2">
                <span className="text-text-muted">Trades closed</span>
                <span className="font-mono text-text-secondary">
                  {lw.trades_closed} / {lw.trades_target}
                </span>
              </div>
              <div className="w-full h-2 rounded-full bg-bg-elevated overflow-hidden">
                <div
                  className="h-full rounded-full transition-all duration-700"
                  style={{
                    width: `${Math.min((lw.trades_closed / lw.trades_target) * 100, 100)}%`,
                    background:
                      lw.trades_closed >= lw.trades_target
                        ? "hsl(142,71%,45%)"
                        : "hsl(217,91%,55%)",
                  }}
                />
              </div>
              <p className="text-xs text-text-muted mt-1">
                {lw.trades_closed} / {lw.trades_target} trades toward promotion review
              </p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
