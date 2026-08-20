// src/components/TradeJournal.tsx — FR-17: Full trade history with thesis + reflection

import { useState } from "react";
import { useApi } from "../hooks/useApi";
import type { JournalEntry } from "../types";

interface JournalResponse {
  trades: JournalEntry[];
  count: number;
}

function fmt(n: number): string {
  return n.toLocaleString("en-US", { maximumFractionDigits: 0 });
}

function ExitReasonBadge({ reason }: { reason: string | null }) {
  if (!reason) return null;
  const styles: Record<string, string> = {
    take_profit: "bg-pass-dim text-pass-text border-pass-dim",
    stop_loss: "bg-loss-dim text-loss-text border-loss-dim",
    timeout: "bg-accent-dim text-accent-text border-accent-dim",
    manual: "bg-bg-elevated text-text-secondary border-border",
  };
  const cls =
    styles[reason] ?? "bg-bg-elevated text-text-secondary border-border";
  return (
    <span
      className={`inline-flex px-2 py-0.5 rounded text-xs font-mono font-semibold border ${cls}`}
    >
      {reason.replace("_", " ")}
    </span>
  );
}

function JournalRow({ entry }: { entry: JournalEntry }) {
  const [expanded, setExpanded] = useState(false);
  const pnl = entry.realized_pnl_usd ?? 0;
  const pct = entry.realized_pnl_pct ?? 0;
  const isWin = pnl > 0;

  const holdDuration = (() => {
    if (!entry.closed_at) return "—";
    const diffMs =
      new Date(entry.closed_at).getTime() -
      new Date(entry.opened_at).getTime();
    const h = Math.floor(diffMs / 3_600_000);
    const m = Math.floor((diffMs % 3_600_000) / 60_000);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  })();

  return (
    <>
      <tr
        className="cursor-pointer hover:bg-bg-elevated transition-colors duration-150 border-b border-border-subtle"
        onClick={() => setExpanded((e) => !e)}
      >
        <td className="py-3 px-4 font-mono text-text-primary font-semibold">
          {entry.symbol}
        </td>
        <td className="py-3 px-4 text-xs font-mono text-text-muted">
          {entry.closed_at
            ? new Date(entry.closed_at).toLocaleString()
            : "—"}
        </td>
        <td className="py-3 px-4">
          <ExitReasonBadge reason={entry.exit_reason} />
        </td>
        <td className={`py-3 px-4 text-right font-mono font-semibold text-sm ${isWin ? "text-pass-text" : "text-loss-text"}`}>
          {pnl >= 0 ? "+" : ""}${pnl.toFixed(4)}
        </td>
        <td className={`py-3 px-4 text-right font-mono text-sm ${isWin ? "text-pass-text" : "text-loss-text"}`}>
          {pct >= 0 ? "+" : ""}{pct.toFixed(1)}%
        </td>
        <td className="py-3 px-4 text-xs font-mono text-text-muted text-right">
          {holdDuration}
        </td>
        <td className="py-3 px-4 text-text-muted text-xs text-center">
          {expanded ? "▴" : "▾"}
        </td>
      </tr>

      {expanded && (
        <tr className="bg-bg-elevated/50">
          <td colSpan={7} className="px-4 pb-4 pt-2">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 animate-fade-in">
              {/* Original thesis */}
              <div>
                <p className="text-xs font-semibold text-text-muted uppercase tracking-wider mb-2">
                  Original Thesis
                </p>
                <div
                  className="p-3 rounded-lg text-xs text-text-secondary"
                  style={{ background: "hsl(240,10%,8%)", border: "1px solid hsl(240,8%,14%)" }}
                >
                  {entry.thesis || "No thesis recorded."}
                </div>
                {entry.invalidation_condition && (
                  <p className="text-xs text-warning/70 mt-2">
                    <span className="font-semibold">Invalidation: </span>
                    {entry.invalidation_condition}
                  </p>
                )}
                {entry.entry_condition && (
                  <p className="text-xs text-text-muted mt-1">
                    <span className="font-semibold">Entry condition: </span>
                    {entry.entry_condition}
                  </p>
                )}
              </div>

              {/* Outcome + reflection */}
              <div>
                <p className="text-xs font-semibold text-text-muted uppercase tracking-wider mb-2">
                  Outcome
                </p>
                <div className="space-y-1 text-xs mb-3">
                  {[
                    ["Entry price", `$${entry.entry_price_usd.toFixed(8)}`],
                    ["Exit price", entry.exit_price_usd != null ? `$${entry.exit_price_usd.toFixed(8)}` : "—"],
                    ["Size", `$${entry.position_size_usd.toFixed(2)}`],
                    ["Liquidity", `$${fmt(entry.candidate_snapshot.liquidity_usd)}`],
                    ["Holders", fmt(entry.candidate_snapshot.holder_count)],
                    ["Confidence", entry.confidence != null ? `${(entry.confidence * 100).toFixed(0)}%` : "—"],
                  ].map(([k, v]) => (
                    <div key={k} className="data-row">
                      <span className="data-key">{k}</span>
                      <span className="data-val">{v}</span>
                    </div>
                  ))}
                </div>

                {/* LLM reflection (FR-26) */}
                {entry.reflection_text ? (
                  <div>
                    <p className="text-xs font-semibold text-text-muted uppercase tracking-wider mb-1">
                      Bot Reflection
                    </p>
                    <div
                      className="p-3 rounded-lg text-xs text-accent-text italic"
                      style={{
                        background: "hsl(217,40%,10%)",
                        border: "1px solid hsl(217,40%,18%)",
                        borderLeft: "3px solid hsl(217,91%,55%)",
                      }}
                    >
                      "{entry.reflection_text}"
                    </div>
                  </div>
                ) : (
                  <p className="text-xs text-text-muted italic">
                    Reflection pending…
                  </p>
                )}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

export function TradeJournal() {
  const [sort, setSort] = useState<"date" | "pnl">("date");
  const { data, loading, error } = useApi<JournalResponse>(
    `/api/journal?limit=100&sort=${sort}`,
    30_000
  );

  return (
    <div className="card h-full flex flex-col">
      <div className="card-header">
        <span className="card-title">Trade Journal</span>
        <div className="flex items-center gap-2">
          <span className="text-xs text-text-muted mr-2">Sort:</span>
          {(["date", "pnl"] as const).map((s) => (
            <button
              key={s}
              onClick={() => setSort(s)}
              className={`btn-ghost ${sort === s ? "!text-text-primary !border-border" : ""}`}
            >
              {s === "date" ? "By Date" : "By P&L"}
            </button>
          ))}
        </div>
      </div>

      {loading && !data && (
        <div className="flex-1 flex items-center justify-center">
          <span className="text-text-muted text-sm">Loading journal…</span>
        </div>
      )}
      {error && (
        <div className="flex-1 flex items-center justify-center">
          <p className="text-loss-text text-sm">Failed to load journal: {error}</p>
        </div>
      )}

      {data && data.trades.length === 0 && (
        <div className="flex-1 flex items-center justify-center">
          <p className="text-text-muted text-sm">No closed trades yet.</p>
        </div>
      )}

      {data && data.trades.length > 0 && (
        <div className="flex-1 overflow-auto">
          <table className="w-full text-sm">
            <thead className="sticky top-0" style={{ background: "hsl(240,12%,9%)" }}>
              <tr className="border-b border-border">
                <th className="py-2 px-4 text-left data-key">Symbol</th>
                <th className="py-2 px-4 text-left data-key">Closed</th>
                <th className="py-2 px-4 text-left data-key">Exit</th>
                <th className="py-2 px-4 text-right data-key">P&L $</th>
                <th className="py-2 px-4 text-right data-key">P&L %</th>
                <th className="py-2 px-4 text-right data-key">Held</th>
                <th className="py-2 px-4 text-center data-key"></th>
              </tr>
            </thead>
            <tbody>
              {data.trades.map((t) => (
                <JournalRow key={t.trade_id} entry={t} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
