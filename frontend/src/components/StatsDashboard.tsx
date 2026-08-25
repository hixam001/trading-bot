import type { StatsResponse } from '../types'

function pnlClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return ''
  return v >= 0 ? 'text-term-green' : 'text-term-red'
}

export default function StatsDashboard({ stats }: { stats: StatsResponse }) {
  return (
    <div className="panel">
      <div className="panel-title">portfolio stats</div>
      <div className="grid grid-cols-5 gap-2 text-xs">
        <div>
          <div className="text-term-dim">total equity</div>
          <div className="text-base font-bold">
            ${stats.equity_usd.toFixed(2)}
          </div>
        </div>
        <div>
          <div className="text-term-dim">total spend</div>
          <div>${stats.total_spend_usd.toFixed(2)}</div>
        </div>
        <div>
          <div className="text-term-dim">realized p&l</div>
          <div className={pnlClass(stats.realized_pnl_usd)}>
            ${stats.realized_pnl_usd.toFixed(2)}
          </div>
        </div>
        <div>
          <div className="text-term-dim">unrealized p&l</div>
          <div className={pnlClass(stats.unrealized_pnl_usd)}>
            {stats.unrealized_pnl_usd !== null
              ? `$${stats.unrealized_pnl_usd.toFixed(2)}`
              : '—'}
          </div>
        </div>
        <div>
          <div className="text-term-dim">cash</div>
          <div>${stats.cash_usd.toFixed(2)}</div>
        </div>
      </div>
    </div>
  )
}
