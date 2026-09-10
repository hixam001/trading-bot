import type { StatsResponse } from '../types'
import { Badge, Panel } from './ui'
import { pnlClass, signedUsd, usd } from '../lib/format'

/**
 * Performance (§59) — the book's track-record numbers, no charts (the
 * operator's terminal constraint): win rate, profit factor, max drawdown,
 * realized/unrealized split, and the trade counts they rest on. Every
 * figure renders verbatim from /api/stats (DESIGN.md §5: no client-side
 * math); null renders `—`, never a fabricated zero.
 *
 * The promotion-gate truth is stated in one honest line, not hidden: with
 * too few closed trades the numbers are marked provisional.
 *
 * The footer's "track equity" is deliberately NOT labeled "equity": it is
 * the anchored book (initial cash + closed realized P&L) the track record
 * is measured against (§52 anchor), while the real wallet's equity leads
 * in the hero band — two different numbers that must never be confused
 * on one page.
 */
export default function Performance({ stats }: { stats: StatsResponse }) {
  const provisional = stats.closed_trades < 10
  return (
    <Panel
      testId="performance"
      title="Performance"
      right={
        provisional ? (
          <Badge tone="warn">provisional · {stats.closed_trades} closed</Badge>
        ) : (
          <Badge tone="dim">{stats.closed_trades} closed</Badge>
        )
      }
    >
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
        <div className="stat-card">
          <div className="stat-label">Total P&L</div>
          <div className={`stat-value ${pnlClass(stats.total_pnl_usd)}`}>
            {signedUsd(stats.total_pnl_usd)}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Realized</div>
          <div className={`stat-value ${pnlClass(stats.realized_pnl_usd)}`}>
            {signedUsd(stats.realized_pnl_usd)}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Unrealized</div>
          <div className={`stat-value ${pnlClass(stats.unrealized_pnl_usd)}`}>
            {signedUsd(stats.unrealized_pnl_usd)}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Win rate</div>
          <div className="stat-value">
            {stats.win_rate === null ? '—' : `${(stats.win_rate * 100).toFixed(1)}%`}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Profit factor</div>
          <div className="stat-value">
            {stats.profit_factor === null ? '—' : stats.profit_factor.toFixed(2)}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Max drawdown</div>
          <div className="stat-value">
            {stats.max_drawdown_pct === null ? '—' : `−${stats.max_drawdown_pct.toFixed(1)}%`}
          </div>
        </div>
      </div>
      <div className="divider" />
      <div className="text-xs text-dim flex flex-wrap gap-x-4 gap-y-1">
        <span>
          open positions <span className="text-body tnum">{stats.open_positions}</span>
        </span>
        <span>
          open cost basis <span className="text-body tnum">{usd(stats.total_spend_usd)}</span>
        </span>
        <span
          title="Track equity: the anchored book (initial cash + closed realized P&L) the track record is measured against — not the wallet. The real wallet's equity leads in the hero band."
        >
          track equity <span className="text-body tnum">{usd(stats.equity_usd)}</span>
        </span>
      </div>
    </Panel>
  )
}