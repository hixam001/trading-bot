import type { SystemStatusResponse } from '../types'

/** System status (I8): Ollama connectivity + provider call budgets. */
export default function SystemStatus({ status }: { status: SystemStatusResponse }) {
  return (
    <div className="panel text-xs space-y-1">
      <div className="panel-title">system status</div>
      <div className="flex justify-between">
        <span className="text-term-dim">backend</span>
        <span>{status.data_backend}</span>
      </div>
      <div className="flex justify-between">
        <span className="text-term-dim">ollama</span>
        <span className={status.ollama_reachable ? 'text-term-green' : 'text-term-amber'}>
          {status.ollama_reachable ? `reachable (${status.model})` : 'unreachable'}
        </span>
      </div>
      <div className="flex justify-between">
        <span className="text-term-dim">narration mode</span>
        <span>{status.narration_mode}</span>
      </div>
      <div className="flex justify-between">
        <span className="text-term-dim">tick interval</span>
        <span>{status.tick_interval_seconds}s</span>
      </div>
      <div className="pt-1 text-term-dim">provider calls today:</div>
      {status.provider_calls_today.length === 0 && (
        <div className="text-term-dim">(mock backend makes no external calls)</div>
      )}
      {status.provider_calls_today.map((p) => (
        <div key={p.provider} className="flex justify-between">
          <span>{p.provider}</span>
          <span>
            {p.call_count} calls · {p.error_count} err · {p.rate_limit_429_count}×429
          </span>
        </div>
      ))}
    </div>
  )
}
