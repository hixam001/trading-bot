// src/components/PromotionGate.tsx — FR-19: Read-only promotion criteria checklist
// This is a STATUS DISPLAY. There are no buttons here. Nothing can be triggered.

import { useApi } from "../hooks/useApi";
import type { PromotionGate } from "../types";

function CriterionRow({ c }: { c: PromotionGate["criteria"][0] }) {
  return (
    <div
      className={`flex items-start gap-3 p-3 rounded-lg border transition-all duration-200 ${
        c.passed
          ? "border-pass-dim/40 bg-pass-dim/20"
          : "border-border bg-bg-elevated/30"
      }`}
    >
      {/* Status icon */}
      <div
        className="mt-0.5 w-5 h-5 rounded-full flex items-center justify-center shrink-0 text-xs font-bold"
        style={
          c.passed
            ? {
                background: "hsl(142,40%,20%)",
                color: "hsl(142,71%,60%)",
                border: "1px solid hsl(142,40%,30%)",
              }
            : {
                background: "hsl(215,10%,15%)",
                color: "hsl(215,15%,45%)",
                border: "1px solid hsl(215,10%,22%)",
              }
        }
      >
        {c.passed ? "✓" : "○"}
      </div>

      {/* Content */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center justify-between gap-2">
          <p
            className={`text-sm font-medium ${
              c.passed ? "text-pass-text" : "text-text-secondary"
            }`}
          >
            {c.name}
          </p>
          {c.actual !== null && (
            <span className="font-mono text-xs text-text-muted shrink-0">
              {typeof c.actual === "number" && c.actual < 1 && c.actual > 0
                ? `${(c.actual * 100).toFixed(1)}%`
                : c.actual}
              {" "}/{" "}
              {typeof c.required === "number" && c.required < 1 && c.required > 0
                ? `${(c.required * 100).toFixed(0)}%`
                : c.required}
            </span>
          )}
        </div>
        <p className="text-xs text-text-muted mt-0.5">{c.detail}</p>
      </div>
    </div>
  );
}

export function PromotionGatePanel() {
  const { data, loading, error } = useApi<PromotionGate>("/api/promotion-gate", 60_000);

  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Promotion Gate</span>
        {data && (
          <span
            className={`px-2 py-1 rounded text-xs font-semibold ${
              data.all_criteria_met ? "badge-pass" : "badge-fail"
            }`}
          >
            {data.all_criteria_met ? "All criteria met" : "Not eligible"}
          </span>
        )}
      </div>

      {loading && !data && (
        <div className="h-32 flex items-center justify-center">
          <span className="text-text-muted text-sm">Checking criteria…</span>
        </div>
      )}

      {error && (
        <p className="text-loss-text text-sm">Failed to load: {error}</p>
      )}

      {data && (
        <>
          <div className="space-y-2 mb-4">
            {data.criteria.map((c) => (
              <CriterionRow key={c.name} c={c} />
            ))}
          </div>

          {/* Summary */}
          <div
            className="p-3 rounded-lg text-xs"
            style={{
              background: data.all_criteria_met
                ? "hsl(142,40%,9%)"
                : "hsl(240,10%,8%)",
              border: data.all_criteria_met
                ? "1px solid hsl(142,40%,18%)"
                : "1px solid hsl(240,8%,14%)",
            }}
          >
            <p
              className={`font-medium mb-1 ${
                data.all_criteria_met ? "text-pass-text" : "text-text-secondary"
              }`}
            >
              {data.summary}
            </p>
          </div>

          {/* Note — always displayed, makes non-auto-activation explicit */}
          <div
            className="mt-3 p-3 rounded-lg text-xs text-warning-text"
            style={{
              background: "hsl(38,40%,8%)",
              border: "1px solid hsl(38,40%,16%)",
            }}
          >
            <span className="font-semibold">Note: </span>
            {data.note}
          </div>
        </>
      )}
    </div>
  );
}
