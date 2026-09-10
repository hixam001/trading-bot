import type { RegimeRow } from '../types'
import { Empty, Panel } from './ui'
import { clock } from '../lib/format'

/** Market regime history (I5): answers "why was the bot quiet?" — scroll-
 * bounded so history never stretches the page; `tall` gives the dedicated
 * market view a taller viewport. */
export default function MarketRegimePanel({
  regimes,
  tall = false,
}: {
  regimes: RegimeRow[]
  tall?: boolean
}) {
  return (
    <Panel testId="market-regime" title="Market regime · one row per tick">
      {regimes.length === 0 ? (
        <Empty>No ticks yet. Regime rows appear once the live cycle runs.</Empty>
      ) : (
        <div
          className="space-y-1 text-xs overflow-y-auto pr-1"
          style={{ maxHeight: tall ? '70vh' : '38vh' }}
        >
          {regimes.map((r) => (
            <div key={r.computed_at} className="flex items-center gap-2">
              <span
                className={`shrink-0 w-9 font-semibold font-mono ${r.regime_ok ? 'text-pass' : 'text-fail'}`}
              >
                {r.regime_ok ? 'OK' : 'BAD'}
              </span>
              <span className="text-faint shrink-0 tnum">{clock(r.computed_at)}</span>
              <span className="text-body flex-1 truncate" title={r.regime_detail}>
                {r.regime_detail}
              </span>
              <span className="text-dim shrink-0 tnum">n={r.candidate_count}</span>
            </div>
          ))}
        </div>
      )}
    </Panel>
  )
}