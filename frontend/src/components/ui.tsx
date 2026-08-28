import type { ReactNode } from 'react'

/**
 * Shared UI primitives — DESIGN.md §2/§3. Every data panel composes these so
 * the five required states (loading / empty / error / offline / stale) are
 * implemented consistently, never ad-hoc.
 */

export type Tone = 'pos' | 'neg' | 'warn' | 'info' | 'dim'

const badgeTone: Record<Tone, string> = {
  pos: 'badge-pos',
  neg: 'badge-neg',
  warn: 'badge-warn',
  info: 'badge-info',
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

/** Label-over-value stat block. */
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
    <div title={title}>
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
  return <div className="text-dim text-xs py-2">{children}</div>
}

/** Error state — what failed + automatic retry note (DESIGN.md §3.3). */
export function ErrorState({ message }: { message: string }) {
  return (
    <div className="border border-neg/50 rounded p-2 text-xs text-neg">
      {message} — retrying automatically.
    </div>
  )
}
