import { useEffect, useState } from 'react'
import LiveFeed from './components/LiveFeed'
import LiveBook from './components/LiveBook'
import Holdings from './components/Holdings'
import Journal from './components/Journal'
import MarketRegimePanel from './components/MarketRegimePanel'
import SystemStatus from './components/SystemStatus'
import Performance from './components/Performance'
import { Skeleton } from './components/ui'
import { useApi } from './hooks/useApi'
import { useFeedSocket } from './hooks/useWebSocket'
import type {
  LiveExecutionsResponse,
  LivePortfolioResponse,
  RegimeRow,
  StatsResponse,
  SystemStatusResponse,
} from './types'

/**
 * App shell — live-trading terminal with three pages:
 *   dashboard — live book + performance + decision feed + regime + status
 *   holdings  — the open live positions in detail
 *   journal   — every sealed order decision + the confirmed money ledger
 * §58 PHOSPHOR AMBER world: the shell reads as a terminal prompt — wordmark
 * with a blinking block cursor, a live UTC clock, gold structural chrome.
 * Every panel implements the five required states (DESIGN.md §3).
 */

const TABS = ['dashboard', 'holdings', 'journal'] as const
type Tab = (typeof TABS)[number]

/** Live UTC clock (HH:MM:SS) — the terminal's heartbeat; 1s tick. */
function UtcClock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(id)
  }, [])
  const h = String(now.getUTCHours()).padStart(2, '0')
  const m = String(now.getUTCMinutes()).padStart(2, '0')
  const s = String(now.getUTCSeconds()).padStart(2, '0')
  return (
    <span className="text-faint text-xs tnum shrink-0 hidden sm:inline" data-testid="utc-clock">
      {h}:{m}:{s} UTC
    </span>
  )
}

export default function App() {
  const [tab, setTab] = useState<Tab>('dashboard')
  const { events, connected, freshId } = useFeedSocket()
  const liveBook = useApi<LivePortfolioResponse>('/api/live/portfolio', 5000)
  const journal = useApi<LiveExecutionsResponse>('/api/live/executions', 10000)
  const regimes = useApi<{ regimes: RegimeRow[] }>('/api/market-regime?limit=30', 15000)
  const status = useApi<SystemStatusResponse>('/api/system-status', 15000)
  const stats = useApi<StatsResponse>('/api/stats', 15000)

  // Global offline banner (DESIGN.md §3.4): only when BOTH primary feeds fail.
  // Panels keep their last data and recover automatically.
  const offline = liveBook.error && status.error

  return (
    <div className="min-h-screen flex flex-col">
      {/* Header — identity + live money warning + socket state.
       * §58: the wordmark is a shell prompt (blinking block cursor). */}
      <header className="flex items-center gap-3 px-4 py-2 border-b border-line bg-panel">
        <span className="font-mono font-bold text-bright tracking-tight text-sm">
          trading-bot<span className="prompt-cursor" aria-hidden="true" />
        </span>
        <span className="badge badge-neg">● LIVE · real money</span>
        <span className="text-dim text-xs hidden lg:inline">
          autonomous Solana memecoin trading
        </span>
        <span
          className={`ml-auto badge ${connected ? 'badge-pos' : 'badge-neg'}`}
          data-testid="ws-state"
        >
          {connected ? 'stream connected' : 'stream offline'}
        </span>
        <UtcClock />
      </header>

      {/* Page tabs — plain buttons (keyboard + screen-reader friendly). The
       * active tab carries the gold rail; passive tabs sit quiet. */}
      <nav
        className="flex gap-1 px-3 pt-2 border-b border-line"
        aria-label="pages"
      >
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            aria-current={tab === t ? 'page' : undefined}
            data-testid={`tab-${t}`}
            className={`relative px-3 py-1.5 rounded-t text-xs font-semibold border-b-2 -mb-px transition-colors duration-200 ease-out-expo ${
              tab === t
                ? 'bg-raised border-gold text-bright'
                : 'border-transparent text-dim hover:text-body hover:bg-raised/50'
            }`}
          >
            {t}
          </button>
        ))}
      </nav>

      {offline && (
        <div
          className="mx-3 mt-3 border border-neg/60 rounded p-2 text-xs text-neg animate-fade-rise"
          role="alert"
          data-testid="offline-banner"
        >
          API unreachable ({status.error}). Panels keep their last data and will
          recover automatically when the backend returns. Not a crash; retrying in
          the background.
        </div>
      )}

      <main className="p-3 flex-1">
        {/* §58: each page arrival is one quiet fade-rise (240ms, expo ease,
         * content visible from frame one) — routine, not an entrance show. */}
        <div key={tab} className="animate-fade-rise">
        {tab === 'dashboard' && (
          <div className="grid grid-cols-1 xl:grid-cols-3 gap-3 items-start">
            {/* Main column: live book + performance + decision feed. */}
            <div className="xl:col-span-2 space-y-3 min-w-0">
              {liveBook.loading ? (
                <div className="panel">
                  <div className="panel-title mb-2">Live book · real money</div>
                  <Skeleton rows={3} />
                </div>
              ) : liveBook.data?.enabled ? (
                <LiveBook book={liveBook.data} />
              ) : null}

              {stats.loading ? (
                <div className="panel">
                  <div className="panel-title mb-2">Performance</div>
                  <Skeleton rows={2} />
                </div>
              ) : stats.data ? (
                <Performance stats={stats.data} />
              ) : null}

              <LiveFeed events={events} connected={connected} freshId={freshId} />
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
              <div className="panel-title mb-2">Holdings · live positions</div>
              <Skeleton rows={4} />
            </div>
          ) : liveBook.data ? (
            <Holdings book={liveBook.data} />
          ) : null)}

        {tab === 'journal' &&
          (journal.loading ? (
            <div className="panel">
              <div className="panel-title mb-2">Journal · live order history</div>
              <Skeleton rows={5} />
            </div>
          ) : journal.data ? (
            <Journal data={journal.data} />
          ) : journal.error ? (
            <div className="panel">
              <div className="panel-title mb-2">Journal · live order history</div>
              <div className="border border-neg/50 rounded p-2 text-xs text-neg">
                {journal.error}. Retrying automatically.
              </div>
            </div>
          ) : null)}
        </div>
      </main>

      {/* §58 footer — the terminal sign-off: quiet truths, one line. */}
      <footer className="px-4 py-2 border-t border-line text-faint text-xs flex flex-wrap gap-x-4 gap-y-1">
        <span>single book · live engine</span>
        <span className="hidden md:inline">
          every figure verbatim from the backend · no client-side math
        </span>
        <span className="ml-auto hidden sm:inline">
          read-only surface · no endpoint can move money
        </span>
      </footer>
    </div>
  )
}

