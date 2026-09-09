import { useState } from 'react'
import type { FeedEventRow, RuleResultRow } from '../types'
import { Badge, Empty, Panel } from './ui'
import { clock } from '../lib/format'

function RuleLine({ r }: { r: RuleResultRow }) {
  // §43: a rule the engine deliberately did not evaluate (metered crowd feed,
  // reserved for candidates that cleared every other rule) is shown as SKIP —
  // never as a failure it did not actually report. Rows written before §43
  // have no `evaluated` field; missing means evaluated.
  const skipped = r.evaluated === false
  const label = skipped ? 'SKIP' : r.passed ? 'PASS' : 'FAIL'
  const tone = skipped ? 'text-dim' : r.passed ? 'text-pos' : 'text-neg'
  return (
    <div className="flex gap-2 text-xs items-baseline">
      <span className={`shrink-0 w-10 font-semibold ${tone}`}>{label}</span>
      <span className="w-44 shrink-0 text-info">{r.rule_id}</span>
      <span className="text-dim">{r.detail}</span>
    </div>
  )
}

/**
 * Live decision feed — the main content. Rows are full-width <button>s
 * (keyboard operable, aria-expanded) that reveal the contract address, the
 * model's verbatim answer, and the rule-by-rule breakdown (DESIGN.md §2/§4).
 *
 * §58 authored motion moment: the row that just arrived over the WS lifts
 * gold once and settles (0.9s, expo ease-out) — the machine decided, and
 * the tape tells you where. Hydration never flashes; only live arrivals do.
 */
export default function LiveFeed({
  events,
  connected,
  freshId,
}: {
  events: FeedEventRow[]
  connected: boolean
  freshId: number | null
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
    <Panel
      testId="live-feed"
      title="Live feed"
      className="flex-1 min-w-0"
      right={
        <Badge tone={connected ? 'pos' : 'neg'}>
          {connected ? '● ws live' : '● ws offline'}
        </Badge>
      }
    >
      {events.length === 0 ? (
        <Empty>
          No decisions yet this cycle. Rows appear as the model evaluates each
          candidate against the rule set.
        </Empty>
      ) : (
        <div className="space-y-1 overflow-y-auto pr-1" style={{ maxHeight: '72vh' }}>
          {events.map((ev) => {
            // All rules passed but no entry -> the model itself declined.
            const modelDeclined = ev.verdict !== 'pass' && ev.failed_rule_ids.length === 0
            const isOpen = expanded === ev.id
            const isFresh = ev.id === freshId
            return (
              <div
                key={ev.id}
                className={`border-b border-line/60 pb-1 ${isFresh ? 'row-flash' : ''}`}
              >
                <button
                  className="w-full text-left flex items-start gap-2 hover:bg-raised px-1.5 py-1 rounded min-h-[24px]"
                  onClick={() => setExpanded(isOpen ? null : ev.id)}
                  aria-expanded={isOpen}
                >
                  <span
                    className={`shrink-0 w-14 font-bold ${
                      ev.verdict === 'pass' ? 'text-pos' : 'text-neg'
                    }`}
                  >
                    {ev.verdict === 'pass' ? 'ENTER' : 'SKIP'}
                  </span>
                  <span className="font-bold w-20 shrink-0 text-bright">{ev.symbol}</span>
                  <span className="text-dim text-xs whitespace-pre-wrap flex-1 line-clamp-2">
                    {ev.thesis}
                  </span>
                  {ev.grounding_flags.length > 0 && (
                    <span
                      className="text-warn text-xs shrink-0"
                      title={ev.grounding_flags.join('; ')}
                    >
                      flags {ev.grounding_flags.length}
                    </span>
                  )}
                  <span className="text-faint text-xs shrink-0 tnum">{clock(ev.ts)}</span>
                </button>

                {isOpen && (
                  <div className="bg-raised/60 border border-gold-deep/60 rounded p-2 mt-1 space-y-2">
                    {/* §58: the expanded row is a selection — the gold-deep
                     * full border marks it; never a colored left edge. */}
                    {/* Token contract address — click to copy. §58: the copy
                     * affordance is the brand's gold — an action, not data. */}
                    <div className="flex items-center gap-2 text-xs flex-wrap">
                      <span className="text-dim shrink-0">contract:</span>
                      <button
                        className="font-mono text-gold hover:text-bright transition-colors duration-150 ease-out-expo break-all text-left underline decoration-gold-deep decoration-dotted underline-offset-2"
                        onClick={() => copyMint(ev.mint_address)}
                        title="click to copy contract address"
                      >
                        {ev.mint_address || 'unknown'}
                      </button>
                      {copiedMint === ev.mint_address && (
                        <span className="text-pos" role="status">copied</span>
                      )}
                    </div>

                    {/* Complete model answer, verbatim */}
                    <div>
                      <div className="text-xs text-dim mb-1">
                        {modelDeclined ? 'model chose not to enter:' : 'model answer:'}
                      </div>
                      <div
                        className={`text-xs whitespace-pre-wrap ${
                          modelDeclined ? 'text-warn' : 'text-body'
                        }`}
                      >
                        {ev.thesis}
                      </div>
                    </div>

                    {/* Rule-by-rule pass/fail breakdown */}
                    <div className="space-y-1">
                      {ev.rule_breakdown.map((r) => (
                        <RuleLine key={r.rule_id} r={r} />
                      ))}
                    </div>

                    <div className="text-xs text-faint pt-1">
                      narration source: {ev.narration_source || 'n/a'} · regime:{' '}
                      {ev.regime_ok ? 'OK' : 'BAD'}
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </Panel>
  )
}

