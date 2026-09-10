import { useEffect, useState } from 'react'
import LiveFeed from './components/LiveFeed'
import LiveBook from './components/LiveBook'
import Holdings from './components/Holdings'
import Journal from './components/Journal'
import MarketRegimePanel from './components/MarketRegimePanel'
import SystemStatus from './components/SystemStatus'
import Performance from './components/Performance'
import { ErrorState, Skeleton, Spark } from './components/ui'
import { useApi } from './hooks/useApi'
import { useFeedSocket } from './hooks/useWebSocket'
import { usd } from './lib/format'
import type {
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
type Tab = (typeof TABS)[number]['id']

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
    <span className="font-mono text-faint text-xs tnum shrink-0 hidden sm:inline" data-testid="utc-clock">
      {h}:{m}:{s} UTC<span className="clock-cursor" aria-hidden="true">_</span>
    </span>
  )
}

/** Hero band stat — small uppercase mono label over a semibold value. */
function HeroStat({ label, value, cls = '' }: { label: string; value: string; cls?: string }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="font-mono text-[9.5px] tracking-[0.12em] text-faint">{label}</span>
      <span className={`font-mono tnum font-semibold text-[15px] leading-none ${cls}`}>{value}</span>
    </div>
  )
}

/** The hero band — equity headline, equity-curve sparkline, record stats. */
function Hero({
  equity,
  curve,
  winRate,
  profitFactor,
  drawdown,
  connected,
}: {
  equity: number | null | undefined
  curve: number[]
  winRate: number | null
  profitFactor: number | null
  drawdown: number | null
  connected: boolean
}) {
  return (
    <header
      data-testid="hero"
      className="hero flex items-center justify-between gap-5 flex-wrap px-4 sm:px-7 py-3.5 bg-surface border-b border-line"
    >
      <div className="flex items-center gap-6 xl:gap-8 flex-wrap">
        <div className="flex flex-col gap-0.5 shrink-0">
          <span className="font-mono font-semibold text-[12.5px] tracking-[0.04em] text-bright">
            trading-bot
          </span>
          <span className="font-mono text-[10px] text-faint">solana memecoin · signal console</span>
        </div>
        <div className="flex items-center gap-4">
          <div className="flex flex-col gap-1.5">
            <span className="font-mono text-[9.5px] tracking-[0.12em] text-faint">EQUITY</span>
            <span className="font-mono tnum font-semibold text-[30px] leading-none text-bright">
              {usd(equity)}
            </span>
          </div>
          <Spark values={curve} className="w-36 h-11 text-pass shrink-0" />
        </div>
        <div className="hidden md:flex items-center gap-6">
          <HeroStat
            label="WIN RATE"
            value={winRate === null ? '—' : `${(winRate * 100).toFixed(1)}%`}
          />
          <HeroStat
            label="PROFIT FACTOR"
            value={profitFactor === null ? '—' : profitFactor.toFixed(2)}
            cls={
              profitFactor === null
                ? ''
                : profitFactor >= 1
                  ? 'text-pass'
                  : 'text-fail'
            }
          />
          <HeroStat
            label="DRAWDOWN"
            value={drawdown === null ? '—' : `−${drawdown.toFixed(1)}%`}
          />
        </div>
      </div>
      <div className="flex items-center gap-3 flex-wrap">
        <span className={`badge ${connected ? 'badge-pass' : 'badge-fail'}`} data-testid="ws-state">
          {connected ? 'stream connected' : 'stream offline'}
        </span>
        <UtcClock />
        <span className="live-tag">
          <span className="live-dot" aria-hidden="true" />
          LIVE · real money
        </span>
      </div>
    </header>
  )
}

/** Loading placeholder panel (DESIGN.md §3.1). */
function LoadingPanel({ title, rows }: { title: string; rows: number }) {
  return (
    <div className="panel">
      <div className="panel-header">
        <h2 className="panel-title">{title}</h2>
      </div>
      <Skeleton rows={rows} />
    </div>
  )
}

/** Journal error panel (DESIGN.md §3.3). */
function JournalErrorPanel({ message }: { message: string }) {
  return (
    <div className="panel">
      <div className="panel-header">
        <h2 className="panel-title">journal · live order history</h2>
      </div>
      <ErrorState message={message} />
    </div>
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
      <Hero
        equity={liveBook.data?.equity_usd}
        curve={(stats.data?.equity_curve ?? []).map((p) => p.equity_usd)}
        winRate={stats.data?.win_rate ?? null}
        profitFactor={stats.data?.profit_factor ?? null}
        drawdown={stats.data?.max_drawdown_pct ?? null}
        connected={connected}
      />

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
                  <span className={`ml-auto badge ${connected ? 'badge-pass' : 'badge-fail'}`}>
                    {connected ? '● ws live' : '● ws offline'}
                  </span>
                </div>
                <LiveFeed events={events} freshId={freshId} />
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
                ) : null}
                {regimes.loading ? (
                  <LoadingPanel title="Market regime" rows={4} />
                ) : regimes.data ? (
                  <MarketRegimePanel regimes={regimes.data.regimes} />
                ) : null}
              </div>
            </div>
          )}

          {tab === 'holdings' && (
            <div className="p-4 xl:p-6 max-w-[1400px] mx-auto">
              {liveBook.loading ? (
                <LoadingPanel title="Holdings · live positions" rows={4} />
              ) : liveBook.data ? (
                <Holdings book={liveBook.data} />
              ) : null}
            </div>
          )}

          {tab === 'journal' && (
            <div className="p-4 xl:p-6 max-w-[1400px] mx-auto">
              {journal.loading ? (
                <LoadingPanel title="Journal · live order history" rows={5} />
              ) : journal.data ? (
                <Journal data={journal.data} />
              ) : journal.error ? (
                <JournalErrorPanel message={journal.error} />
              ) : null}
            </div>
          )}

          {tab === 'market' && (
            <div className="p-4 xl:p-6 max-w-[1100px] mx-auto">
              {regimes.loading ? (
                <LoadingPanel title="Market regime" rows={4} />
              ) : regimes.data ? (
                <MarketRegimePanel regimes={regimes.data.regimes} tall />
              ) : null}
            </div>
          )}

          {tab === 'system' && (
            <div className="p-4 xl:p-6 max-w-[1100px] mx-auto">
              {status.loading ? (
                <LoadingPanel title="System status" rows={4} />
              ) : status.data ? (
                <SystemStatus status={status.data} />
              ) : null}
            </div>
          )}
        </div>
      </main>

      {/* Statusline — the terminal sign-off: quiet truths, one strip. */}
      <footer className="statusline">
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