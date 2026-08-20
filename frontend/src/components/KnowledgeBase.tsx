// src/components/KnowledgeBase.tsx — FR-20: Static KB + dynamic stats display

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useApi } from "../hooks/useApi";
import type { KnowledgeBase } from "../types";

function WinRateTable({
  title,
  data,
}: {
  title: string;
  data: Record<string, { trades: number; win_rate: number | null }>;
}) {
  return (
    <div className="mb-4">
      <p className="text-xs font-semibold text-text-muted uppercase tracking-wider mb-2">
        {title}
      </p>
      <table className="w-full text-xs">
        <thead>
          <tr
            style={{ background: "hsl(240,10%,12%)" }}
            className="border-b border-border"
          >
            <th className="py-1.5 px-3 text-left data-key">Bucket</th>
            <th className="py-1.5 px-3 text-right data-key">Trades</th>
            <th className="py-1.5 px-3 text-right data-key">Win Rate</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(data).map(([bucket, v]) => (
            <tr
              key={bucket}
              className="border-b border-border-subtle hover:bg-bg-elevated/30"
            >
              <td className="py-1.5 px-3 font-mono text-text-secondary">
                {bucket}
              </td>
              <td className="py-1.5 px-3 text-right font-mono text-text-muted">
                {v.trades}
              </td>
              <td className="py-1.5 px-3 text-right font-mono">
                {v.win_rate != null ? (
                  <span
                    className={
                      v.win_rate >= 0.55
                        ? "text-pass-text"
                        : v.win_rate < 0.4
                        ? "text-loss-text"
                        : "text-text-secondary"
                    }
                  >
                    {(v.win_rate * 100).toFixed(1)}%
                  </span>
                ) : (
                  <span className="text-text-muted">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function KnowledgeBasePanel() {
  const [tab, setTab] = useState<"static" | "ingested" | "stats">("static");
  const { data, loading, error } = useApi<KnowledgeBase>("/api/knowledge-base", 60_000);

  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Knowledge Base</span>
        <div className="flex gap-1">
          {(
            [
              ["static", "Curated"],
              ["ingested", "Ingested"],
              ["stats", "Trade Stats"],
            ] as const
          ).map(([t, label]) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`tab-btn ${tab === t ? "active" : ""}`}
            >
              {label}
              {t === "ingested" && data?.ingested_files.length
                ? ` (${data.ingested_files.length})`
                : ""}
            </button>
          ))}
        </div>
      </div>

      {loading && !data && (
        <div className="h-48 flex items-center justify-center">
          <span className="text-text-muted text-sm">Loading knowledge base…</span>
        </div>
      )}
      {error && (
        <p className="text-loss-text text-sm">Failed to load: {error}</p>
      )}

      {data && (
        <>
          {/* ── Static knowledge ─────────────────────────────────────────── */}
          {tab === "static" && (
            <div
              className="overflow-y-auto max-h-[600px] pr-1 prose-kb"
              style={{ maxHeight: "60vh" }}
            >
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {data.static_knowledge || "_No static knowledge loaded._"}
              </ReactMarkdown>
            </div>
          )}

          {/* ── Ingested files ───────────────────────────────────────────── */}
          {tab === "ingested" && (
            <div>
              {data.ingested_files.length === 0 ? (
                <div className="h-32 flex flex-col items-center justify-center gap-2">
                  <p className="text-text-muted text-sm">
                    No ingested files yet.
                  </p>
                  <p className="text-text-muted text-xs">
                    Use{" "}
                    <code className="font-mono text-accent-text bg-bg-elevated px-1 py-0.5 rounded">
                      POST /api/ingest
                    </code>{" "}
                    to add external material.
                  </p>
                </div>
              ) : (
                <div className="space-y-2">
                  {data.ingested_files.map((f) => (
                    <div
                      key={f.filename}
                      className="flex items-center justify-between p-2 rounded-lg"
                      style={{
                        background: "hsl(240,10%,8%)",
                        border: "1px solid hsl(240,8%,14%)",
                      }}
                    >
                      <span className="text-sm font-mono text-text-secondary">
                        {f.filename}
                      </span>
                      <span className="text-xs font-mono text-text-muted">
                        {f.chars.toLocaleString()} chars
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* ── Dynamic stats ─────────────────────────────────────────────── */}
          {tab === "stats" && (
            <div>
              {data.dynamic_stats.total_closed === 0 ? (
                <div className="h-32 flex items-center justify-center">
                  <p className="text-text-muted text-sm">
                    Dynamic stats appear after trades close.
                  </p>
                </div>
              ) : (
                <>
                  <div className="mb-4 p-3 rounded-lg" style={{ background: "hsl(240,10%,8%)", border: "1px solid hsl(240,8%,14%)" }}>
                    <div className="flex items-center justify-between">
                      <span className="text-xs text-text-muted">Overall win rate</span>
                      <span className="font-mono text-sm text-text-primary">
                        {data.dynamic_stats.win_rate_overall != null
                          ? `${(data.dynamic_stats.win_rate_overall * 100).toFixed(1)}%`
                          : "N/A"}
                        {" "}
                        <span className="text-text-muted text-xs">
                          ({data.dynamic_stats.total_closed} trades)
                        </span>
                      </span>
                    </div>
                  </div>
                  <WinRateTable
                    title="Win Rate by Liquidity Bucket"
                    data={data.dynamic_stats.win_rate_by_liquidity_bucket}
                  />
                  <WinRateTable
                    title="Win Rate by Token Age"
                    data={data.dynamic_stats.win_rate_by_age_bucket}
                  />
                </>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
