import { useEffect, useMemo, useRef, useState } from 'react'
import type { FeedEventRow, FeedFilter } from '../types'
import { CopyText, Empty, HistoryButton, RuleLine } from './ui'
import { clock } from '../lib/format'
import { registerList, type ListController } from '../lib/shortcuts'

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
 *
 * §64.3 — `filter` narrows the view to one funnel stage. This is a VIEW over
 * verbatim rows, exactly like the journal filter: nothing is re-derived,
 * nothing is fetched, and the classification below is deliberately the same
 * one /api/funnel and scripts/perf_report.py use (verdict=fail with empty
 * failed_rule_ids = the MODEL declined among gate-passers).
 *
 * §64.4 — "since you last looked": this is watched intermittently, so on
 * `visibilitychange -> hidden` the newest visible id is frozen in
 * sessionStorage; on return a hairline divider marks where that boundary sits,
 * making what is new immediately legible. Purely client-side, no new data, and
 * it stays silent until there is genuinely something new below it.
 */
const FILTER_LABEL: Record<FeedFilter, string> = {
  all: 'all decisions',
  approved: 'model approved',
  refused: 'model refused (gate passed)',
  gate_passed: 'gate passed',
  gate_refused: 'gate refused (rule failure)',
}

/** The §57 stage a row belongs to — identical classification to /api/funnel. */
function stageOf(ev: FeedEventRow): FeedFilter {
  if (ev.verdict === 'pass') return 'approved'
  return ev.failed_rule_ids.length === 0 ? 'refused' : 'gate_refused'
}

const SEEN_KEY = 'feed:lastSeenId'

