import type { FunnelResponse } from '../types'
import { Empty, Panel } from './ui'

/**
 * Refusal funnel (§63) — selectivity at a glance: candidates seen -> gate
 * passed -> model approved -> filled. Bespoke divs over the server counts
 * only: no chart library (§5), no invented data; unknown stages render "—"
 * (DESIGN.md §2), never 0.
 */
export default function RefusalFunnel({ funnel }: { funnel: FunnelResponse }) {
  const stages = [
    { label: 'candidates seen', n: funnel.candidates_seen, note: 'window' },
    { label: 'gate passed', n: funnel.gate_passed, note: `${funnel.gate_refused} gate refusals` },
    { label: 'model approved', n: funnel.model_approved, note: `${funnel.model_refused} refusals` },
    { label: 'filled', n: funnel.filled, note: 'sealed commits with a fill' },
  ]
  const base = funnel.candidates_seen
  const rate = funnel.model_refusal_rate_of_gate_passers
  return (
    <Panel testId="refusal-funnel" title="Refusal funnel">
      {base === 0 && funnel.candidates_seen_total === 0 ? (
        <Empty>No candidates evaluated yet. The funnel fills as the engine cycles.</Empty>
      ) : (
        <div data-testid="funnel-stages">
          {stages.map((s, i) => (
            <div key={s.label} className="mb-2 last:mb-0">
              <div className="flex items-baseline justify-between gap-3 text-xs">
                <span className="text-dim">
                  <span className="font-mono text-[10px] text-faint mr-1.5">{i + 1}.</span>
                  {s.label}
                </span>
                <span className="font-mono tnum font-semibold text-bright">
                  {s.n}
                </span>
              </div>
              <div className="mt-1 h-1.5 w-full bg-raised rounded overflow-hidden">
                <div
                  className="h-full bg-live"
                  style={{ width: base === 0 ? '0%' : `${(s.n / base) * 100}%` }}
                />
              </div>
              <div className="mt-0.5 text-[10px] text-faint">{s.note}</div>
            </div>
          ))}
          <div className="divider" />
          <div className="flex items-baseline justify-between gap-3 text-xs">
            <span className="text-dim">model refusal · share of gate-passers</span>
            <span className={rate === null ? 'font-mono tnum text-dim' : 'font-mono tnum text-warn'}>
              {rate === null ? '—' : `${(rate * 100).toFixed(1)}%`}
            </span>
          </div>
          <div className="mt-1 text-[10px] text-faint">
            {funnel.candidates_seen_total} candidates evaluated all-time ·
            classification identical to scripts/perf_report.py
          </div>
        </div>
      )}
    </Panel>
  )
}

