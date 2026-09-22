import { useEffect, useRef, useState } from 'react'
import type { LiveCommitEntry, LiveExecutionsResponse } from '../types'
import type { PaletteJournalFilter } from './CommandPalette'
import { Badge, CopyText, Empty, HistoryButton, Panel, type Tone } from './ui'
import { num, pnlClass, price, shortAddr, signedUsd, usd } from '../lib/format'
import { registerList, type ListController } from '../lib/shortcuts'

/**
 * Journal — the live order history. Two verbatim views of the live_execution
 * state dir (served read-only by /api/live/executions):
 *
 *   1. order decisions — every sealed commit with its lifecycle:
 *      sealed -> published (memo on-chain) -> bound (fill), or failed + reason.
 *      This is the page that answers "the bot said enter — why didn't it buy?"
 *      The table is scroll-bounded: it never stretches the page as decisions
 *      accumulate (operator directive, §59).
 *   2. money ledger — CLOSED TRADES ONLY (operator directive, §59): the
 *      execution ledger's confirmed closes, each with proceeds and realized
 *      P&L. Buys stay visible in the order-decisions lifecycle above.
 *
 * The COMPLETE contract (mint) address of every coin is shown, click-to-copy.
 * No client-side money math (DESIGN.md §5); states per DESIGN.md §3.
 */

const commitTone: Record<LiveCommitEntry['status'], { tone: Tone; label: string }> = {
  bound: { tone: 'pass', label: 'filled' },
  published: { tone: 'warn', label: 'memo only · no fill' },
  sealed: { tone: 'live', label: 'sealed' },
  failed: { tone: 'fail', label: 'failed' },
}

