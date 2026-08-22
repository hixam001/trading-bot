import { useState } from 'react'
import type { FeedEventRow, RuleResultRow } from '../types'

function RuleLine({ r }: { r: RuleResultRow }) {
  return (
    <div className="flex gap-2 text-xs">
      <span className={r.passed ? 'text-term-green' : 'text-term-red'}>
        {r.passed ? 'PASS' : 'FAIL'}
      </span>
      <span className="w-44 shrink-0 text-term-blue">{r.rule_id}</span>
      <span className="text-term-dim">{r.detail}</span>
    </div>
  )
}

export default function LiveFeed({
  events,
  connected,
}: {
  events: FeedEventRow[]
  connected: boolean
}) {
  const [expanded, setExpanded] = useState<number | null>(null)

  return (
    <div className="panel flex-1 min-w-0 overflow-auto" style={{ maxHeight: '75vh' }}>
      <div className="panel-title flex justify-between">
        <span>live feed</span>
        <span className={connected ? 'text-term-green' : 'text-term-red'}>
          {connected ? '● ws live' : '● ws offline (polling only)'}
        </span>
      </div>
      {events.length === 0 && (
        <div className="text-term-dim text-xs">Waiting for tick events…</div>
      )}
      <div className="space-y-1">
        {events.map((ev) => (
          <div key={ev.id} className="border-b border-term-border/50 pb-1">
            <button
              className="w-full text-left flex items-center gap-2 hover:bg-term-bg px-1 rounded"
              onClick={() => setExpanded(expanded === ev.id ? null : ev.id)}
            >
              <span
                className={
                  ev.verdict === 'pass'
                    ? 'text-term-green font-bold'
                    : 'text-term-red font-bold'
                }
              >
                {ev.verdict === 'pass' ? 'ENTER' : 'REJECT'}
              </span>
              <span className="font-bold w-20">{ev.symbol}</span>
              <span className="text-term-dim text-xs truncate flex-1">{ev.thesis}</span>
              {ev.grounding_flags.length > 0 && (
                <span
                  className="text-term-amber text-xs"
                  title={ev.grounding_flags.join('; ')}
                >
                  ⚑ {ev.grounding_flags.length} grounding flag(s)
                </span>
              )}
              <span className="text-term-dim text-xs">
                {new Date(ev.ts).toLocaleTimeString()}
              </span>
            </button>
            {expanded === ev.id && (
              <div className="pl-4 pt-1 space-y-0.5 bg-term-bg/40 p-2 rounded mt-1">
                {ev.rule_breakdown.map((r) => (
                  <RuleLine key={r.rule_id} r={r} />
                ))}
                <div className="text-xs text-term-dim pt-1">
                  narration source: {ev.narration_source || 'n/a'} · regime:{' '}
                  {ev.regime_ok ? 'OK' : 'BAD'}
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
