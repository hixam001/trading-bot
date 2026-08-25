import { useState } from 'react'
import LiveFeed from './components/LiveFeed'
import Holdings from './components/Holdings'
import TradeJournal from './components/TradeJournal'
import StatsDashboard from './components/StatsDashboard'
import MarketRegimePanel from './components/MarketRegimePanel'
import PromotionGate from './components/PromotionGate'
import SystemStatus from './components/SystemStatus'
import { useApi } from './hooks/useApi'
import { useFeedSocket } from './hooks/useWebSocket'
import type {
  HoldingRow,
  StatsResponse,
  TradeRow,
} from './types'

const TABS = ['feed', 'journal', 'gate'] as const
type Tab = (typeof TABS)[number]

export default function App() {
  const [tab, setTab] = useState<Tab>('feed')

  // Live feed via WebSocket; slower panels poll.
  const { events, connected } = useFeedSocket()
  const holdings = useApi<{ cash_usd: number; open_positions: HoldingRow[] }>(
    '/api/holdings', 5000)
  const stats = useApi<StatsResponse>('/api/stats', 10000)
  const journal = useApi<{ total: number; trades: TradeRow[] }>('/api/journal', 15000)
  const regimes = useApi<{ regimes: Parameters<typeof MarketRegimePanel>[0]['regimes'] }>(
    '/api/market-regime?limit=30', 15000)
  const gate = useApi<Parameters<typeof PromotionGate>[0]['data']>('/api/promotion-gate', 30000)
  const status = useApi<Parameters<typeof SystemStatus>[0]['status']>('/api/system-status', 15000)

  const offline =
    stats.error && holdings.error ? (
      <div className="panel border-term-red text-term-red text-xs m-2">
        API unreachable ({stats.error}) — panels will recover automatically when
        the backend comes back. Not a crash; retrying in the background.
      </div>
    ) : null

  return (
    <div className="min-h-screen flex flex-col">
      <div className="flex items-center gap-4 px-4 py-2 border-b border-term-border">
        <span className="font-bold text-term-blue">trading-bot</span>
        <nav className="flex gap-1 text-xs">
          {TABS.map((t) => (
            <button
              key={t}
              className={`px-3 py-1 rounded ${tab === t ? 'bg-term-panel border border-term-border text-term-blue' : 'text-term-dim hover:text-term-text'}`}
              onClick={() => setTab(t)}
            >
              {t}
            </button>
          ))}
        </nav>
      </div>

      {offline}

      <main className="p-3 grid grid-cols-1 xl:grid-cols-3 gap-3">
        <div className="xl:col-span-2 space-y-3 min-w-0">
          {stats.data && <StatsDashboard stats={stats.data} />}

          {tab === 'feed' && (
            <LiveFeed events={events} connected={connected} />
          )}

          {tab === 'journal' && journal.data && (
            <TradeJournal trades={journal.data.trades} total={journal.data.total} />
          )}

          {tab === 'gate' && gate.data && <PromotionGate data={gate.data} />}
        </div>

        <div className="space-y-3">
          {regimes.data && <MarketRegimePanel regimes={regimes.data.regimes} />}
          {status.data && <SystemStatus status={status.data} />}
          {holdings.data && (
            <Holdings
              holdings={holdings.data.open_positions}
              cash={holdings.data.cash_usd}
            />
          )}
        </div>
      </main>
    </div>
  )
}
