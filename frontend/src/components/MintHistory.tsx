import { useCallback, useEffect, useRef, useState } from 'react'
import type { FeedEventRow, MintHistoryResponse } from '../types'
import { apiUrl } from '../lib/api'
import { registerList, type ListController } from '../lib/shortcuts'
import { clock, shortAddr } from '../lib/format'
import { CopyText, Empty, ErrorState, RuleLine, Skeleton } from './ui'

/**
 * §65 — mint-history drill-down: EVERY feed event ever recorded for ONE mint,
 * newest first (GET /api/mint/{mint}/history) — not just the window the live
 * tape holds. Directly useful for a system that re-evaluates mints it has
 * seen before (blocklist, cooldown, scale-in candidates).
 *
 * Navigation-stack semantics: this is a LEVEL above the tab views, not a
 * disconnected modal — Esc collapses its expanded row first, then backs out
 * (the App-level Esc order), and its list registers with the §65 keyboard
 * registry so j/k/g/G/Enter operate it exactly like the tape.
 *
 * DESIGN.md §3 five states: loading (skeleton), empty (explicit sentence),
 * error (what failed + automatic retry — a scheduled 5s retry), offline
 * (app-level banner), stale (single fetch per open; backing out and reopening
 * refreshes — no hidden polling behind a drill-down).
 */
const PAGE = 100
const RETRY_MS = 5000

