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
  const [copiedMint, setCopiedMint] = useState<string | null>(null)

  async function copyMint(mint: string) {
    try {
      await navigator.clipboard.writeText(mint)
      setCopiedMint(mint)
      setTimeout(() => setCopiedMint((m) => (m === mint ? null : m)), 1500)
    } catch {
      /* clipboard unavailable — address is still fully visible/selectable */
    }
  }

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
        {events.map((ev) => {
          // All rules passed but no entry -> the model itself declined.
          const modelDeclined =
            ev.verdict !== 'pass' && ev.failed_rule_ids.length === 0
          return (
            <div key={ev.id} className="border-b border-term-border/50 pb-1">
              <button
                className="w-full text-left flex items-start gap-2 hover:bg-term-bg px-1 rounded"
                onClick={() => setExpanded(expanded === ev.id ? null : ev.id)}
              >
                <span
                  className={
                    ev.verdict === 'pass'
                      ? 'text-term-green font-bold shrink-0'
                      : 'text-term-red font-bold shrink-0'
                  }
                >
                  {ev.verdict === 'pass' ? 'ENTER' : 'PASS'}
                </span>
                <span className="font-bold w-20 shrink-0">{ev.symbol}</span>
                <span className="text-term-dim text-xs whitespace-pre-wrap flex-1">
                  {ev.thesis}
                </span>
                {ev.grounding_flags.length > 0 && (
                  <span
                    className="text-term-amber text-xs shrink-0"
                    title={ev.grounding_flags.join('; ')}
                  >
                    ⚑ {ev.grounding_flags.length} grounding flag(s)
                  </span>
                )}
                <span className="text-term-dim text-xs shrink-0">
                  {new Date(ev.ts).toLocaleTimeString()}
                </span>
              </button>
              {expanded === ev.id && (
                <div className="pl-4 pt-1 space-y-2 bg-term-bg/40 p-2 rounded mt-1">
                  {/* Token contract address — click to copy */}
                  <div className="flex items-center gap-2 text-xs flex-wrap">
                    <span className="text-term-dim shrink-0">contract:</span>
                    <button
                      className="font-mono text-term-blue hover:text-term-text break-all text-left"
                      onClick={() => copyMint(ev.mint_address)}
                      title="click to copy contract address"
                    >
                      {ev.mint_address || 'unknown'}
                    </button>
                    {copiedMint === ev.mint_address && (
                      <span className="text-term-green">copied ✓</span>
                    )}
                  </div>

                  {/* Complete model answer, verbatim */}
                  <div>
                    <div className="text-xs text-term-dim mb-0.5">
                      {modelDeclined
                        ? 'model chose not to enter:'
                        : 'model answer:'}
                    </div>
                    <div
                      className={`text-xs whitespace-pre-wrap ${
                        modelDeclined ? 'text-term-amber' : 'text-term-text'
                      }`}
                    >
                      {ev.thesis}
                    </div>
                  </div>

                  {/* Rule-by-rule pass/fail breakdown */}
                  <div className="space-y-0.5">
                    {ev.rule_breakdown.map((r) => (
                      <RuleLine key={r.rule_id} r={r} />
                    ))}
                  </div>

                  <div className="text-xs text-term-dim pt-1">
                    narration source: {ev.narration_source || 'n/a'} · regime:{' '}
                    {ev.regime_ok ? 'OK' : 'BAD'}
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
