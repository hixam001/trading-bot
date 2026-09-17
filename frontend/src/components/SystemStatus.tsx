import type { SafetyResponse, SystemStatusResponse } from '../types'
import { Badge, Empty, Panel } from './ui'
import { clock, epochClock, shortAddr, signedUsd, usd } from '../lib/format'

/**
 * System status (I8): the configured reasoning model (DeepSeek is main now —
 * ollama is no longer the brain), data backend, and per-provider call budgets.
 *
 * §64.1a adds the SAFETY section at the top, because the most consequential
 * mechanisms in a real-money system were previously invisible here: the kill
 * switch, the daily-loss circuit breaker and the mint blocklist. If the
 * breaker tripped at 3am the dashboard looked normal — the audit's single
 * sharpest frontend finding.
 *
 * Read-only by construction, like the command palette: nothing in this panel
 * (or the endpoint behind it) can engage, clear or write any of this state.
 * A kill-switch toggle here would put live system safety one misclick away.
 */
export default function SystemStatus({
  status,
  safety,
}: {
  status: SystemStatusResponse
  safety: SafetyResponse | null
}) {
  const reasoning = status.narration_mode
  return (
    <>
      {safety && <SafetySection safety={safety} />}
      <Panel testId="system-status" title="System status">
        <div className="text-xs space-y-1">
          <div className="flex justify-between items-center">
            <span className="text-dim">reasoning model</span>
            <Badge tone={reasoning === 'template' ? 'dim' : 'live'}>{reasoning}</Badge>
          </div>
          <div className="flex justify-between">
            <span className="text-dim">data backend</span>
            <span className="text-body">{status.data_backend}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-dim">tick interval</span>
            <span className="text-body tnum">{status.tick_interval_seconds}s</span>
          </div>
        </div>

      <div className="divider" />
      <div className="stat-label mb-1">provider calls today</div>
      {status.provider_calls_today.length === 0 ? (
        <Empty>No external calls yet.</Empty>
      ) : (
        <div className="text-xs space-y-1">
          {status.provider_calls_today.map((p) => (
            <div key={p.provider} className="flex justify-between items-center">
              <span className="text-body">{p.provider}</span>
              <span className="text-dim tnum">
                {p.call_count} calls · {p.error_count} err · {p.rate_limit_429_count}×429
              </span>
            </div>
          ))}
        </div>
      )}

      <div className="divider" />
      <div className="stat-label mb-1">recent LLM calls</div>
      {(status.llm_usage_recent ?? []).length === 0 ? (
        <Empty>No LLM calls recorded yet.</Empty>
      ) : (
        <div className="text-xs space-y-1 overflow-y-auto pr-1" style={{ maxHeight: '24vh' }}>
          {(status.llm_usage_recent ?? []).slice(0, 12).map((u) => (
            <div key={u.id} className="flex justify-between items-center gap-2">
              <span className="flex items-center gap-2 min-w-0">
                <span
                  className={`shrink-0 w-1.5 h-1.5 rounded-full ${
                    u.status === 'success' ? 'bg-pass' : 'bg-fail'
                  }`}
                  aria-label={u.status}
                />
                <span className="text-body truncate">{u.task}</span>
                <span className="text-faint shrink-0">{u.provider}</span>
              </span>
              <span className="text-faint shrink-0 tnum">
                {u.latency_ms !== null ? `${(u.latency_ms / 1000).toFixed(1)}s` : '—'} · {clock(u.ts)}
              </span>
            </div>
          ))}
        </div>
      )}
      </Panel>
    </>
  )
}

/** One label/value row of the safety section. */
function SafetyRow({
  label,
  tone,
  badge,
  value,
  note,
}: {
  label: string
  tone: 'pass' | 'fail' | 'warn' | 'dim'
  badge: string
  value?: string
  note?: string
}) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-dim shrink-0">{label}</span>
      <span className="flex items-baseline gap-2 min-w-0 justify-end">
        {value && <span className="font-mono tnum text-body truncate">{value}</span>}
        <Badge tone={tone}>{badge}</Badge>
      </span>
      {note && <span className="sr-only">{note}</span>}
    </div>
  )
}