export default function MintHistory({
  mint,
  onClose,
}: {
  mint: string
  onClose: () => void
}) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [total, setTotal] = useState(0)
  const [events, setEvents] = useState<FeedEventRow[]>([])
  const [expanded, setExpanded] = useState<number | null>(null)
  const [cursor, setCursor] = useState(0)
  const [done, setDone] = useState(false) // fetched through to the end
  const listRef = useRef<HTMLDivElement>(null)
  const retryTimer = useRef<number | null>(null)
  const inFlight = useRef(false)

  // Fresh state for the §65 registry (subscribed once, never stale).
  const stateRef = useRef({ events, expanded, cursor, total })
  stateRef.current = { events, expanded, cursor, total }

  const fetchPage = useCallback(
    async (offset: number) => {
      if (inFlight.current) return
      inFlight.current = true
      if (retryTimer.current) {
        window.clearTimeout(retryTimer.current)
        retryTimer.current = null
      }
      setLoading(offset === 0)
      setError(null)
      try {
        const r = await fetch(
          apiUrl(`/api/mint/${encodeURIComponent(mint)}/history?limit=${PAGE}&offset=${offset}`),
        )
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        const body = (await r.json()) as MintHistoryResponse
        setTotal(body.total)
        setDone(offset + body.events.length >= body.total)
        setEvents((prev) => {
          if (offset === 0) return body.events
          // A retry/overlap can return rows already loaded; never duplicate
          // React keys (and never grow the list with the same page twice).
          const seen = new Set(prev.map((ev) => ev.id))
          return [...prev, ...body.events.filter((ev) => !seen.has(ev.id))]
        })
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
        // §3.3: the retry is automatic, not a dead end.
        if (retryTimer.current) window.clearTimeout(retryTimer.current)
        retryTimer.current = window.setTimeout(() => void fetchPage(offset), RETRY_MS)
      } finally {
        inFlight.current = false
        setLoading(false)
      }
    },
    [mint],
  )

  useEffect(() => {
    void fetchPage(0)
    return () => {
      if (retryTimer.current) window.clearTimeout(retryTimer.current)
    }
  }, [fetchPage])

  // §65 keyboard navigation — registered so j/k/Enter/g/G/Esc operate this
  // list exactly like the tape, with the same guards (App's dispatcher).
  const ctrlRef = useRef<ListController>(null as unknown as ListController)
  ctrlRef.current = {
    id: 'mint-history',
    owns: (el) => !!listRef.current && (el === listRef.current || listRef.current.contains(el)),
    length: () => stateRef.current.events.length,
    moveDown: () =>
      setCursor((c) => Math.min(c + 1, Math.max(stateRef.current.events.length - 1, 0))),
    moveUp: () => setCursor((c) => Math.max(c - 1, 0)),
    jumpTop: () => setCursor(0),
    jumpBottom: () => setCursor(Math.max(stateRef.current.events.length - 1, 0)),
    toggle: () => {
      const ev = stateRef.current.events[stateRef.current.cursor]
      if (ev) setExpanded((cur) => (cur === ev.id ? null : ev.id))
    },
    collapse: () => {
      if (stateRef.current.expanded !== null) {
        setExpanded(null)
        return true
      }
      return false
    },
  }
  useEffect(() => registerList('mint-history', ctrlRef), [])

  // §84: the list registers with the global vocabulary, but while IT has
  // focus App's dispatcher stands down (`owns`). Without a local handler
  // j/k/Enter were dead exactly when the user was inside the list.
  function onListKeyDown(e: React.KeyboardEvent) {
    const { events: rows, cursor: cur } = stateRef.current
    if (rows.length === 0) return
    const target = e.target
    // A focused row button owns Enter (native click expands that row).
    if (e.key === 'Enter' && target instanceof HTMLElement
        && target.closest('button, a, [role="button"]')) return
    if (e.key === 'j' || e.key === 'ArrowDown') {
      e.preventDefault()
      setCursor((c) => Math.min(c + 1, rows.length - 1))
    } else if (e.key === 'k' || e.key === 'ArrowUp') {
      e.preventDefault()
      setCursor((c) => Math.max(c - 1, 0))
    } else if (e.key === 'g') {
      e.preventDefault()
      setCursor(0)
    } else if (e.key === 'G') {
      e.preventDefault()
      setCursor(rows.length - 1)
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const ev = rows[cur]
      if (ev) setExpanded((open) => (open === ev.id ? null : ev.id))
    }
  }

  // Keep the cursor row in view as j/k moves it.
  useEffect(() => {
    listRef.current
      ?.querySelector(`[data-mh-row="${cursor}"]`)
      ?.scrollIntoView({ block: 'nearest' })
  }, [cursor])

  const hasMore = !done && events.length < total

  return (
    <MintHistoryBody
      mint={mint}
      onClose={onClose}
      loading={loading}
      error={error}
      total={total}
      events={events}
      expanded={expanded}
      cursor={cursor}
      listRef={listRef}
      onExpand={setExpanded}
      onKeyDown={onListKeyDown}
      onRowFocus={setCursor}
      hasMore={hasMore}
      onOlder={() => void fetchPage(events.length)}
    />
  )
}

