import type { LivePortfolioResponse } from '../types'
import { Badge, CopyText, Empty, HistoryButton, Panel } from './ui'
import { clock, num, pnlClass, price, signedUsd, usd } from '../lib/format'

/**
 * Holdings — the dedicated live positions page. Same data as the dashboard's
 * positions panel (single source: /api/live/portfolio), rendered wide: every
 * open position with entry, mark, value, unrealized P&L, age, and the
 * COMPLETE contract address, click-to-copy (operator directive, §59).
 * §65: a `history` affordance beside the address opens the mint drill-down —
 * a separate click target, never overloading the copy button.
 * Every figure is rendered verbatim from the backend (DESIGN.md §5).
 */
export default function Holdings({
  book,
  focusMint,
  onMintHistory,
}: {
  book: LivePortfolioResponse
  /** §63: mint jumped to from the command palette — row highlighted. */
  focusMint?: string | null
  /** §65: opens the mint-history drill-down level. */
  onMintHistory?: (mint: string) => void
}) {
  if (!book.enabled) {
    return (
      <Panel testId="holdings" title="Holdings">
        <Empty>Live book not available: {book.reason ?? 'unknown reason'}.</Empty>
      </Panel>
    )
  }
  const positions = book.positions ?? []
  const hidden = book.chain_excluded ?? []

  return (
    <Panel
      testId="holdings"
      /* §66: the title is the view's name; the count moved into the header's
         right slot (the form the dashboard's positions panel already uses) and
         the redundant `live positions` subtitle is gone. */
      title="Holdings"
      right={
        <span className="flex items-baseline gap-3">
          <span className="font-mono text-[10px] text-faint tnum">
            {positions.length} open
          </span>
          <Badge tone="fail">● LIVE · real money</Badge>
        </span>
      }
    >
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-3">
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
        <div className="stat-card">
          <div className="stat-label">Cash</div>
          <div className="stat-value">{usd(book.cash_usd)}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Deployed today</div>
          <div className="stat-value">{usd(book.deployed_today_usd)}</div>
        </div>
      </div>

      {(book.chain_excluded?.length ?? 0) > 0 && (
        <div className="mb-2 text-[10.5px] text-warn">
          {hidden.length} journal position{hidden.length === 1 ? '' : 's'} not
          shown · sold on-chain · scan{' '}
          {book.chain_scan?.stale ? 'stale' : clock(book.chain_scan?.at_utc)}
        </div>
      )}

      <div className="divider" />

      {positions.length === 0 ? (
        <Empty>
          No open live positions. The bot buys only when the model says buy AND
          all gate rules pass; closed trades so far: {book.closed_trades ?? 0}.
        </Empty>
      ) : (
        <div className="overflow-x-auto">
          {/* Responsive commitment (§63 Stage 2): below the table's natural
              width it scrolls INSIDE this container — never squashes, never
              stretches the page. */}
          <table className="w-full min-w-[720px] text-xs border-collapse">
            <thead>
              <tr>
                <th className="th">Token</th>
                <th className="th">Contract</th>
                <th className="th text-right">Size</th>
                <th className="th text-right">Entry</th>
                <th className="th text-right">Mark</th>
                <th className="th text-right">Value</th>
                <th className="th text-right">Unrl P&L</th>
                <th className="th text-right">Opened</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr
                  key={p.mint_address}
                  data-mint={p.mint_address}
                  className={
                    focusMint === p.mint_address
                      ? 'bg-raised shadow-[inset_2px_0_0_0] shadow-live'
                      : 'hover:bg-raised'
                  }
                >
                  <td className="td font-semibold text-bright">${p.symbol}</td>
                  <td className="td max-w-[260px]">
                    <span className="inline-flex items-center gap-1.5 flex-wrap">
                      <CopyText
                        value={p.mint_address}
                        className="font-mono text-[10px] text-dim break-all hover:text-live"
                      />
                      {onMintHistory && (
                        <HistoryButton mint={p.mint_address} onOpen={onMintHistory} />
                      )}
                    </span>
                  </td>
                  <td className="td-num">
                    {usd(p.cost_usd)}
                    <div className="font-mono text-[10px] text-faint">{num(p.tokens)} tok</div>
                  </td>
                  <td className="td-num">{price(p.entry_price_usd)}</td>
                  <td className="td-num">{price(p.current_price_usd)}</td>
                  <td className="td-num">{usd(p.value_usd)}</td>
                  <td className={`td-num ${pnlClass(p.unrealized_pnl_usd)}`}>
                    {signedUsd(p.unrealized_pnl_usd)}
                  </td>
                  <td className="td-num">{clock(p.opened_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}