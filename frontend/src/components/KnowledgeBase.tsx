import { useState } from 'react'
import type { KnowledgeBaseResponse } from '../types'

export default function KnowledgeBase({ kb }: { kb: KnowledgeBaseResponse }) {
  const [tab, setTab] = useState<'static' | 'ingested' | 'stats'>('static')
  return (
    <div className="panel overflow-auto" style={{ maxHeight: '50vh' }}>
      <div className="panel-title flex gap-3 items-center">
        <span>knowledge base</span>
        {(['static', 'ingested', 'stats'] as const).map((t) => (
          <button
            key={t}
            className={`text-xs px-2 rounded ${tab === t ? 'bg-term-bg text-term-blue' : 'text-term-dim'}`}
            onClick={() => setTab(t)}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === 'static' && (
        <pre className="text-xs whitespace-pre-wrap text-term-dim">
          {kb.static_knowledge || 'No static knowledge file found.'}
        </pre>
      )}

      {tab === 'ingested' && (
        <div className="space-y-2 text-xs">
          {kb.ingested.length === 0 && (
            <div className="text-term-dim">Nothing ingested yet.</div>
          )}
          {kb.ingested.map((d) => (
            <div key={d.filename}>
              <div className="font-bold">{d.filename}</div>
              <div className="text-term-dim">{d.digest}</div>
            </div>
          ))}
        </div>
      )}

      {tab === 'stats' && (
        <div className="grid grid-cols-2 gap-4 text-xs">
          {Object.entries(kb.dynamic_stats).map(([group, buckets]) => (
            <div key={group}>
              <div className="text-term-dim mb-1">{group.replace('by_', 'win rate ')}</div>
              {Object.entries(buckets).map(([label, s]) => (
                <div key={label} className="flex justify-between gap-2">
                  <span>{label}</span>
                  <span>
                    {s.win_rate !== null
                      ? `${(s.win_rate * 100).toFixed(0)}% (${s.trades})`
                      : `— (0)`}
                  </span>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
