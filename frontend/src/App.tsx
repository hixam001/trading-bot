import { useState } from 'react'
import LiveFeed from './components/LiveFeed'
import LiveBook from './components/LiveBook'
import Holdings from './components/Holdings'
import Journal from './components/Journal'
import MarketRegimePanel from './components/MarketRegimePanel'
import SystemStatus from './components/SystemStatus'
import CommandPalette, { type PaletteJournalFilter } from './components/CommandPalette'
import Performance from './components/Performance'
import RefusalFunnel from './components/RefusalFunnel'
import { ErrorPanel, LoadingPanel } from './components/ui'
import Hero from './components/Hero'
import AlertStrip from './components/AlertStrip'
import { useApi } from './hooks/useApi'
import { useFeedSocket } from './hooks/useWebSocket'
import type {
  Tab,
  FeedFilter,
  SafetyResponse,
  FunnelSnapshotsResponse,
  FunnelResponse,
  LiveExecutionsResponse,
  LivePortfolioResponse,
  RegimeRow,
  StatsResponse,
  SystemStatusResponse,
} from './types'

/**
 * App shell — the SIGNAL terminal (§59): a full-height console with a hero
 * band (equity headline + equity-curve sparkline + track-record stats), five
 * views (live / holdings / journal / market / system) and an engine
 * statusline. The live view is mission control: three independently scrolled
 * columns — decisions tape, live positions, book-health stack.
 * Every panel implements the five required states (DESIGN.md §3).
 */

const TABS = [
  { id: 'dashboard', label: 'live' },
  { id: 'holdings', label: 'holdings' },
  { id: 'journal', label: 'journal' },
  { id: 'market', label: 'market' },
  { id: 'system', label: 'system' },
] as const

