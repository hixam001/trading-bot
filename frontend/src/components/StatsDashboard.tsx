import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'
import type { StatsResponse } from '../types'

export default function StatsDashboard({ stats }: { stats: StatsResponse }) {
  return (
    <div className="panel">
      <div className="panel-title">portfolio stats</div>
      <div className="grid grid-cols-3 gap-2 text-xs mb-3">
        <div>
          <div className="text-term-dim">equity</div>
          <div className="text-base font-bold">
            ${stats.equity_usd.toFixed(2)}
          </div>
        </div>
        <div>
          <div className="text-term-dim">cash</div>
          <div>${stats.cash_usd.toFixed(2)}</div>
        </div>
        <div>
          <div className="text-term-dim">total p&l</div>
          <div className={stats.total_pnl_usd >= 0 ? 'text-term-green' : 'text-term-red'}>
            ${stats.total_pnl_usd.toFixed(2)}
          </div>
        </div>
        <div>
          <div className="text-term-dim">win rate</div>
          <div>{stats.win_rate !== null ? `${(stats.win_rate * 100).toFixed(1)}%` : '—'}</div>
        </div>
        <div>
          <div className="text-term-dim">profit factor</div>
          <div>{stats.profit_factor ?? '—'}</div>
        </div>
        <div>
          <div className="text-term-dim">max drawdown</div>
          <div>{stats.max_drawdown_pct.toFixed(1)}%</div>
        </div>
      </div>
      <div className="text-xs text-term-dim mb-1">
        equity curve ({stats.closed_trades} closed trades)
      </div>
      <div style={{ width: '100%', height: 180 }}>
        <ResponsiveContainer>
          <LineChart data={stats.equity_curve}>
            <XAxis dataKey="closed_at" hide />
            <YAxis domain={['auto', 'auto']} tick={{ fontSize: 10 }} width={50} />
            <Tooltip
              contentStyle={{ background: '#11161f', border: '1px solid #1e2633', fontSize: 12 }}
            />
            <Line dataKey="equity_usd" stroke="#58a6ff" dot={false} strokeWidth={2} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}
