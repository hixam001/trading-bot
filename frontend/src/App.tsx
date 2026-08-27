import LiveFeed from './components/LiveFeed'
import LiveBook from './components/LiveBook'
import MarketRegimePanel from './components/MarketRegimePanel'
import SystemStatus from './components/SystemStatus'
import { useApi } from './hooks/useApi'
import { useFeedSocket } from './hooks/useWebSocket'
import type { LivePortfolioResponse } from './types'

export default function App() {
  // Live decision feed via WebSocket; slower panels poll. The dashboard is
  // live-trading-first: the real-wallet book leads, the decision feed is the
  // main content, and market regime + system status sit in the sidebar. The
  // paper-book panels (stats / holdings / journal) and the promotion gate were
  // removed when the system went live.
  const { events, connected } = useFeedSocket()
  const liveBook = useApi<LivePortfolioResponse>('/api/live/portfolio', 5000)
  const regimes = useApi<{ regimes: Parameters<typeof MarketRegimePanel>[0]['regimes'] }>(
    '/api/market-regime?limit=30', 15000)
  const status = useApi<Parameters<typeof SystemStatus>[0]['status']>('/api/system-status', 15000)

  const offline =
    liveBook.error && status.error ? (
      <div className="panel border-term-red text-term-red text-xs m-2">
        API unreachable ({status.error}) — panels will recover automatically when
        the backend comes back. Not a crash; retrying in the background.
      </div>
    ) : null

  return (
    <div className="min-h-screen flex flex-col">
      <div className="flex items-center gap-4 px-4 py-2 border-b border-term-border">
        <span className="font-bold text-term-blue">trading-bot</span>
        <span className="text-term-dim text-xs">live trading dashboard</span>
      </div>

      {offline}

      <main className="p-3 grid grid-cols-1 xl:grid-cols-3 gap-3">
        <div className="xl:col-span-2 space-y-3 min-w-0">
          {liveBook.data?.enabled && <LiveBook book={liveBook.data} />}

          <LiveFeed events={events} connected={connected} />
        </div>

        <div className="space-y-3">
          {regimes.data && <MarketRegimePanel regimes={regimes.data.regimes} />}
          {status.data && <SystemStatus status={status.data} />}
        </div>
      </main>
    </div>
  )
}
