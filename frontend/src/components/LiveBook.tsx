import type { LivePortfolioResponse } from '../types'

function pnlClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return ''
  return v >= 0 ? 'text-term-green' : 'text-term-red'
}

function usd(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return `$${v.toFixed(2)}`
}

/**
 * The LIVE book — the real wallet, real positions, real money. Shown first
 * whenever live execution is armed; the paper book stays below it for
 * comparison. Cash is the wallet's on-chain USDC balance (never simulated).
 */
export default function LiveBook({ book }: { book: LivePortfolioResponse }) {
  if (!book.enabled) return null
  const positions = book.positions ?? []
  return (
    <div className="panel border-term-red">
      <div className="panel-title flex items-center gap-2">
        <span className="text-term-red font-bold">● LIVE BOOK — REAL MONEY</span>
        <span className="text-term-dim font-normal normal-case">
          wallet {book.wallet ? `${book.wallet.slice(0, 4)}…${book.wallet.slice(-4)}` : '—'}
          {book.manual_confirmation ? ' · manual confirmation ON' : ' · autonomous'}
        </span>
      </div>
      <div className="grid grid-cols-5 gap-2 text-xs">
        <div>
          <div className="text-term-dim">equity</div>
          <div className="text-base font-bold">{usd(book.equity_usd)}</div>
        </div>
        <div>
          <div className="text-term-dim">cash (on-chain USDC)</div>
          <div>{usd(book.cash_usd)}</div>
        </div>
        <div>
          <div className="text-term-dim">realized p&l</div>
          <div className={pnlClass(book.realized_pnl_usd)}>
            {usd(book.realized_pnl_usd)}
          </div>
        </div>
        <div>
          <div className="text-term-dim">unrealized p&l</div>
          <div className={pnlClass(book.unrealized_pnl_usd)}>
            {usd(book.unrealized_pnl_usd)}
          </div>
        </div>
        <div>
          <div className="text-term-dim">SOL (fees)</div>
          <div>{book.sol_balance !== null && book.sol_balance !== undefined
            ? book.sol_balance.toFixed(4) : '—'}</div>
        </div>
      </div>
      {positions.length > 0 && (
        <table className="w-full text-xs mt-2">
          <thead>
            <tr className="text-term-dim text-left">
              <th className="py-1">token</th>
              <th>cost</th>
              <th>mark</th>
              <th>value</th>
              <th>uP&L</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr key={p.mint_address} className="border-t border-term-border">
                <td className="py-1 font-bold">{p.symbol}</td>
                <td>{usd(p.cost_usd)}</td>
                <td>{p.current_price_usd !== null
                  ? `$${p.current_price_usd.toPrecision(4)}` : '—'}</td>
                <td>{usd(p.value_usd)}</td>
                <td className={pnlClass(p.unrealized_pnl_usd)}>
                  {usd(p.unrealized_pnl_usd)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {positions.length === 0 && (
        <div className="text-term-dim text-xs mt-2">
          no open live positions — deployed today: {usd(book.deployed_today_usd)}
        </div>
      )}
    </div>
  )
}
