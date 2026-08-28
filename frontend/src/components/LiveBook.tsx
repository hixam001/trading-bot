import type { LivePortfolioResponse } from '../types'
import { Badge, Empty, Panel, Stat } from './ui'
import { num, pnlClass, price, shortAddr, signedUsd, usd } from '../lib/format'

/**
 * The LIVE book — the real wallet, real positions, real money. This is the
 * headline panel: it leads the dashboard whenever live execution is armed.
 * Cash is the wallet's on-chain USDC balance (never simulated). Every figure
 * is rendered verbatim from the backend (DESIGN.md §5: no client-side math).
 */
export default function LiveBook({ book }: { book: LivePortfolioResponse }) {
  if (!book.enabled) return null
  const positions = book.positions ?? []

  return (
    <Panel
      testId="live-book"
      title="Live book — real money"
      right={
        <div className="flex items-center gap-2">
          <Badge tone="neg">● LIVE</Badge>
          {book.manual_confirmation ? (
            <Badge tone="warn">manual confirm</Badge>
          ) : (
            <Badge tone="dim">autonomous</Badge>
          )}
        </div>
      }
    >
      {/* Wallet identity — full address in the title attr, short form shown. */}
      <div className="text-xs text-dim mb-3" title={book.wallet ?? undefined}>
        wallet <span className="text-body">{shortAddr(book.wallet)}</span>
      </div>

      {/* Headline stats. Equity is the largest; the rest support it. */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-x-4 gap-y-3">
        <Stat label="Equity" value={usd(book.equity_usd)} valueClass="text-2xl font-semibold" />
        <Stat label="Cash · USDC" value={usd(book.cash_usd)} />
        <Stat label="Open value" value={usd(book.open_value_usd)} />
        <Stat
          label="Unrealized P&L"
          value={signedUsd(book.unrealized_pnl_usd)}
          valueClass={pnlClass(book.unrealized_pnl_usd)}
        />
        <Stat
          label="Realized P&L"
          value={signedUsd(book.realized_pnl_usd)}
          valueClass={pnlClass(book.realized_pnl_usd)}
        />
        <Stat label="SOL · fees" value={num(book.sol_balance)} />
      </div>

      <div className="divider" />

      {positions.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-xs border-collapse">
            <thead>
              <tr>
                <th className="th">Token</th>
                <th className="th text-right">Cost</th>
                <th className="th text-right">Mark</th>
                <th className="th text-right">Value</th>
                <th className="th text-right">uP&L</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.mint_address} className="hover:bg-raised">
                  <td className="td font-semibold text-bright">{p.symbol}</td>
                  <td className="td-num">{usd(p.cost_usd)}</td>
                  <td className="td-num">{price(p.current_price_usd)}</td>
                  <td className="td-num">{usd(p.value_usd)}</td>
                  <td className={`td-num ${pnlClass(p.unrealized_pnl_usd)}`}>
                    {signedUsd(p.unrealized_pnl_usd)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty>
          No open live positions. Deployed today: {usd(book.deployed_today_usd)} ·
          closed trades: {book.closed_trades ?? 0}.
        </Empty>
      )}
    </Panel>
  )
}

