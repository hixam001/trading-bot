import type { LivePortfolioResponse } from '../types'
import { Badge, Empty, Panel, Stat } from './ui'
import { clock, num, pnlClass, price, shortAddr, signedUsd, usd } from '../lib/format'

/**
 * Holdings — the dedicated live positions page. Same data as the dashboard's
 * LiveBook panel (single source: /api/live/portfolio), rendered wide: every
 * open position with entry, mark, value, unrealized P&L, age, and mint.
 * Every figure is rendered verbatim from the backend (DESIGN.md §5).
 */
export default function Holdings({ book }: { book: LivePortfolioResponse }) {
  if (!book.enabled) {
    return (
      <Panel testId="holdings" title="Holdings — live positions">
        <Empty>Live book not available: {book.reason ?? 'unknown reason'}.</Empty>
      </Panel>
    )
  }
  const positions = book.positions ?? []

  return (
    <Panel
      testId="holdings"
      title={`Holdings — ${positions.length} open live position${positions.length === 1 ? '' : 's'}`}
      right={<Badge tone="neg">● LIVE · real money</Badge>}
    >
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-3 mb-3">
        <Stat label="Open value" value={usd(book.open_value_usd)} />
        <Stat
          label="Unrealized P&L"
          value={signedUsd(book.unrealized_pnl_usd)}
          valueClass={pnlClass(book.unrealized_pnl_usd)}
        />
        <Stat label="Cash · USDC" value={usd(book.cash_usd)} />
        <Stat label="Deployed today" value={usd(book.deployed_today_usd)} />
      </div>

      <div className="divider" />

      {positions.length === 0 ? (
        <Empty>
          No open live positions. The bot buys only when the model says buy AND
          all gate rules pass; closed trades so far: {book.closed_trades ?? 0}.
        </Empty>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs border-collapse">
            <thead>
              <tr>
                <th className="th">Token</th>
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
                <tr key={p.mint_address} className="hover:bg-raised">
                  <td className="td">
                    <div className="font-semibold text-bright">{p.symbol}</div>
                    <div className="text-dim" title={p.mint_address}>
                      {shortAddr(p.mint_address)} · {num(p.tokens)} tokens
                    </div>
                  </td>
                  <td className="td-num">{usd(p.cost_usd)}</td>
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
