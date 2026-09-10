import { useState, type ReactNode } from 'react'

/**
 * Shared UI primitives — DESIGN.md §2/§3. Every data panel composes these so
 * the five required states (loading / empty / error / offline / stale) are
 * implemented consistently, never ad-hoc.
 */

export type Tone = 'pass' | 'fail' | 'warn' | 'live' | 'dim'

const badgeTone: Record<Tone, string> = {
  pass: 'badge-pass',
  fail: 'badge-fail',
  warn: 'badge-warn',
  live: 'badge-live',
  dim: 'badge-dim',
}

/** Panel with a header row (title left, optional status right). */
export function Panel({
  title,
  right,
  children,
  className = '',
  testId,
}: {
  title: string
  right?: ReactNode
  children: ReactNode
  className?: string
  testId?: string
}) {
  return (
    <section className={`panel ${className}`} data-testid={testId}>
      <div className="panel-header">
        <h2 className="panel-title">{title}</h2>
        {right}
      </div>
      {children}
    </section>
  )
}

/** Stat card — label-over-value on a nested surface. */
export function Stat({
  label,
  value,
  valueClass = '',
  title,
}: {
  label: string
  value: ReactNode
  valueClass?: string
  title?: string
}) {
  return (
    <div className="stat-card" title={title}>
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${valueClass}`}>{value}</div>
    </div>
  )
}

/** Semantic badge. Meaning is never carried by color alone — pass a word. */
export function Badge({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`badge ${badgeTone[tone]}`}>{children}</span>
}

/** Loading skeleton — N stacked bars (DESIGN.md §3.1). */
export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-2" role="status" aria-label="loading">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton h-4" style={{ width: `${90 - i * 12}%` }} />
      ))}
    </div>
  )
}

/** Explicit empty state — says what is empty and why (DESIGN.md §3.2). */
export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>
}

/** Error state — what failed + automatic retry note (DESIGN.md §3.3). */
export function ErrorState({ message }: { message: string }) {
  return (
    <div className="border border-fail/40 bg-fail/10 rounded p-2.5 text-xs text-fail">
      {message}. Retrying automatically.
    </div>
  )
}

/**
 * Click-to-copy text (§59: the COMPLETE contract address is always shown —
 * never truncated). The copy affordance announces "copied" by text, not
 * color alone (DESIGN.md §4).
 */
export function CopyText({
  value,
  className = '',
  title = 'click to copy',
}: {
  value: string
  className?: string
  title?: string
}) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      /* clipboard unavailable — the value stays fully visible/selectable */
    }
  }
  return (
    <span className="inline-flex items-baseline gap-1.5 min-w-0">
      <button
        type="button"
        onClick={copy}
        title={title}
        className={`text-left underline decoration-dotted underline-offset-2 transition-colors duration-150 ease-out-expo ${className}`}
      >
        {value}
      </button>
      {copied && (
        <span className="text-pass text-[10px] shrink-0" role="status">
          copied
        </span>
      )}
    </span>
  )
}

/**
 * Sparkline — a tiny SVG polyline over verbatim values (e.g. the equity
 * curve). No chart library (§5 ban holds); the shape visualizes data the
 * backend sent, it never invents numbers. Renders null under 2 points.
 */
export function Spark({
  values,
  w = 160,
  h = 44,
  className = '',
}: {
  values: number[]
  w?: number
  h?: number
  className?: string
}) {
  if (values.length < 2) return null
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const pts = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * w
      const y = h - 2 - ((v - min) / span) * (h - 4)
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="none"
      className={className}
      aria-hidden="true"
    >
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  )
}