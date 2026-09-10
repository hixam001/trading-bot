import { useState } from 'react'
import type { LiveCommitEntry, LiveExecutionsResponse } from '../types'
import { Badge, CopyText, Empty, Panel, type Tone } from './ui'
import { num, pnlClass, price, shortAddr, signedUsd, usd } from '../lib/format'

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

export default function Journal({ data }: { data: LiveExecutionsResponse }) {
  const [expanded, setExpanded] = useState<string | null>(null)

  if (!data.enabled) {
    return (
      <Panel testId="journal" title="Journal · live order history">
        <Empty>Live journal not available: {data.reason ?? 'unknown reason'}.</Empty>
      </Panel>
    )
  }

  const commits = data.commits ?? []
  // §59: the money ledger shows CLOSED TRADES ONLY — confirmed closes with
  // realized P&L. Buys remain in the order-decisions lifecycle above.
  const closes = (data.records ?? []).filter((r) => r.kind === 'close')
  const t = data.totals

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

        <div className="divider" />

        {commits.length === 0 ? (
          <Empty>
            No sealed order decisions yet. Every ENTER that reaches the executor
            is sealed here before any network call — including the ones that fail.
          </Empty>
        ) : (
          <div className="overflow-auto max-h-[60vh] border border-line-soft rounded">
            <table className="w-full text-xs border-collapse">
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
                {commits.map((c) => {
                  const st = commitTone[c.status] ?? { tone: 'dim' as Tone, label: c.status }
                  const isOpen = expanded === c.hash
                  return (
                    <CommitRow
                      key={c.hash}
                      c={c}
                      st={st}
                      isOpen={isOpen}
                      onToggle={() => setExpanded(isOpen ? null : c.hash)}
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
            <table className="w-full text-xs border-collapse">
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
                      <CopyText
                        value={r.mint}
                        className="font-mono text-[10px] text-dim break-all hover:text-live"
                      />
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
}: {
  c: LiveCommitEntry
  st: { tone: Tone; label: string }
  isOpen: boolean
  onToggle: () => void
}) {
  return (
    <>
      <tr className="hover:bg-raised">
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
                  <CopyText
                    value={c.payload.mint}
                    className="font-mono text-[10.5px] text-dim break-all hover:text-live"
                  />
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