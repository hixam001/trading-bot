import type { FeedFilter, FunnelResponse, FunnelSnapshotsResponse } from '../types'
import { Empty, Panel, Spark } from './ui'

/**
 * Refusal funnel (§63, extended in §64) — selectivity at a glance:
 * candidates seen -> gate passed -> model approved -> filled. Bespoke divs
 * over the server counts only: no chart library (§5), no invented data;
 * unknown stages render "—" (DESIGN.md §2), never 0.
 *
 * §64.3 — every stage is now a DRILL-DOWN entry point into the rows behind the
 * number (the dashboard-practice principle: a metric that raises a question
 * should be able to answer it). `filled` opens the journal filtered to bound
 * commits; the refusal/approval stages open the decisions tape filtered to the
 * exact same classification the counts use.
 *
 * §64.2 — when the backend has persisted ≥2 snapshots, the refusal rate is
 * trended with the shared <Spark>: the question worth answering is whether the
 * model's refusal discipline is holding or drifting down. Under 2 stored
 * points there is nothing to trend, and the panel says so instead of drawing
 * a line through a single dot.
 */
export default function RefusalFunnel({
  funnel,
  snapshots,
  onDrill,
  onJournal,
}: {
  funnel: FunnelResponse
  snapshots?: FunnelSnapshotsResponse | null
  onDrill?: (stage: FeedFilter) => void
  /** §64.3: `filled` is the one stage whose rows live in the journal (bound
   *  commits), not the tape — the audit asked specifically for that jump. */
  onJournal?: () => void
}) {
  const stages: {
    key: string
    label: string
    n: number
    note: string
    drill: FeedFilter
    hint: string
  }[] = [
    {
      key: 'seen',
      label: 'candidates seen',
      n: funnel.candidates_seen,
      note: 'window',
      drill: 'all',
      hint: 'open the tape',
    },
    {
      key: 'gate_refused',
      label: 'gate refused',
      n: funnel.gate_refused,
      note: 'rule failures',
      drill: 'gate_refused',
      hint: 'open the refused rows',
    },
    {
      key: 'gate_passed',
      label: 'gate passed',
      n: funnel.gate_passed,
      note: `${funnel.model_refused} of them refused by the model`,
      drill: 'gate_passed',
      hint: 'open the gate-passers',
    },
    {
      key: 'model_refused',
      label: 'model refused',
      n: funnel.model_refused,
      note: 'gate passed, model declined',
      drill: 'refused',
      hint: 'open model refusals',
    },
    {
      key: 'model_approved',
      label: 'model approved',
      n: funnel.model_approved,
      note: `${funnel.model_refused} refusals`,
      drill: 'approved',
      hint: 'open the approved rows',
    },
    {
      key: 'filled',
      label: 'filled',
      n: funnel.filled,
      note: 'sealed commits with a fill',
      drill: 'all',
      hint: 'open the journal, filtered to bound',
    },
  ]
  const base = funnel.candidates_seen
  const rate = funnel.model_refusal_rate_of_gate_passers
  // Verbatim stored rates, oldest first — the trend series. Nulls (no
  // denominator) are dropped from the SHAPE only; the count of stored points
  // is never inflated to imply history that isn't there.
  const rates = (snapshots?.snapshots ?? [])
    .map((s) => s.model_refusal_rate)
    .filter((r): r is number => r !== null)
  const canTrend = snapshots != null && snapshots.count >= 2 && rates.length >= 2
  const latest = snapshots?.snapshots?.[snapshots.snapshots.length - 1]
  return (
    <Panel testId="refusal-funnel" title="Refusal funnel">
      {base === 0 && funnel.candidates_seen_total === 0 ? (
        <Empty>No candidates evaluated yet. The funnel fills as the engine cycles.</Empty>
      ) : (
        <div data-testid="funnel-stages">
          {stages.map((s, i) => (
            <div key={s.key} className="mb-2 last:mb-0">
              <button
                type="button"
                data-testid={`funnel-stage-${s.key}`}
                onClick={() => s.key === 'filled' ? onJournal?.() : onDrill?.(s.drill)}
                disabled={!(s.key === 'filled' ? onJournal : onDrill) || s.n === 0}
                title={s.n === 0 ? 'nothing in this stage yet' : s.hint}
                className="group w-full text-left disabled:cursor-default"
              >
                <div className="flex items-baseline justify-between gap-3 text-xs">
                  <span className="text-dim group-enabled:group-hover:text-live">
                    <span className="font-mono text-[10px] text-faint mr-1.5">{i + 1}.</span>
                    {s.label}
                    {onDrill && s.n > 0 && (
                      <span className="ml-1.5 text-[10px] text-faint group-hover:text-live">
                        ↗
                      </span>
                    )}
                  </span>
                  <span className="font-mono tnum font-semibold text-bright">{s.n}</span>
                </div>
                <div className="mt-1 h-1.5 w-full bg-raised rounded overflow-hidden">
                  <div
                    className="h-full bg-live"
                    style={{ width: base === 0 ? '0%' : `${(s.n / base) * 100}%` }}
                  />
                </div>
                <div className="mt-0.5 text-[10px] text-faint">{s.note}</div>
              </button>
            </div>
          ))}
          <div className="divider" />
          <div className="flex items-baseline justify-between gap-3 text-xs">
            <span className="text-dim">model refusal · share of gate-passers</span>
            <span className={rate === null ? 'font-mono tnum text-dim' : 'font-mono tnum text-warn'}>
              {rate === null ? '—' : `${(rate * 100).toFixed(1)}%`}
            </span>
          </div>
          <div className="mt-1 text-[10px] text-faint">
            {funnel.candidates_seen_total} candidates evaluated all-time ·
            classification identical to scripts/perf_report.py
          </div>

          {/* §64.2 — the persisted trend. Deliberately absent until the backend
              has ≥2 stored snapshots: a sparkline over one point would be
              inventing a trend (Stage 3.3 of the last audit was skipped for
              exactly this reason, and that judgement still holds). */}
          <div className="divider" />
          <div className="stat-label mb-1">refusal rate · persisted snapshots</div>
          {canTrend ? (
            <div data-testid="funnel-trend">
              <div className="flex items-center gap-3">
                <Spark
                  values={rates}
                  w={220}
                  h={40}
                  className="w-full max-w-[220px] h-10 text-warn"
                />
                <span className="font-mono text-[10px] text-faint tnum shrink-0">
                  {rates.length} points
                  {latest?.ts ? ` · latest ${latest.ts.slice(0, 16).replace('T', ' ')}` : ''}
                </span>
              </div>
              <div className="mt-1 text-[10px] text-faint">
                oldest → newest · each point computed by the engine over its own window
              </div>
            </div>
          ) : (
            <div className="text-[10.5px] text-faint leading-snug" data-testid="funnel-trend-empty">
              {snapshots == null
                ? 'no stored history yet — snapshots accumulate as the engine cycles'
                : `${snapshots.count} snapshot${snapshots.count === 1 ? '' : 's'} stored · ` +
                  'a trend needs at least 2. The engine writes one throttled row per cycle, ' +
                  'so this fills in on its own.'}
            </div>
          )}
        </div>
      )}
    </Panel>
  )
}

