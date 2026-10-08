import { useCallback, useEffect, useRef, useState } from 'react'
import { apiUrl } from '../lib/api'

/**
 * Polling fetch hook (I10: graceful degraded state — an unreachable API
 * renders an explicit offline banner, not a blank crash).
 */
export function useApi<T>(url: string, intervalMs?: number) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const mountedRef = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  const refresh = useCallback(async () => {
    try {
      const r = await fetch(apiUrl(url))
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const json = (await r.json()) as T
      if (mountedRef.current) {
        setData(json)
        setError(null)
      }
    } catch (e) {
      if (mountedRef.current) {
        setError(e instanceof Error ? e.message : String(e))
      }
    } finally {
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
