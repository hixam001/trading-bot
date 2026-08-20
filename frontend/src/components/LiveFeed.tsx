// src/components/LiveFeed.tsx — FR-15: Real-time decision feed via WebSocket

import { useState } from "react";
import type { FeedEvent } from "../types";
import { useWebSocket } from "../hooks/useWebSocket";
import { useApi } from "../hooks/useApi";

function formatAge(ts: string): string {
  const diff = Date.now() - new Date(ts).getTime();
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return `${h}h ago`;
}

function fmt(n: number, decimals = 0): string {
  return n.toLocaleString("en-US", { maximumFractionDigits: decimals });
}

function FeedEventRow({ event, isNew }: { event: FeedEvent; isNew: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const isPass = event.verdict === "pass";

  return (
    <div
      className={`expandable-row p-3 rounded-lg mb-1 ${isNew ? "feed-item-enter" : ""} ${
        isPass ? "border border-pass-dim/30" : "border border-transparent"
      }`}
      onClick={() => setExpanded((e) => !e)}
    >
      {/* ── Main row ─────────────────────────────────────────────────────── */}
      <div className="flex items-center gap-3 min-w-0">
        {/* Verdict badge */}
        <span className={isPass ? "badge-pass" : "badge-fail"}>
          {isPass ? "▲ PASS" : "▼ FAIL"}
        </span>

        {/* Symbol */}
        <span className="font-mono font-semibold text-sm text-text-primary w-20 shrink-0">
          {event.symbol}
        </span>

        {/* Confidence bar (only for pass) */}
        {isPass && event.confidence != null && (
          <div className="flex items-center gap-1.5 shrink-0">
            <div className="w-16 h-1.5 rounded-full bg-bg-elevated overflow-hidden">
              <div
                className="h-full rounded-full bg-pass"
                style={{ width: `${event.confidence * 100}%` }}
              />
            </div>
            <span className="text-xs font-mono text-pass-text">
              {(event.confidence * 100).toFixed(0)}%
            </span>
          </div>
        )}

        {/* Thesis preview */}
        <p className="text-xs text-text-secondary truncate flex-1 min-w-0">
          {event.thesis || "—"}
        </p>

        {/* Timestamp */}
        <span className="text-xs font-mono text-text-muted shrink-0">
          {formatAge(event.ts)}
        </span>

        {/* Expand chevron */}
        <span className="text-text-muted text-xs ml-1">
          {expanded ? "▴" : "▾"}
        </span>
      </div>

      {/* ── Expanded detail ───────────────────────────────────────────────── */}
      {expanded && (
        <div className="mt-3 pt-3 border-t border-border-subtle animate-fade-in grid grid-cols-1 md:grid-cols-2 gap-4">
          {/* Candidate stats */}
          <div>
            <p className="text-xs font-semibold text-text-muted uppercase tracking-wider mb-2">
              Token Stats
            </p>
            <div className="space-y-1">
              {[
                ["Price", `$${event.candidate_snapshot.price_usd?.toFixed(8)}`],
                ["Liquidity", `$${fmt(event.candidate_snapshot.liquidity_usd)}`],
                ["Volume 24h", `$${fmt(event.candidate_snapshot.volume_24h_usd)}`],
                ["Holders", fmt(event.candidate_snapshot.holder_count)],
                ["Top holder", `${event.candidate_snapshot.top_holder_pct?.toFixed(1)}%`],
                ["Age", `${event.candidate_snapshot.age_hours?.toFixed(1)}h`],
                ["Market cap", `$${fmt(event.candidate_snapshot.market_cap_usd)}`],
              ].map(([k, v]) => (
                <div key={k} className="data-row">
                  <span className="data-key">{k}</span>
                  <span className="data-val">{v}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Verdict detail */}
          <div>
            <p className="text-xs font-semibold text-text-muted uppercase tracking-wider mb-2">
              Verdict Detail
            </p>
            {event.risk_flags.length > 0 && (
              <div className="mb-2 flex flex-wrap gap-1">
                {event.risk_flags.map((f) => (
                  <span
                    key={f}
                    className="px-1.5 py-0.5 rounded text-xs font-mono"
                    style={{
                      background: "hsl(0,40%,12%)",
                      color: "hsl(0,72%,65%)",
                      border: "1px solid hsl(0,40%,20%)",
                    }}
                  >
                    {f}
                  </span>
                ))}
              </div>
            )}
            {event.thesis && (
              <p className="text-xs text-text-secondary mb-2">{event.thesis}</p>
            )}
            {event.entry_condition && (
              <div className="data-row">
                <span className="data-key">Entry condition</span>
                <span className="text-xs text-text-secondary">{event.entry_condition}</span>
              </div>
            )}
            {event.invalidation_condition && (
              <div className="data-row">
                <span className="data-key">Invalidation</span>
                <span className="text-xs text-text-secondary">{event.invalidation_condition}</span>
              </div>
            )}
            {event.led_to_trade_id && (
              <p className="text-xs font-mono text-pass-text mt-2">
                → Trade opened: {event.led_to_trade_id.slice(0, 8)}…
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

interface FeedPageResponse {
  events: FeedEvent[];
}

export function LiveFeed() {
  const { events: wsEvents, connected } = useWebSocket();
  // Also load historical events on mount so the feed isn't empty
  const { data: historical } = useApi<FeedPageResponse>("/api/feed?limit=50", undefined);
  const [newIds] = useState<Set<number | null>>(new Set());

  // Merge: ws events (newest) on top, historical below, deduplicated by id
  const allEvents = (() => {
    const seen = new Set<number | null>();
    const merged: FeedEvent[] = [];
    for (const e of [...wsEvents, ...(historical?.events ?? [])]) {
      if (!seen.has(e.id)) {
        seen.add(e.id);
        merged.push(e);
      }
    }
    return merged;
  })();

  return (
    <div className="card h-full flex flex-col">
      <div className="card-header">
        <div className="flex items-center gap-2">
          <span className="card-title">Live Feed</span>
          <span
            className={`status-dot ${connected ? "online" : "offline"}`}
            title={connected ? "WebSocket connected" : "WebSocket disconnected"}
          />
        </div>
        <span className="text-xs font-mono text-text-muted">
          {allEvents.length} events
        </span>
      </div>

      {allEvents.length === 0 ? (
        <div className="flex-1 flex items-center justify-center">
          <p className="text-text-muted text-sm">
            {connected
              ? "Waiting for the tick loop to produce events…"
              : "Connecting to live feed…"}
          </p>
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto -mr-1 pr-1">
          {allEvents.map((event) => (
            <FeedEventRow
              key={event.id ?? event.ts + event.symbol}
              event={event}
              isNew={wsEvents.some((e) => e.id === event.id && e.id !== null)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