export default function LiveFeed({
  events,
  freshId,
  filter = 'all',
  onClearFilter,
  onMintHistory,
}: {
  events: FeedEventRow[]
  freshId: number | null
  filter?: FeedFilter
  onClearFilter?: () => void
  /** §65: the `history` affordance beside the mint address opens the drill-down. */
  onMintHistory?: (mint: string) => void
}) {
  const [expanded, setExpanded] = useState<number | null>(null)
  // §63 keyboard navigation: j/k move a cursor down/up the tape, enter
  // toggles the cursor row, esc collapses. The tape container is focusable
  // (tabIndex=0) and owns the keys while focused — global j/k would fire
  // while typing elsewhere, so scope beats convenience here. Existing
  // accessibility is untouched: rows stay real <button>s with visible
  // focus rings and aria-expanded.
  const [cursor, setCursor] = useState(0)
  const listRef = useRef<HTMLDivElement>(null)
  // Latest events for the visibility handler, which is subscribed once and
  // must not re-subscribe on every WS arrival.
  const eventsRef = useRef(events)
  eventsRef.current = events

  // §64.4 — the id the feed was at when the tab was last hidden. Read once
  // per session; NaN/missing means "no marker" (first visit: nothing to
  // compare against, so no divider is drawn).
  const [seenId, setSeenId] = useState<number | null>(() => {
    try {
      const raw = sessionStorage.getItem(SEEN_KEY)
      const n = raw === null ? NaN : Number(raw)
      return Number.isFinite(n) ? n : null
    } catch {
      return null
    }
  })

  useEffect(() => {
    function onHide() {
      if (document.visibilityState !== 'hidden') return
      const newest = eventsRef.current[0]?.id
      if (newest === undefined) return
      try {
        sessionStorage.setItem(SEEN_KEY, String(newest))
      } catch {
        /* storage unavailable — the marker simply stays absent */
      }
      setSeenId(newest)
    }
    document.addEventListener('visibilitychange', onHide)
    return () => document.removeEventListener('visibilitychange', onHide)
  }, [])

  // Filtered view (verbatim rows only — a projection, never a re-derivation).
  const visible = useMemo(
    () => events.filter((ev) => filter === 'all' || (filter === 'gate_passed'
      ? ev.verdict === 'pass' || (ev.verdict === 'fail' && ev.failed_rule_ids.length === 0)
      : stageOf(ev) === filter)),
    [events, filter],
  )

  // The boundary row: the first VISIBLE row that is not newer than the frozen
  // id. Everything above it arrived while the tab was in the background.
  // boundary === -1 means EVERY visible row is new, so the divider belongs
  // above the first row. boundary === 0 means nothing is new — the divider is
  // then suppressed entirely (an always-present marker is noise, not signal).
  const boundary = seenId === null ? -1 : visible.findIndex((ev) => ev.id <= seenId)
  const markerAt = boundary === -1 ? 0 : boundary
  const unseenCount = boundary === -1 ? visible.length : boundary
  const showMarker = seenId !== null && unseenCount > 0

  function onListKeyDown(e: React.KeyboardEvent) {
    if (visible.length === 0) return
    if (e.key === 'j' || e.key === 'ArrowDown') {
      e.preventDefault()
      setCursor((c) => Math.min(c + 1, visible.length - 1))
    } else if (e.key === 'k' || e.key === 'ArrowUp') {
      e.preventDefault()
      setCursor((c) => Math.max(c - 1, 0))
    } else if (e.key === 'g') {
      e.preventDefault()
      setCursor(0)
    } else if (e.key === 'G') {
      e.preventDefault()
      setCursor(visible.length - 1)
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const ev = visible[cursor]
      if (ev) setExpanded((cur) => (cur === ev.id ? null : ev.id))
    } else if (e.key === 'Escape') {
      setExpanded(null)
    }
  }

  // §65: the tape registers with the global vocabulary so j/k/g/G/Enter also
  // work WITHOUT the tape having focus (guarded by App's dispatcher). The
  // controller reads fresh state through refs; `owns` makes the dispatcher
  // stand down while this focused listbox handles its own keys — no key ever
  // double-steps. Escape is NOT claimed here: App's dispatcher collapses the
  // row first and then may back out of a drill-down level.
  const ctrlRef = useRef<ListController>(null as unknown as ListController)
  ctrlRef.current = {
    id: 'feed',
    owns: (el) => !!listRef.current && (el === listRef.current || listRef.current.contains(el)),
    length: () => visible.length,
    moveDown: () => setCursor((c) => Math.min(c + 1, Math.max(visible.length - 1, 0))),
    moveUp: () => setCursor((c) => Math.max(c - 1, 0)),
    jumpTop: () => setCursor(0),
    jumpBottom: () => setCursor(Math.max(visible.length - 1, 0)),
    toggle: () => {
      const ev = visible[cursorRef.current]
      if (ev) setExpanded((cur) => (cur === ev.id ? null : ev.id))
    },
    collapse: () => {
      if (expanded !== null) {
        setExpanded(null)
        return true
      }
      return false
    },
  }
  useEffect(() => registerList('feed', ctrlRef), [])
  const cursorRef = useRef(cursor)
  cursorRef.current = cursor

  // Keep the cursor row in view as j/k moves it.
  useEffect(() => {
    listRef.current
      ?.querySelector(`[data-feed-row="${cursor}"]`)
      ?.scrollIntoView({ block: 'nearest' })
  }, [cursor])

  return (
    <section data-testid="live-feed" className="flex flex-col flex-1 min-h-0">
      {filter !== 'all' && (
        <div
          className="sticky top-0 z-20 flex items-center gap-2 px-4 py-2 bg-raised border-b border-line-soft text-[10.5px] text-warn"
          data-testid="feed-filter-note"
        >
          <span>
            funnel drill-down · {FILTER_LABEL[filter]} — {visible.length} of{' '}
            {events.length} decisions
          </span>
          {onClearFilter && (
            <button
              type="button"
              className="text-live underline decoration-dotted underline-offset-2 hover:text-bright transition-colors duration-150 ease-out-expo"
              onClick={onClearFilter}
            >
              show all
            </button>
          )}
        </div>
      )}
      {visible.length === 0 ? (
        <div className="px-4 py-7">
          {filter === 'all' ? (
            <Empty>
              No decisions yet this cycle. Rows appear as the model evaluates each
              candidate against the rule set.
            </Empty>
          ) : (
            <Empty>
              No decisions in the “{FILTER_LABEL[filter]}” stage in the loaded
              window. The tape keeps the newest 200 rows; older stages age out.
            </Empty>
          )}
        </div>
      ) : (
        <div
          ref={listRef}
          className="flex-1 min-h-0 overflow-y-auto max-h-[65vh] xl:max-h-none focus-visible:outline focus-visible:outline-1 focus-visible:-outline-offset-1 focus-visible:outline-line-strong"
          tabIndex={0}
          role="listbox"
          aria-label="decisions tape · j/k move, g/G jump, enter expand, esc collapse"
          aria-activedescendant={`feed-row-${cursor}`}
          onKeyDown={onListKeyDown}
          data-testid="feed-list"
        >
          {visible.map((ev, i) => {
            // All rules passed but no entry -> the model itself declined.
            const modelDeclined = ev.verdict !== 'pass' && ev.failed_rule_ids.length === 0
            const isOpen = expanded === ev.id
            const isFresh = ev.id === freshId
            return (
              <div key={ev.id}>
                {/* §64.4: where this session's attention last ended. Only drawn
                    when rows actually arrived while the tab was hidden. */}
                {showMarker && i === markerAt && (
                  <div
                    className="flex items-center gap-2 px-4 py-1.5 bg-surface/60 border-y border-line-soft"
                    role="separator"
                    data-testid="feed-new-marker"
                  >
                    <span className="font-mono text-[9.5px] tracking-[0.12em] text-faint">
                      ▲ NEW SINCE YOU LAST LOOKED · {unseenCount}
                    </span>
                  </div>
                )}
                <div
                  id={`feed-row-${i}`}
                  data-feed-row={i}
                  role="option"
                  aria-selected={i === cursor}
                  className={`border-b border-line-soft ${isFresh ? 'row-flash' : ''} ${
                    i === cursor ? 'bg-surface shadow-[inset_2px_0_0_0] shadow-live' : ''
                  }`}
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
                      {/* The complete contract address — click to copy (§59).
                          §65: the `history` affordance beside it opens the
                          mint drill-down — a separate target, never the same
                          click (copy stays copy). */}
                      <div className="flex items-center gap-2 flex-wrap text-[10.5px]">
                        <span className="text-faint shrink-0">contract:</span>
                        <CopyText
                          value={ev.mint_address || 'unknown'}
                          className="font-mono text-[10.5px] text-dim break-all hover:text-live"
                        />
                        {onMintHistory && ev.mint_address && (
                          <HistoryButton mint={ev.mint_address} onOpen={onMintHistory} />
                        )}
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
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}