/** The drill-down's presentation: rows identical in voice to the tape. */
function MintHistoryBody({
  mint, onClose, loading, error, total, events, expanded, cursor,
  listRef, onExpand, onKeyDown, onRowFocus, hasMore, onOlder,
}: {
  mint: string
  onClose: () => void
  loading: boolean
  error: string | null
  total: number
  events: FeedEventRow[]
  expanded: number | null
  cursor: number
  listRef: React.RefObject<HTMLDivElement | null>
  onExpand: (id: number | null) => void
  onKeyDown: (e: React.KeyboardEvent) => void
  onRowFocus: (i: number) => void
  hasMore: boolean
  onOlder: () => void
}) {
  return (
    <section data-testid="mint-history" className="p-4 xl:p-6 max-w-[1100px] mx-auto">
      <div className="panel">
        <div className="panel-header">
          <h2 className="panel-title">mint history</h2>
          <div className="flex items-center gap-3 min-w-0">
            <CopyText
              value={mint}
              className="font-mono text-[10px] text-dim break-all hover:text-live"
            />
            <button
              type="button"
              onClick={onClose}
              className="text-live underline decoration-line-strong decoration-dotted underline-offset-2 hover:text-bright transition-colors duration-150 ease-out-expo text-[10.5px] min-h-[24px]"
              data-testid="mint-history-back"
            >
              ← back
            </button>
          </div>
        </div>

        <div className="mb-2 text-[10.5px] text-faint">
          every decision ever recorded for this mint · newest first
          {!error ? (
            <span className="tnum">
              {' '}· {total} event{total === 1 ? '' : 's'}
            </span>
          ) : null}
        </div>

        {loading && events.length === 0 ? (
          <Skeleton rows={4} />
        ) : error && events.length === 0 ? (
          <ErrorState
            message={`mint history unavailable for ${shortAddr(mint, 8, 8)} (${error})`}
          />
        ) : events.length === 0 ? (
          <Empty>
            No recorded decisions for this mint. It has never been evaluated by
            the engine, or it predates the feed_events table.
          </Empty>
        ) : (
          <>
            {/* A pagination failure keeps the rows already loaded — the
                error is about the NEXT page, not the history we have. */}
            {error && (
              <div
                className="mb-2 text-[10.5px] text-warn"
                data-testid="mint-history-page-error"
              >
                older history unavailable ({error}) · retrying automatically
              </div>
            )}
            <MintHistoryList
              events={events} expanded={expanded} cursor={cursor}
              listRef={listRef} onExpand={onExpand} onKeyDown={onKeyDown}
              onRowFocus={onRowFocus}
            />
          </>
        )}

        {!error && hasMore && (
          <div className="px-4 py-3 border-t border-line-soft">
            <button
              type="button"
              className="text-[10.5px] text-live underline decoration-line-strong decoration-dotted underline-offset-2 hover:text-bright transition-colors duration-150 ease-out-expo min-h-[24px]"
              onClick={onOlder}
              data-testid="mint-history-more"
            >
              load older · {total - events.length} before these
            </button>
          </div>
        )}
      </div>
    </section>
  )
}

/** The scroll-bounded listbox: the SAME row voice as the decisions tape. */
function MintHistoryList({
  events, expanded, cursor, listRef, onExpand, onKeyDown, onRowFocus,
}: {
  events: FeedEventRow[]
  expanded: number | null
  cursor: number
  listRef: React.RefObject<HTMLDivElement | null>
  onExpand: (id: number | null) => void
  onKeyDown: (e: React.KeyboardEvent) => void
  onRowFocus: (i: number) => void
}) {
  return (
    <div
      ref={listRef as React.RefObject<HTMLDivElement>}
      className="overflow-y-auto max-h-[65vh] border border-line-soft rounded focus-visible:outline focus-visible:outline-1 focus-visible:-outline-offset-1 focus-visible:outline-line-strong"
      tabIndex={0}
      role="listbox"
      aria-label="mint history · j/k move, enter expand, esc collapse"
      aria-activedescendant={`mh-row-${cursor}`}
      data-testid="mint-history-list"
      onKeyDown={onKeyDown}
    >
      {events.map((ev, i) => {
        const isOpen = expanded === ev.id
        return (
          <div
            key={ev.id}
            id={`mh-row-${i}`}
            data-mh-row={i}
            role="option"
            aria-selected={i === cursor}
            onFocus={() => onRowFocus?.(i)}
            className={`border-b border-line-soft ${
              i === cursor ? 'bg-surface shadow-[inset_2px_0_0_0] shadow-live' : ''
            }`}
          >
            <button
              className="w-full text-left px-4 py-3 hover:bg-surface min-h-[24px]"
              onClick={() => onExpand(isOpen ? null : ev.id)}
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
                <span className="tnum">{clock(ev.ts)}</span>
              </div>
            </button>
            {isOpen && (
              <div className="mx-4 mb-3 bg-raised border border-line-soft rounded p-3 space-y-2.5">
                <div className="text-xs whitespace-pre-wrap text-body">{ev.thesis}</div>
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
  )
}

