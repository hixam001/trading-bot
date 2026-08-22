import type { HoldingRow } from '../types'

export default function Holdings({ holdings, cash }: { holdings: HoldingRow[]; cash?: number }) {
  return (
    <div className="panel">
      <div className="panel-title">
        holdings ({holdings.length} open{cash !== undefined ? ` · cash $${cash.toFixed(2)}` : ''})
      </div>
      {holdings.length === 0 ? (
        <div className="text-term-dim text-xs">No open positions.</div>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-term-dim text-left">
              <th className="pr-2">symbol</th>
              <th className="pr-2">size</th>
              <th className="pr-2">entry</th>
              <th className="pr-2">now</th>
              <th className="pr-2">unrl P&L</th>
            </tr>
          </thead>
          <tbody>
            {holdings.map((h) => (
              <tr key={h.trade_id} className="border-t border-term-border/40">
                <td className="pr-2 font-bold">{h.symbol}</td>
                <td className="pr-2">${h.position_size_usd.toFixed(2)}</td>
                <td className="pr-2">${h.entry_price_usd.toExponential(3)}</td>
                <td className="pr-2">
                  {h.current_price_usd !== null
                    ? `$${h.current_price_usd.toExponential(3)}`
                    : '—'}
                </td>
                <td
                  className={
                    h.unrealized_pnl_pct === null
                      ? 'text-term-dim'
                      : h.unrealized_pnl_pct >= 0
                        ? 'text-term-green'
                        : 'text-term-red'
                  }
                >
                  {h.unrealized_pnl_usd !== null
                    ? `$${h.unrealized_pnl_usd.toFixed(2)} (${h.unrealized_pnl_pct?.toFixed(1)}%)`
                    : 'price n/a'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
