import { useMemo, useState } from 'react'
import type { TradeRow } from '../types'

type SortKey = 'closed_at' | 'realized_pnl_usd' | 'symbol'

export default function TradeJournal({ trades, total }: { trades: TradeRow[]; total: number }) {
  const [sortKey, setSortKey] = useState<SortKey>('closed_at')
  const [filter, setFilter] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)

  const sorted = useMemo(() => {
    const t = [...trades]
    t.sort((a, b) =>
      sortKey === 'symbol'
        ? a.symbol.localeCompare(b.symbol)
        : String(b[sortKey]).localeCompare(String(a[sortKey])),
    )
    return filter
      ? t.filter(
          (x) =>
            x.symbol.toLowerCase().includes(filter.toLowerCase()) ||
            (x.exit_reason ?? '').includes(filter),
        )
      : t
  }, [trades, sortKey, filter])

  return (
    <div className="panel overflow-auto" style={{ maxHeight: '70vh' }}>
      <div className="panel-title flex justify-between items-center">
        <span>trade journal ({trades.length} of {total} closed)</span>
        <span className="flex gap-2 items-center normal-case tracking-normal">
          <input
            className="bg-term-bg border border-term-border rounded px-2 py-0.5 text-xs"
            placeholder="filter…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <select
            className="bg-term-bg border border-term-border rounded px-1 py-0.5 text-xs"
            value={sortKey}
            onChange={(e) => setSortKey(e.target.value as SortKey)}
          >
            <option value="closed_at">newest</option>
            <option value="realized_pnl_usd">p&l</option>
            <option value="symbol">symbol</option>
          </select>
        </span>
      </div>
      {sorted.length === 0 && (
        <div className="text-term-dim text-xs">No closed trades yet.</div>
      )}
      <div className="space-y-1">
        {sorted.map((t) => (
          <div key={t.trade_id} className="border-b border-term-border/40 pb-1">
            <button
              className="w-full flex items-center gap-3 text-left hover:bg-term-bg px-1 rounded"
              onClick={() => setExpanded(expanded === t.trade_id ? null : t.trade_id)}
            >
              <span className="font-bold w-20">{t.symbol}</span>
              <span
                className={
                  (t.realized_pnl_usd ?? 0) >= 0 ? 'text-term-green' : 'text-term-red'
                }
              >
                ${t.realized_pnl_usd?.toFixed(2)} ({t.realized_pnl_pct?.toFixed(1)}%)
              </span>
              <span className="text-term-dim text-xs">{t.exit_reason}</span>
              <span className="text-term-dim text-xs ml-auto">
                {t.closed_at ? new Date(t.closed_at).toLocaleString() : ''}
              </span>
            </button>
            {expanded === t.trade_id && (
              <div className="text-xs bg-term-bg/40 p-2 rounded mt-1 space-y-1">
                <div>
                  <span className="text-term-dim">entry thesis: </span>
                  {t.thesis || '—'}
                </div>
                <div>
                  <span className="text-term-dim">reflection: </span>
                  {t.reflection_text || '(pending)'}
                </div>
                <div className="text-term-dim">
                  entry ${t.entry_price_usd.toExponential(3)} → exit $
                  {t.exit_price_usd?.toExponential(3)} · size ${t.position_size_usd.toFixed(2)}
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
