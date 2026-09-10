import type { LivePortfolioResponse } from '../types'
import { CopyText, Empty } from './ui'
import { clock, num, pnlClass, price, shortAddr, signedUsd, usd } from '../lib/format'

/**
 * Live positions — the dashboard's middle column. The wallet equity headline
 * lives in the hero band (§59); this panel carries the open positions
 * themselves, each with its COMPLETE contract address visible and
 * click-to-copy (operator directive, §59). The list is scroll-bounded so it
 * never stretches the column. Every figure is rendered verbatim from
 * /api/live/portfolio (DESIGN.md §5: no client-side math).
 */
export default function LiveBook({ book }: { book: LivePortfolioResponse }) {
  if (!book.enabled) return null
  const positions = book.positions ?? []
  const hidden = book.chain_excluded ?? []

  return (
    <section data-testid="live-book" className="panel flex flex-col flex-1 min-h-0 m-3">
      <div className="panel-header">
        <h2 className="panel-title">positions</h2>
        <span className="font-mono text-[10px] text-faint tnum">{positions.length} open</span>
      </div>

      <div className="grid grid-cols-2 gap-2 mb-2">
        <div className="stat-card">
          <div className="stat-label">Open value</div>
          <div className="stat-value">{usd(book.open_value_usd)}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Unrealized P&L</div>
          <div className={`stat-value ${pnlClass(book.unrealized_pnl_usd)}`}>
            {signedUsd(book.unrealized_pnl_usd)}
          </div>
        </div>
      </div>

      {positions.length === 0 ? (
        <Empty>
          No open live positions. Deployed today: {usd(book.deployed_today_usd)} ·
          closed trades: {book.closed_trades ?? 0}.
        </Empty>
      ) : (
        <div className="flex-1 min-h-0 overflow-y-auto max-h-[65vh] xl:max-h-none">
          {positions.map((p) => (
            <div key={p.mint_address} className="hrow">
              <div className="flex items-baseline justify-between gap-2">
                <span className="font-bold text-bright">${p.symbol}</span>
                <span
                  className={`font-mono text-[11px] font-semibold tnum ${pnlClass(p.unrealized_pnl_usd)}`}
                >
                  {signedUsd(p.unrealized_pnl_usd)}
                </span>
              </div>
              {/* Complete contract address, click-to-copy (§59). */}
              <div className="mt-1">
                <CopyText
                  value={p.mint_address}
                  className="font-mono text-[10px] text-dim break-all hover:text-live"
                />
              </div>
              <div className="flex justify-between gap-2 mt-1 font-mono text-[10.5px] text-faint tnum">
                <span>
                  {num(p.tokens)} tok · mark {price(p.current_price_usd)}
                </span>
                <span>{usd(p.value_usd)}</span>
              </div>
            </div>
          ))}
        </div>
      )}

      {hidden.length > 0 && (
        <div className="mt-2 text-[10.5px] text-warn">
          {hidden.length} journal position{hidden.length === 1 ? '' : 's'} not
          shown · sold on-chain · scan{' '}
          {book.chain_scan?.stale ? 'stale' : clock(book.chain_scan?.at_utc)}
        </div>
      )}

      <div className="mt-2 text-[10.5px] text-faint" title={book.wallet ?? undefined}>
        wallet {shortAddr(book.wallet)}
      </div>
    </section>
  )
}