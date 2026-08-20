// src/components/PaperTradingBanner.tsx
// FR-22: Persistent "PAPER TRADING — NO REAL FUNDS" indicator.
// This is a safety requirement, not optional styling.
// It must remain always visible and cannot be dismissed.

export function PaperTradingBanner() {
  return (
    <div
      className="fixed top-0 left-0 right-0 z-50 flex items-center justify-center gap-3 px-4 py-2"
      style={{
        background:
          "linear-gradient(90deg, hsl(38,80%,12%) 0%, hsl(38,60%,10%) 50%, hsl(38,80%,12%) 100%)",
        borderBottom: "1px solid hsl(38,95%,30%)",
        boxShadow: "0 1px 0 0 hsl(38,60%,20%) inset, 0 2px 12px rgba(246,166,35,0.15)",
      }}
    >
      <span className="text-warning text-base" aria-hidden="true">⚠</span>
      <span className="text-xs font-semibold tracking-widest uppercase text-warning-text">
        Paper Trading — No Real Funds
      </span>
      <span className="text-warning text-base" aria-hidden="true">⚠</span>
      <span className="text-xs text-warning/60 font-mono hidden sm:inline">
        · All positions are simulated · Zero real transactions occur ·
      </span>
    </div>
  );
}
