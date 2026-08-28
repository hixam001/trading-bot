import type { SystemStatusResponse } from '../types'
import { Badge, Empty, Panel } from './ui'
import { clock } from '../lib/format'

/**
 * System status (I8): the configured reasoning model (DeepSeek is main now —
 * ollama is no longer the brain), data backend, and per-provider call budgets.
 */
export default function SystemStatus({ status }: { status: SystemStatusResponse }) {
  const reasoning = status.narration_mode
  return (
    <Panel testId="system-status" title="System status">
      <div className="text-xs space-y-1.5">
        <div className="flex justify-between items-center">
          <span className="text-dim">reasoning model</span>
          <Badge tone={reasoning === 'template' ? 'dim' : 'info'}>{reasoning}</Badge>
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
              <span className="flex items-center gap-1.5 min-w-0">
                <span
                  className={`shrink-0 w-1.5 h-1.5 rounded-full ${
                    u.status === 'success' ? 'bg-pos' : 'bg-neg'
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
  )
}

