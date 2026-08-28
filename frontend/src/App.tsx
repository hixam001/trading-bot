import { useState } from 'react'
import LiveFeed from './components/LiveFeed'
import LiveBook from './components/LiveBook'
import Holdings from './components/Holdings'
import Journal from './components/Journal'
import MarketRegimePanel from './components/MarketRegimePanel'
import SystemStatus from './components/SystemStatus'
import { Skeleton } from './components/ui'
import { useApi } from './hooks/useApi'
import { useFeedSocket } from './hooks/useWebSocket'
import type {
  LiveExecutionsResponse,
  LivePortfolioResponse,
  RegimeRow,
  SystemStatusResponse,
} from './types'

/**
 * App shell — live-trading-first terminal with three pages:
 *   dashboard — live book + decision feed + regime + system status
 *   holdings  — the open live positions in detail
 *   journal   — every sealed order decision + the confirmed money ledger
 * The paper-book panels are gone for good; every page is live data only.
 * Every panel implements the five required states (DESIGN.md §3).
 */

const TABS = ['dashboard', 'holdings', 'journal'] as const
type Tab = (typeof TABS)[number]

export default function App() {
  const [tab, setTab] = useState<Tab>('dashboard')
  const { events, connected } = useFeedSocket()
  const liveBook = useApi<LivePortfolioResponse>('/api/live/portfolio', 5000)
  const journal = useApi<LiveExecutionsResponse>('/api/live/executions', 10000)
  const regimes = useApi<{ regimes: RegimeRow[] }>('/api/market-regime?limit=30', 15000)
  const status = useApi<SystemStatusResponse>('/api/system-status', 15000)

  // Global offline banner (DESIGN.md §3.4): only when BOTH primary feeds fail.
  // Panels keep their last data and recover automatically.
  const offline = liveBook.error && status.error

  return (
    <div className="min-h-screen flex flex-col">
      {/* Header — identity + live money warning + socket state. */}
      <header className="flex items-center gap-3 px-4 py-2.5 border-b border-line bg-panel">
        <span className="font-sans font-bold text-bright tracking-tight">trading-bot</span>
        <span className="badge badge-neg">● LIVE · real money</span>
        <span className="text-dim text-xs hidden sm:inline">
          autonomous Solana memecoin trading
        </span>
        <span
          className={`ml-auto badge ${connected ? 'badge-pos' : 'badge-neg'}`}
          data-testid="ws-state"
        >
          {connected ? 'stream connected' : 'stream offline'}
        </span>
      </header>

      {/* Page tabs — plain buttons: keyboard + screen-reader friendly. */}
      <nav className="flex gap-1 px-3 pt-2" aria-label="pages">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            aria-current={tab === t ? 'page' : undefined}
            data-testid={`tab-${t}`}
            className={`px-3 py-1.5 rounded text-xs font-semibold border ${
              tab === t
                ? 'bg-raised border-line text-bright'
                : 'border-transparent text-dim hover:text-body hover:bg-raised/50'
            }`}
          >
            {t}
          </button>
        ))}
      </nav>

      {offline && (
        <div
          className="mx-3 mt-3 border border-neg/60 rounded p-2 text-xs text-neg"
          role="alert"
          data-testid="offline-banner"
        >
          API unreachable ({status.error}) — panels keep their last data and will
          recover automatically when the backend returns. Not a crash; retrying in
          the background.
        </div>
      )}

      <main className="p-3 flex-1">
        {tab === 'dashboard' && (
          <div className="grid grid-cols-1 xl:grid-cols-3 gap-3 items-start">
            {/* Main column: live book + decision feed. */}
            <div className="xl:col-span-2 space-y-3 min-w-0">
              {liveBook.loading ? (
                <div className="panel">
                  <div className="panel-title mb-2">Live book — real money</div>
                  <Skeleton rows={3} />
                </div>
              ) : liveBook.data?.enabled ? (
                <LiveBook book={liveBook.data} />
              ) : null}

              <LiveFeed events={events} connected={connected} />
            </div>

            {/* Sidebar: regime + system status. */}
            <div className="space-y-3 min-w-0">
              {regimes.loading ? (
                <div className="panel">
                  <div className="panel-title mb-2">Market regime</div>
                  <Skeleton rows={4} />
                </div>
              ) : regimes.data ? (
                <MarketRegimePanel regimes={regimes.data.regimes} />
              ) : null}

              {status.loading ? (
                <div className="panel">
                  <div className="panel-title mb-2">System status</div>
                  <Skeleton rows={4} />
                </div>
              ) : status.data ? (
                <SystemStatus status={status.data} />
              ) : null}
            </div>
          </div>
        )}

        {tab === 'holdings' &&
          (liveBook.loading ? (
            <div className="panel">
              <div className="panel-title mb-2">Holdings — live positions</div>
              <Skeleton rows={4} />
            </div>
          ) : liveBook.data ? (
            <Holdings book={liveBook.data} />
          ) : null)}

        {tab === 'journal' &&
          (journal.loading ? (
            <div className="panel">
              <div className="panel-title mb-2">Journal — live order history</div>
              <Skeleton rows={5} />
            </div>
          ) : journal.data ? (
            <Journal data={journal.data} />
          ) : journal.error ? (
            <div className="panel">
              <div className="panel-title mb-2">Journal — live order history</div>
              <div className="border border-neg/50 rounded p-2 text-xs text-neg">
                {journal.error} — retrying automatically.
              </div>
            </div>
          ) : null)}
      </main>
    </div>
  )
}