function ts(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return '—'
  const d = new Date(epochSeconds * 1000)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString('en-GB', {
    hour12: false,
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

function TxLink({ sig, label }: { sig: string | null; label: string }) {
  if (!sig) return <span className="text-dim">—</span>
  return (
    <a
      className="text-live underline decoration-line-strong decoration-dotted underline-offset-2 hover:text-bright transition-colors duration-150 ease-out-expo"
      href={`https://solscan.io/tx/${sig}`}
      target="_blank"
      rel="noopener noreferrer"
      title={sig}
    >
      {label} {shortAddr(sig, 6, 6)}
    </a>
  )
}

export default function Journal({
  data,
  filter,
  onClearFilter,
  onMintHistory,
}: {
  data: LiveExecutionsResponse
  /** §63: filter set from the command palette (status and/or free text). */
  filter?: PaletteJournalFilter
  onClearFilter?: () => void
  /** §65: the `history` affordance beside a mint opens the drill-down. */
  onMintHistory?: (mint: string) => void
}) {
  const [expanded, setExpanded] = useState<string | null>(null)
  // §65 keyboard navigation over the ORDER-DECISIONS rows: j/k move a cursor,
  // enter toggles the cursor row's proof detail (the existing expand
  // mechanism), esc collapses, g/G jump. The money ledger is not registered —
  // its rows have no expand mechanism, so Enter would be a lie.
  const [cursor, setCursor] = useState(0)
  const tableRef = useRef<HTMLDivElement>(null)

  const all = data.commits ?? []
  // §63: optional palette-driven filter — a status and/or free text over
  // symbol, mint and exit (fail) reason. Verbatim rows, client-side view only.
  const active = !!filter && (filter.status !== 'all' || filter.query.trim() !== '')
  const commits = active
    ? all.filter((c) => {
        if (filter!.status !== 'all' && c.status !== filter!.status) return false
        const q = filter!.query.trim().toLowerCase()
        if (!q) return true
        const hay = `${c.payload?.symbol ?? ''} ${c.payload?.mint ?? ''} ${
          c.fail_reason ?? ''
        }`.toLowerCase()
        return hay.includes(q)
      })
    : all
  // §59: the money ledger shows CLOSED TRADES ONLY — confirmed closes with
  // realized P&L. Buys remain in the order-decisions lifecycle above.
  const closes = (data.records ?? []).filter((r) => r.kind === 'close')
  const t = data.totals

  // §65 registration: fresh state through refs, subscribed once on mount.
  // Only the active tab's components are mounted, so this is "the journal
  // list" exactly while the journal tab is up.
  const stateRef = useRef({ commits, expanded, cursor })
  stateRef.current = { commits, expanded, cursor }
  const ctrlRef = useRef<ListController>(null as unknown as ListController)
  ctrlRef.current = {
    id: 'journal',
    length: () => stateRef.current.commits.length,
    moveDown: () =>
      setCursor((c) => Math.min(c + 1, Math.max(stateRef.current.commits.length - 1, 0))),
    moveUp: () => setCursor((c) => Math.max(c - 1, 0)),
    jumpTop: () => setCursor(0),
    jumpBottom: () => setCursor(Math.max(stateRef.current.commits.length - 1, 0)),
    toggle: () => {
      const c = stateRef.current.commits[stateRef.current.cursor]
      if (c) setExpanded((cur) => (cur === c.hash ? null : c.hash))
    },
    collapse: () => {
      if (stateRef.current.expanded !== null) {
        setExpanded(null)
        return true
      }
      return false
    },
  }
  useEffect(() => registerList('journal', ctrlRef), [])

  // Keep the cursor row in view as j/k moves it.
  useEffect(() => {
    tableRef.current
      ?.querySelector(`[data-journal-row="${cursor}"]`)
      ?.scrollIntoView({ block: 'nearest' })
  }, [cursor])

  if (!data.enabled) {
    return (
      <Panel testId="journal" title="Journal · live order history">
        <Empty>Live journal not available: {data.reason ?? 'unknown reason'}.</Empty>
      </Panel>
    )
  }

  return (
    <div className="space-y-3 min-w-0">
      <Panel
        testId="journal"
        title="Journal · live order history"
        right={<Badge tone="fail">● LIVE</Badge>}
      >
        {t && (
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-2 mb-3">
            <div className="stat-card">
              <div className="stat-label">Order decisions</div>
              <div className="stat-value">{String(t.commits)}</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Filled (bound)</div>
              <div className="stat-value text-pass">{String(t.bound)}</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Memo only · no fill</div>
              <div className="stat-value text-warn">{String(t.published_unfilled)}</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Failed</div>
              <div className={`stat-value ${t.failed > 0 ? 'text-fail' : ''}`}>
                {String(t.failed)}
              </div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Closed trades</div>
              <div className="stat-value">{String(t.closes ?? closes.length)}</div>
            </div>
          </div>
        )}

        {active && (
          <div
            className="flex items-center gap-2 mb-2 text-[10.5px] text-warn"
            data-testid="journal-filter-note"
          >
            <span>
              filtered
              {filter!.status !== 'all' ? ` · ${filter!.status}` : ''}
              {filter!.query.trim() ? ` · "${filter!.query.trim()}"` : ''} — {commits.length} of{' '}
              {all.length} decisions
            </span>
            <button
              type="button"
              className="text-live underline decoration-dotted underline-offset-2 hover:text-bright transition-colors duration-150 ease-out-expo"
              onClick={onClearFilter}
            >
              show all
            </button>
          </div>
        )}

        <div className="divider" />

        {commits.length === 0 ? (
          <Empty>
            No sealed order decisions yet. Every ENTER that reaches the executor
            is sealed here before any network call — including the ones that fail.
          </Empty>
        ) : (
          <div ref={tableRef} className="overflow-auto max-h-[60vh] border border-line-soft rounded">
            {/* §63 Stage 2: below natural width this scrolls INSIDE its
                container — never squashes, never stretches the page. */}
            <table className="w-full min-w-[640px] text-xs border-collapse">
              <thead>
                <tr>
                  <th className="th sticky top-0 bg-surface">Time</th>
                  <th className="th sticky top-0 bg-surface">Side</th>
                  <th className="th sticky top-0 bg-surface">Token</th>
                  <th className="th sticky top-0 bg-surface text-right">Size</th>
                  <th className="th sticky top-0 bg-surface">Status</th>
                  <th className="th sticky top-0 bg-surface">Detail</th>
                </tr>
              </thead>
              <tbody>
                {commits.map((c, i) => {
                  const st = commitTone[c.status] ?? { tone: 'dim' as Tone, label: c.status }
                  const isOpen = expanded === c.hash
                  return (
                    <CommitRow
                      key={c.hash}
                      c={c}
                      st={st}
                      isOpen={isOpen}
                      onToggle={() => setExpanded(isOpen ? null : c.hash)}
                      focused={i === cursor}
                      rowIndex={i}
                      mintHistory={onMintHistory ? () => onMintHistory(c.payload?.mint ?? '') : undefined}
                    />
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel
        title="Money ledger · closed trades"
        right={<span className="font-mono text-[10px] text-faint tnum">{closes.length} closed</span>}
      >
        {closes.length === 0 ? (
          <Empty>
            No closed trades yet. A row appears here only when a position is
            fully closed on-chain (proceeds + realized P&L); open buys live in
            the order decisions above.
          </Empty>
        ) : (
          <div className="overflow-auto max-h-[60vh] border border-line-soft rounded">
            {/* §63 Stage 2: same in-container scroll guard as the table above. */}
            <table className="w-full min-w-[720px] text-xs border-collapse">
              <thead>
                <tr>
                  <th className="th sticky top-0 bg-surface">Time</th>
                  <th className="th sticky top-0 bg-surface">Kind</th>
                  <th className="th sticky top-0 bg-surface">Contract</th>
                  <th className="th sticky top-0 bg-surface text-right">USD</th>
                  <th className="th sticky top-0 bg-surface text-right">Tokens</th>
                  <th className="th sticky top-0 bg-surface text-right">Price</th>
                  <th className="th sticky top-0 bg-surface text-right">P&L</th>
                  <th className="th sticky top-0 bg-surface">Signature</th>
                </tr>
              </thead>
              <tbody>
                {closes.map((r) => (
                  <tr key={`${r.idempotency_key}-${r.ts}`} className="hover:bg-raised">
                    <td className="td whitespace-nowrap text-dim">{ts(r.ts)}</td>
                    <td className="td font-semibold text-bright">{r.kind}</td>
                    <td className="td max-w-[240px]">
                      <span className="inline-flex items-center gap-1.5 flex-wrap">
                        <CopyText
                          value={r.mint}
                          className="font-mono text-[10px] text-dim break-all hover:text-live"
                        />
                        {onMintHistory && (
                          <HistoryButton mint={r.mint} onOpen={onMintHistory} />
                        )}
                      </span>
                    </td>
                    <td className="td-num">{usd(r.usd_size, 4)}</td>
                    <td className="td-num">{num(r.tokens_out)}</td>
                    <td className="td-num">{price(r.price_usd)}</td>
                    <td className={`td-num ${pnlClass(r.pnl_usd)}`}>{signedUsd(r.pnl_usd)}</td>
                    <td className="td">
                      <TxLink sig={r.signature || null} label="tx" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  )
}

/** One commit row + its expandable proof detail (hash, memo, fill, mint). */
function CommitRow({
  c,
  st,
  isOpen,
  onToggle,
  focused = false,
  rowIndex,
  mintHistory,
}: {
  c: LiveCommitEntry
  st: { tone: Tone; label: string }
  isOpen: boolean
  onToggle: () => void
  /** §65: the j/k cursor highlight (a cursor, not a hover state). */
  focused?: boolean
  rowIndex: number
  /** §65: open the mint drill-down (only when a mint exists on the commit). */
  mintHistory?: () => void
}) {
  return (
    <>
      <tr
        data-journal-row={rowIndex}
        className={`hover:bg-raised ${focused ? 'bg-raised shadow-[inset_2px_0_0_0] shadow-live' : ''}`}
      >
        <td className="td whitespace-nowrap text-dim">{ts(c.sealed_at)}</td>
        <td className="td font-semibold text-bright">{c.kind}</td>
        <td className="td">{c.payload?.symbol ?? shortAddr(c.payload?.mint)}</td>
        <td className="td-num">
          {c.payload?.usd !== undefined
            ? usd(c.payload.usd, 4)
            : c.payload?.fraction !== undefined
              ? `${Math.round(c.payload.fraction * 100)}% of position`
              : '—'}
        </td>
        <td className="td">
          <Badge tone={st.tone}>{st.label}</Badge>
        </td>
        <td className="td">
          <button
            className="text-live underline decoration-line-strong decoration-dotted underline-offset-2 hover:text-bright transition-colors duration-150 ease-out-expo"
            aria-expanded={isOpen}
            onClick={onToggle}
          >
            {isOpen ? 'hide' : 'proof'}
          </button>
        </td>
      </tr>
      {isOpen && (
        <tr>
          <td colSpan={6} className="td bg-raised">
            <div className="space-y-1 py-1">
              <div>
                <span className="text-dim">fail reason: </span>
                {c.fail_reason ?? '—'}
              </div>
              <div>
                <span className="text-dim">commit hash: </span>
                <span title={c.hash}>{shortAddr(c.hash, 10, 10)}</span>
              </div>
              <div>
                <span className="text-dim">memo: </span>
                <TxLink sig={c.memo_signature} label="memo" />
                {c.memo_slot ? <span className="text-dim"> · slot {c.memo_slot}</span> : null}
              </div>
              <div>
                <span className="text-dim">fill: </span>
                <TxLink sig={c.signature} label="fill" />
              </div>
              <div>
                <span className="text-dim">mint: </span>
                {c.payload?.mint ? (
                  <span className="inline-flex items-center gap-1.5 flex-wrap">
                    <CopyText
                      value={c.payload.mint}
                      className="font-mono text-[10.5px] text-dim break-all hover:text-live"
                    />
                    {mintHistory && <HistoryButton mint={c.payload.mint} onOpen={() => mintHistory()} />}
                  </span>
                ) : (
                  <span className="text-dim">—</span>
                )}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}