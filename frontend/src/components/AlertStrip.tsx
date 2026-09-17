import type { SafetyResponse } from '../types'

/**
 * The persistent alert strip (§64.1b) — one line of symptoms that genuinely
 * demand operator attention, visible from EVERY view, directly under the
 * statusline's counterparts at the top of the shell.
 *
 * Alert-fatigue rule, deliberately followed: this is reserved for conditions
 * that require a human (kill switch / breaker engaged, LLM provider down,
 * data-provider chain exhausted, no fills while candidates flow). Everything
 * routine — a self-expiring break, a quiet window, a stale-but-serving
 * provider — stays out. An empty strip renders NOTHING and stays empty for
 * days: that is the feature working correctly, not wasted space.
 *
 * There is deliberately NO dismiss affordance. A dismissal would let a real
 * breaker trip be hidden with one click, and the whole point is that these
 * conditions cannot be silenced from the UI. They clear when the system
 * clears them. Read-only by construction (§64): no control here writes
 * anything, anywhere.
 */
export default function AlertStrip({ safety, error }: {
  safety: SafetyResponse | null
  error: string | null
}) {
  const alerts = [...(safety?.alerts ?? [])]
  if (error) alerts.unshift({
    id: 'safety_unavailable', severity: 'warn',
    message: safety
      ? 'Safety poll failed — displayed safety state is stale. Retrying automatically.'
      : 'Safety state unavailable — cannot confirm protection status. Retrying automatically.',
  })
  if (alerts.length === 0) return null

  return (
    <div
      data-testid="alert-strip"
      role="status"
      aria-live="polite"
      aria-label="operator alerts"
      className="flex items-start gap-2.5 flex-wrap px-4 sm:px-7 py-2 border-b border-line bg-raised"
    >
      <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.12em] text-faint pt-[1px] shrink-0">
        alerts
      </span>
      {alerts.map((a) => (
        <span
          key={a.id}
          data-testid={`alert-${a.id}`}
          className={`flex items-baseline gap-1.5 text-[11.5px] leading-snug min-w-0 ${
            a.severity === 'fail' ? 'text-fail' : 'text-warn'
          }`}
        >
          <span className="font-mono text-[10px] font-semibold uppercase shrink-0">
            [{a.severity === 'fail' ? 'FAIL' : 'WARN'}]
          </span>
          <span className="text-body">{a.message}</span>
        </span>
      ))}
    </div>
  )
}