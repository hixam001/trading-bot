import { useCallback, useEffect, useRef, useState } from 'react'
import { apiUrl } from '../lib/api'

/**
 * Polling fetch hook (I10: graceful degraded state — an unreachable API
 * renders an explicit offline banner, not a blank crash).
 */
const REQUEST_TIMEOUT_MS = 15_000

export function useApi<T>(url: string, intervalMs?: number) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const mountedRef = useRef(true)
  const inFlight = useRef<AbortController | null>(null)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      // §84: a hung request must not keep the poll loop alive after unmount.
      inFlight.current?.abort()
      inFlight.current = null
    }
  }, [])

  const refresh = useCallback(async () => {
    // The poll fires every N seconds; a request slower than the interval
    // would otherwise stack up. One in-flight fetch per hook.
    if (inFlight.current) return
    const ctrl = new AbortController()
    inFlight.current = ctrl
    const timer = window.setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS)
    try {
      const r = await fetch(apiUrl(url), {
        credentials: 'include',
        signal: ctrl.signal,
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const json = (await r.json()) as T
      if (mountedRef.current) {
        setData(json)
        setError(null)
      }
    } catch (e) {
      if (mountedRef.current) {
        const aborted = e instanceof DOMException && e.name === 'AbortError'
        setError(aborted
          ? `timeout after ${REQUEST_TIMEOUT_MS / 1000}s`
          : e instanceof Error ? e.message : String(e))
      }
    } finally {
      window.clearTimeout(timer)
      if (inFlight.current === ctrl) inFlight.current = null
      if (mountedRef.current) {
        setLoading(false)
      }
    }
  }, [url])

  useEffect(() => {
    refresh()
    if (!intervalMs) return
    const id = setInterval(refresh, intervalMs)
    return () => clearInterval(id)
  }, [refresh, intervalMs])

  return { data, error, loading, refresh }
}