export default function App() {
  const [tab, setTab] = useState<Tab>('dashboard')
  const { events, connected, freshId } = useFeedSocket()
  const liveBook = useApi<LivePortfolioResponse>('/api/live/portfolio', 5000)
  const journal = useApi<LiveExecutionsResponse>('/api/live/executions', 10000)
  const regimes = useApi<{ regimes: RegimeRow[] }>('/api/market-regime?limit=30', 15000)
  const status = useApi<SystemStatusResponse>('/api/system-status', 15000)
  const stats = useApi<StatsResponse>('/api/stats', 15000)
  const funnel = useApi<FunnelResponse>('/api/funnel', 15000)
  const safety = useApi<SafetyResponse>('/api/safety', 15000)
  const snapshots = useApi<FunnelSnapshotsResponse>('/api/funnel/snapshots?limit=300', 60000)
  const [feedFilter, setFeedFilter] = useState<FeedFilter>('all')

  // §63 operator upgrades: read-only command palette state. The palette
  // navigates, filters the journal and jumps to a position; it never writes.
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [journalFilter, setJournalFilter] = useState<PaletteJournalFilter>({
    status: 'all',
    query: '',
  })
  const [focusMint, setFocusMint] = useState<string | null>(null)

  // Global offline banner (DESIGN.md §3.4): only when BOTH primary feeds fail.
  // Panels keep their last data and recover automatically.
  const offline = liveBook.error && status.error

  return (
    <div className="min-h-screen flex flex-col">
      <Hero
        equity={liveBook.data?.equity_usd}
        curve={(stats.data?.equity_curve ?? []).map((p) => p.equity_usd)}
        winRate={stats.data?.win_rate ?? null}
        profitFactor={stats.data?.profit_factor ?? null}
        drawdown={stats.data?.max_drawdown_pct ?? null}
        connected={connected}
      />

      <AlertStrip safety={safety.data} error={safety.error} />

      {/* View tabs — the active view carries the signal underline. */}
      <nav className="tabs" aria-label="views">
        {TABS.map((t) => (
          <button
            key={t.id}
            data-testid={`tab-${t.id}`}
            className={`tab ${tab === t.id ? 'active' : ''}`}
            aria-current={tab === t.id ? 'page' : undefined}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      {offline && (
        <div
          data-testid="offline-banner"
          className="mx-4 mt-3 border border-fail/40 bg-fail/10 rounded px-3.5 py-2.5 text-xs text-fail"
        >
          API unreachable · both primary feeds failed. Panels keep their last
          data and retry automatically.
        </div>
      )}

      <main className={`flex-1 min-h-0 overflow-y-auto ${tab === 'dashboard' ? 'xl:overflow-hidden' : ''}`}>
        <div key={tab} className="animate-fade-rise h-full">
          {tab === 'dashboard' && (
            /* Mission control — three independently scrolled columns on xl. */
            <div className="grid grid-cols-1 xl:grid-cols-[1.4fr_1fr_0.9fr] xl:h-full xl:min-h-0">
              {/* Col 1 — the decisions tape (internally scroll-bounded). */}
              <div className="min-w-0 min-h-0 flex flex-col xl:border-r xl:border-line">
                <div className="sticky top-0 z-10 flex items-center gap-2 bg-base border-b border-line px-4 py-2.5">
                  <h2 className="panel-title">decisions</h2>
                  <span className="font-mono text-[10px] text-faint tnum">{events.length}</span>
                  <span className="font-mono text-[10px] text-faint hidden xl:inline">
                    j/k move · enter expand · esc collapse
                  </span>
                  <span className={`ml-auto badge ${connected ? 'badge-pass' : 'badge-fail'}`}>
                    {connected ? '● ws live' : '● ws offline'}
                  </span>
                </div>
                <LiveFeed events={events} freshId={freshId} filter={feedFilter} onClearFilter={() => setFeedFilter('all')} />
              </div>

              {/* Col 2 — the live book's open positions. */}
              <div className="min-w-0 min-h-0 flex flex-col xl:border-r xl:border-line">
                {liveBook.loading ? (
                  <div className="m-3">
                    <LoadingPanel title="positions" rows={3} />
                  </div>
                ) : liveBook.data?.enabled ? (
                  <LiveBook book={liveBook.data} />
                ) : (
                  <div className="m-3">
                    <div className="panel">
                      <div className="panel-header">
                        <h2 className="panel-title">positions</h2>
                      </div>
                      <div className="empty">
                        Live book not available:{' '}
                        {liveBook.data?.reason ?? 'waiting for the first poll'}.
                      </div>
                    </div>
                  </div>
                )}
              </div>

              {/* Col 3 — book-health stack: track record + regime. */}
              <div className="min-w-0 min-h-0 xl:overflow-y-auto p-3 space-y-3">
                {stats.loading ? (
                  <LoadingPanel title="Performance" rows={2} />
                ) : stats.data ? (
                  <Performance stats={stats.data} />
                ) : stats.error ? (
                  <ErrorPanel title="Performance" message={stats.error} />
                ) : null}
                {regimes.loading ? (
                  <LoadingPanel title="Market regime" rows={4} />
                ) : regimes.data ? (
                  <MarketRegimePanel regimes={regimes.data.regimes} />
                ) : regimes.error ? (
                  <ErrorPanel title="Market regime" message={regimes.error} />
                ) : null}
              </div>
            </div>
          )}

          {tab === 'holdings' && (
            <div className="p-4 xl:p-6 max-w-[1400px] mx-auto">
              {liveBook.loading ? (
                <LoadingPanel title="Holdings · live positions" rows={4} />
              ) : liveBook.data ? (
                <Holdings book={liveBook.data} focusMint={focusMint} />
              ) : liveBook.error ? (
                <ErrorPanel title="Holdings · live positions" message={liveBook.error} />
              ) : null}
            </div>
          )}

          {tab === 'journal' && (
            <div className="p-4 xl:p-6 max-w-[1400px] mx-auto">
              {journal.loading ? (
                <LoadingPanel title="Journal · live order history" rows={5} />
              ) : journal.data ? (
                <Journal
                  data={journal.data}
                  filter={journalFilter}
                  onClearFilter={() => setJournalFilter({ status: 'all', query: '' })}
                />
              ) : journal.error ? (
                <ErrorPanel title="Journal · live order history" message={journal.error} />
              ) : null}
            </div>
          )}

          {tab === 'market' && (
            <div className="p-4 xl:p-6 max-w-[1100px] mx-auto">
              {regimes.loading ? (
                <LoadingPanel title="Market regime" rows={4} />
              ) : regimes.data ? (
                <MarketRegimePanel regimes={regimes.data.regimes} tall />
              ) : regimes.error ? (
                <ErrorPanel title="Market regime" message={regimes.error} />
              ) : null}
            </div>
          )}

          {tab === 'system' && (
            <div className="p-4 xl:p-6 max-w-[1100px] mx-auto">
              {status.loading ? (
                <LoadingPanel title="System status" rows={4} />
              ) : status.data ? (
                <SystemStatus status={status.data} safety={safety.data} />
              ) : status.error ? (
                <ErrorPanel title="System status" message={status.error} />
              ) : null}
              {/* §63: the refusal funnel — selectivity at a glance, server-computed. */}
              <div className="mt-3">
                {funnel.loading ? (
                  <LoadingPanel title="Refusal funnel" rows={3} />
                ) : funnel.data ? (
                  <RefusalFunnel
                    funnel={funnel.data}
                    snapshots={snapshots.data}
                    onDrill={(stage) => { setFeedFilter(stage); setTab('dashboard') }}
                    onJournal={() => { setJournalFilter({ status: 'bound', query: '' }); setTab('journal') }}
                  />
                ) : funnel.error ? (
                  <ErrorPanel title="Refusal funnel" message={funnel.error} />
                ) : null}
              </div>
            </div>
          )}
        </div>
      </main>

      {/* Statusline — the terminal sign-off: quiet truths, one strip. */}
      <footer className="statusline">
        <span>single book · live engine</span>
        <span className="hidden lg:inline">⌘K · command palette</span>
        <span className="hidden md:inline">
          every figure verbatim from the backend · no client-side math
        </span>
        <span className="ml-auto hidden sm:inline">
          read-only surface · no endpoint can move money
        </span>
      </footer>

      {/* §63: read-only command palette (⌘K / Ctrl-K) — navigation, journal
          filtering and jump-to-position; no state writes, ever. */}
      <CommandPalette
        open={paletteOpen}
        onOpen={() => setPaletteOpen(true)}
        onClose={() => setPaletteOpen(false)}
        onGo={setTab}
        onJournal={(f) => {
          setJournalFilter(f)
          setTab('journal')
        }}
        onPosition={(mint) => {
          setFocusMint(mint)
          setTab('holdings')
        }}
        positions={liveBook.data?.positions ?? []}
      />
    </div>
  )
}