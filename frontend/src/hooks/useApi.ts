import { useCallback, useEffect, useState } from 'react'
import { apiUrl } from '../lib/api'

/**
 * Polling fetch hook (I10: graceful degraded state — an unreachable API
 * renders an explicit offline banner, not a blank crash).
 */
export function useApi<T>(url: string, intervalMs?: number) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    try {
      const r = await fetch(apiUrl(url))
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setData((await r.json()) as T)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
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
