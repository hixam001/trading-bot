import type { RegimeRow } from '../types'

/** Market regime history (I5): answers "why was the bot quiet?" */
export default function MarketRegimePanel({ regimes }: { regimes: RegimeRow[] }) {
  return (
    <div className="panel overflow-auto" style={{ maxHeight: '40vh' }}>
      <div className="panel-title">market regime (one row per tick)</div>
      {regimes.length === 0 && <div className="text-term-dim text-xs">No ticks yet.</div>}
      <div className="space-y-1 text-xs">
        {regimes.map((r) => (
          <div key={r.computed_at} className="flex items-center gap-2">
            <span className={r.regime_ok ? 'text-term-green' : 'text-term-red'}>
              {r.regime_ok ? 'OK ' : 'BAD'}
            </span>
            <span className="text-term-dim">
              {new Date(r.computed_at).toLocaleTimeString()}
            </span>
            <span>{r.regime_detail}</span>
            <span className="text-term-dim ml-auto">n={r.candidate_count}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
