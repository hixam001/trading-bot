import { useState } from 'react'
import type { FeedEventRow, RuleResultRow } from '../types'
import { CopyText, Empty } from './ui'
import { clock } from '../lib/format'

function RuleLine({ r }: { r: RuleResultRow }) {
  // §43: a rule the engine deliberately did not evaluate (metered crowd feed,
  // reserved for candidates that cleared every other rule) is shown as SKIP —
  // never as a failure it did not actually report. Rows written before §43
  // have no `evaluated` field; missing means evaluated.
  const skipped = r.evaluated === false
  const dot = skipped ? 'text-dim' : r.passed ? 'text-pass' : 'text-fail'
  return (
    <div className="flex justify-between gap-3 text-[10.5px] py-0.5">
      <span className="text-dim whitespace-nowrap">
        <span className={`mr-1 ${dot}`} aria-hidden="true">●</span>
        {r.rule_id}
      </span>
      <span className="text-faint text-right">{r.detail}</span>
    </div>
  )
}

/**
 * The decisions tape — the main content. Rows are full-width <button>s
 * (keyboard operable, aria-expanded) that reveal the COMPLETE contract
 * address (click-to-copy), the model's verbatim answer, and the rule-by-rule
 * breakdown (DESIGN.md §2/§4).
 *
 * §59 authored motion moment: the row that just arrived over the WS lifts
 * signal-cyan once and settles (0.9s, expo ease-out) — the machine decided,
 * and the tape tells you where. Hydration never flashes; only live arrivals
 * do. The list is scroll-bounded: new decisions never stretch the page —
 * the tape scrolls inside its own viewport (operator directive, §59).
 */
export default function LiveFeed({
  events,
  freshId,
}: {
  events: FeedEventRow[]
  freshId: number | null
}) {
  const [expanded, setExpanded] = useState<number | null>(null)

  return (
    <section data-testid="live-feed" className="flex flex-col flex-1 min-h-0">
      {events.length === 0 ? (
        <div className="px-4 py-7">
          <Empty>
            No decisions yet this cycle. Rows appear as the model evaluates each
            candidate against the rule set.
          </Empty>
        </div>
      ) : (
        <div className="flex-1 min-h-0 overflow-y-auto max-h-[65vh] xl:max-h-none">
          {events.map((ev) => {
            // All rules passed but no entry -> the model itself declined.
            const modelDeclined = ev.verdict !== 'pass' && ev.failed_rule_ids.length === 0
            const isOpen = expanded === ev.id
            const isFresh = ev.id === freshId
            return (
              <div
                key={ev.id}
                className={`border-b border-line-soft ${isFresh ? 'row-flash' : ''}`}
              >
                <button
                  className="w-full text-left px-4 py-3 hover:bg-surface min-h-[24px]"
                  onClick={() => setExpanded(isOpen ? null : ev.id)}
                  aria-expanded={isOpen}
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="font-bold text-bright">${ev.symbol}</span>
                    <span
                      className={`font-mono text-[10.5px] font-bold tracking-[0.03em] ${
                        ev.verdict === 'pass' ? 'text-pass' : 'text-reject'
                      }`}
                    >
                      [{ev.verdict === 'pass' ? 'ENTER' : 'SKIP'}]
                    </span>
                  </div>
                  <p className="text-xs text-dim leading-relaxed mt-1 line-clamp-2">
                    {ev.thesis}
                  </p>
                  <div className="flex items-center gap-3 mt-1.5 text-[10.5px] text-faint">
                    {ev.grounding_flags.length > 0 && (
                      <span className="text-warn" title={ev.grounding_flags.join('; ')}>
                        flags {ev.grounding_flags.length}
                      </span>
                    )}
                    <span className="tnum">{clock(ev.ts)}</span>
                  </div>
                </button>

                {isOpen && (
                  <div className="mx-4 mb-3 bg-raised border border-line-soft rounded p-3 space-y-2.5">
                    {/* The complete contract address — click to copy. */}
                    <div className="flex items-center gap-2 flex-wrap text-[10.5px]">
                      <span className="text-faint shrink-0">contract:</span>
                      <CopyText
                        value={ev.mint_address || 'unknown'}
                        className="font-mono text-[10.5px] text-dim break-all hover:text-live"
                      />
                    </div>

                    {/* Complete model answer, verbatim */}
                    <div>
                      <div className="text-[10.5px] text-faint mb-1">
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
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-5">
                      {ev.rule_breakdown.map((r) => (
                        <RuleLine key={r.rule_id} r={r} />
                      ))}
                    </div>

                    <div className="text-[10.5px] text-faint pt-1">
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
    </section>
  )
}