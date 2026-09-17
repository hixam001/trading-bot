import { useEffect, useState } from 'react'
import { Spark } from './ui'
import { usd } from '../lib/format'

/**
 * The hero band (§59, extracted from App.tsx in §64.4): equity headline, the
 * equity-curve sparkline, the track-record stats and the stream/live chrome.
 * Extracted verbatim — App.tsx keeps routing and state wiring only; the shell
 * was the file most likely to become the one nobody wants to touch (§2.3).
 */

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

export default function Hero({
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