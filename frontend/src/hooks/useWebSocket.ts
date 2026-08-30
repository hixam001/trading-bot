import { useEffect, useRef, useState } from 'react'
import { apiUrl, wsUrl } from '../lib/api'
import type { FeedEventRow } from '../types'

/**
 * Decision feed hook (I1): hydrate recent history from REST /api/feed on mount
 * (so the feed survives reloads and is never blank while waiting for the next
 * tick), then live-append new decisions over WebSocket /ws/feed. Rows are
 * deduped by id and kept newest-first, capped at 200.
 */
export function useFeedSocket(): { events: FeedEventRow[]; connected: boolean } {
  const [events, setEvents] = useState<FeedEventRow[]>([])
  const [connected, setConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)

  // Merge helper: newest-first, deduped by id, capped.
  const merge = (prev: FeedEventRow[], incoming: FeedEventRow[]) => {
    const byId = new Map<number, FeedEventRow>()
    for (const ev of [...prev, ...incoming]) byId.set(ev.id, ev)
    return [...byId.values()]
      .sort((a, b) => b.id - a.id)
      .slice(0, 200)
  }

  // 1) Hydrate history (fail-soft: a REST hiccup just leaves the WS path).
  useEffect(() => {
    let cancelled = false
    fetch(apiUrl('/api/feed?limit=50'))
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (cancelled || !d || !Array.isArray(d.events)) return
        setEvents((prev) => merge(prev, d.events as FeedEventRow[]))
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [])

  // 2) Live push over WebSocket.
  useEffect(() => {
    const ws = new WebSocket(wsUrl('/ws/feed'))
    wsRef.current = ws
    ws.onopen = () => setConnected(true)
    ws.onclose = () => setConnected(false)
    ws.onerror = () => setConnected(false)
    ws.onmessage = (m) => {
      try {
        const ev = JSON.parse(m.data) as FeedEventRow
        setEvents((prev) => merge(prev, [ev]))
      } catch {
        /* ignore malformed frame */
      }
    }
    return () => ws.close()
  }, [])

  return { events, connected }
}