/**
 * The §64.1a safety section — the state of the three mechanisms that can stop
 * the bot, rendered verbatim from /api/safety. Every badge carries a WORD
 * (DESIGN.md §4: meaning is never color alone), and an unreadable figure stays
 * `—` rather than 0 (null-never-zero).
 *
 * A tripped breaker IS the kill switch: they share one state file, shown here
 * as one truth in two rows so the auto-trip's `AUTO: ...` reason is legible
 * next to the money that caused it.
 */
function SafetySection({ safety }: { safety: SafetyResponse }) {
  const ks = safety.kill_switch
  const br = safety.daily_loss_breaker
  const bl = safety.blocklist
  const autoTripped = ks.reason.startsWith('AUTO:')
  return (
    <Panel
      testId="safety-state"
      title="Safety state"
      className="mb-3"
      right={
        <span className="font-mono text-[10px] text-faint" data-testid="safety-armed">
          {safety.armed ? 'armed · live trading enabled' : 'disarmed'}
        </span>
      }
    >
      <div className="text-xs space-y-1.5">
        <SafetyRow
          label="kill switch"
          tone={ks.engaged ? 'fail' : 'pass'}
          badge={ks.engaged ? 'ENGAGED' : 'clear'}
          note={ks.reason}
        />
        {ks.engaged && (
          <div className="text-[10.5px] text-fail leading-snug" data-testid="kill-switch-reason">
            {autoTripped ? 'tripped automatically' : 'halted'} · {ks.reason}
          </div>
        )}

        <SafetyRow
          label="daily-loss breaker"
          tone={br.engaged ? 'fail' : 'dim'}
          badge={br.engaged ? 'TRIPPED' : 'watching'}
          value={`${signedUsd(br.realized_pnl_today_usd)} today / ${usd(br.limit_usd)} limit`}
        />
        <div className="text-[10.5px] text-faint leading-snug" data-testid="breaker-headroom">
          {br.headroom_usd === null
            ? 'headroom unknown — the execution ledger is unreadable (not zero)'
            : `${usd(br.headroom_usd)} of further realized loss before the breaker trips`}
        </div>

        <SafetyRow
          label="blocklist"
          tone="dim"
          badge={bl.blocks === null ? 'unknown' : `${bl.blocks} blocked`}
          value={
            bl.blocks === null || bl.blocks === 0
              ? undefined
              : `${bl.auto} auto · ${bl.manual} manual`
          }
        />
        {bl.latest ? (
          <div className="text-[10.5px] text-faint leading-snug" data-testid="blocklist-latest">
            latest: {bl.latest.symbol || shortAddr(bl.latest.mint)} — {bl.latest.reason} (
            {bl.latest.kind}) · {clock(bl.latest.blocked_at)}
          </div>
        ) : (
          <div className="text-[10.5px] text-faint leading-snug">
            {bl.blocks === null ? 'blocklist unreadable — count unknown' : 'no mint has been blocked yet'}
          </div>
        )}

        <SafetyRow
          label="engine break"
          tone={safety.break.on_break ? 'warn' : 'dim'}
          badge={safety.break.on_break ? 'ON BREAK' : 'none'}
          value={
            safety.break.on_break
              ? `until ${epochClock(safety.break.break_until_epoch)}`
              : undefined
          }
        />
        {safety.break.on_break && safety.break.reason && (
          <div className="text-[10.5px] text-warn leading-snug">{safety.break.reason}</div>
        )}

        <SafetyRow
          label="crowd scraper observation"
          tone={safety.crowd_chain?.stale ? 'warn' : 'dim'}
          badge={safety.crowd_chain?.exhausted == null ? 'unknown' : safety.crowd_chain.exhausted ? 'exhausted' : 'available'}
        />
        <div className="divider" />
        <div className="text-[10.5px] text-faint leading-snug" data-testid="safety-readonly">
          read-only display · no control here can engage, clear or trip anything. The kill
          switch and breaker are changed only by the engine and the operator's own CLI.
        </div>
      </div>
    </Panel>
  )
}