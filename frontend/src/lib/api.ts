/**
 * API base for split deployments (dashboard on Vercel/CF Pages, engine on its
 * own host). Empty string = same-origin — the local single-process mode where
 * the backend serves the built dashboard (unchanged behavior).
 *
 * SECURITY: this file must NEVER hold secrets. Anything exposed to Vite via
 * the VITE_ prefix is inlined into the public JS bundle at build time; the
 * only sanctioned variable here is VITE_API_BASE_URL (a public origin).
 */
export const API_BASE: string = (
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? ''
).replace(/\/+$/, '')

/** Prefix a backend-relative path (e.g. '/api/feed') with the API base. */
export function apiUrl(path: string): string {
  return `${API_BASE}${path}`
}

/** Build the WebSocket URL for a backend-relative path (e.g. '/ws/feed'). */
export function wsUrl(path: string): string {
  if (!API_BASE) {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    return `${proto}://${location.host}${path}`
  }
  const base = new URL(API_BASE)
  const proto = base.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${base.host}${path}`
}