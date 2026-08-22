import type { PromotionGateResponse } from '../types'

/** Status display, never a control (I6). */
export default function PromotionGate({ data }: { data: PromotionGateResponse }) {
  return (
    <div className="panel">
      <div className="panel-title">promotion gate — status display only</div>
      <div className="space-y-1 text-xs">
        {data.criteria.map((c) => (
          <div key={c.name} className="flex items-center gap-2">
            <span className={c.passed ? 'text-term-green' : 'text-term-red'}>
              {c.passed ? '✓' : '✗'}
            </span>
            <span className="w-44">{c.name}</span>
            <span className="text-term-dim">{c.detail}</span>
          </div>
        ))}
      </div>
      <div className="text-xs text-term-amber mt-2">{data.summary}</div>
      <div className="text-xs text-term-dim mt-1">{data.note}</div>
    </div>
  )
}
