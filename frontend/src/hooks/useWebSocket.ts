import { useEffect, useRef, useState } from 'react'
import type { FeedEventRow } from '../types'

/** WebSocket hook for /ws/feed live push (I1). */
export function useFeedSocket(): { events: FeedEventRow[]; connected: boolean } {
  const [events, setEvents] = useState<FeedEventRow[]>([])
  const [connected, setConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}/ws/feed`)
    wsRef.current = ws
    ws.onopen = () => setConnected(true)
    ws.onclose = () => setConnected(false)
    ws.onerror = () => setConnected(false)
    ws.onmessage = (m) => {
      try {
        const ev = JSON.parse(m.data) as FeedEventRow
        setEvents((prev) => [ev, ...prev].slice(0, 200))
      } catch {
        /* ignore malformed frame */
      }
    }
    return () => ws.close()
  }, [])

  return { events, connected }
}
