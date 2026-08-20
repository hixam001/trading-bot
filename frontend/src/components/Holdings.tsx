// src/components/Holdings.tsx — FR-16: Open positions with live unrealized P&L

import { useApi } from "../hooks/useApi";
import type { Holding } from "../types";

interface HoldingsResponse {
  holdings: Holding[];
  open_count: number;
  cash_balance_usd: number;
}

function holdDuration(openedAt: string): string {
  const diff = Date.now() - new Date(openedAt).getTime();
  const h = Math.floor(diff / 3_600_000);
  const m = Math.floor((diff % 3_600_000) / 60_000);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function PnlDisplay({ pnl, pct }: { pnl: number | null; pct: number | null }) {
  if (pnl === null || pct === null)
    return <span className="text-text-muted font-mono text-xs">—</span>;
  const cls = pnl >= 0 ? "pnl-positive" : "pnl-negative";
  return (
    <div className={`${cls} text-right`}>
      <div className="text-sm">{pnl >= 0 ? "+" : ""}${pnl.toFixed(4)}</div>
      <div className="text-xs opacity-80">
        {pct >= 0 ? "+" : ""}{pct.toFixed(1)}%
      </div>
    </div>
  );
}

function HoldingCard({ h }: { h: Holding }) {
  const isProfit = (h.unrealized_pnl_usd ?? 0) >= 0;

  return (
    <div
      className={`card mb-3 ${
        h.unrealized_pnl_usd !== null
          ? isProfit
            ? "border-glow-pass"
            : "border-glow-loss"
          : ""
      }`}
    >
      {/* Header row */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-3">
          <div
            className="w-8 h-8 rounded-lg flex items-center justify-center font-mono font-bold text-xs"
            style={{
              background: "hsl(240,10%,15%)",
              border: "1px solid hsl(240,10%,22%)",
            }}
          >
            {h.symbol.slice(0, 2)}
          </div>
          <div>
            <p className="font-mono font-semibold text-text-primary">{h.symbol}</p>
            <p className="text-xs text-text-muted font-mono">
              {h.mint_address.slice(0, 8)}…
            </p>
          </div>
        </div>
        <PnlDisplay pnl={h.unrealized_pnl_usd} pct={h.unrealized_pnl_pct} />
      </div>

      {/* Stats grid */}
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs mb-3">
        {[
          ["Entry", `$${h.entry_price_usd.toFixed(8)}`],
          ["Current", h.current_price_usd != null ? `$${h.current_price_usd.toFixed(8)}` : "—"],
          ["Size", `$${h.position_size_usd.toFixed(2)}`],
          ["Held", holdDuration(h.opened_at)],
        ].map(([k, v]) => (
          <div key={k} className="flex justify-between">
            <span className="text-text-muted">{k}</span>
            <span className="font-mono text-text-secondary">{v}</span>
          </div>
        ))}
      </div>

      {/* Thesis */}
      {h.thesis && (
        <div
          className="p-2 rounded text-xs text-text-secondary"
          style={{ background: "hsl(240,10%,8%)", border: "1px solid hsl(240,8%,14%)" }}
        >
          <span className="text-text-muted uppercase text-xs tracking-wider">Thesis: </span>
          {h.thesis}
        </div>
      )}

      {/* Invalidation condition */}
      {h.invalidation_condition && (
        <p className="text-xs text-warning/70 mt-2">
          <span className="font-semibold">Invalidation: </span>
          {h.invalidation_condition}
        </p>
      )}
    </div>
  );
}

export function Holdings() {
  // Poll every 15s for updated prices (FR-16: doesn't need WebSocket)
  const { data, loading, error } = useApi<HoldingsResponse>("/api/holdings", 15_000);

  return (
    <div className="card h-full flex flex-col">
      <div className="card-header">
        <span className="card-title">Holdings</span>
        <div className="flex items-center gap-3">
          {data && (
            <span className="text-xs font-mono text-text-muted">
              Cash: <span className="text-text-secondary">${data.cash_balance_usd.toFixed(2)}</span>
            </span>
          )}
          <span className="text-xs font-mono text-text-muted">
            {data?.open_count ?? 0} open
          </span>
        </div>
      </div>

      {loading && !data && (
        <div className="flex-1 flex items-center justify-center">
          <div className="text-text-muted text-sm">Loading holdings…</div>
        </div>
      )}

      {error && (
        <div className="flex-1 flex items-center justify-center">
          <p className="text-loss-text text-sm">Failed to load holdings: {error}</p>
        </div>
      )}

      {data && data.holdings.length === 0 && (
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center">
            <p className="text-text-muted text-sm">No open positions</p>
            <p className="text-text-muted text-xs mt-1">
              Cash available: ${data.cash_balance_usd.toFixed(2)}
            </p>
          </div>
        </div>
      )}

      {data && data.holdings.length > 0 && (
        <div className="flex-1 overflow-y-auto">
          {data.holdings.map((h) => (
            <HoldingCard key={h.trade_id} h={h} />
          ))}
        </div>
      )}
    </div>
  );
}
