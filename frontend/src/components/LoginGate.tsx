import { useState, type FormEvent } from 'react'
import { apiUrl } from '../lib/api'

export default function LoginGate() {
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const trimmed = token.trim()
    if (!trimmed || busy) return
    setBusy(true)
    setError(null)
    try {
      const res = await fetch(apiUrl('/api/auth/login'), {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: trimmed }),
      })
      if (!res.ok) {
        let detail = `HTTP ${res.status}`
        try {
          const body = (await res.json()) as { detail?: unknown }
          if (typeof body.detail === 'string') detail = body.detail
        } catch {
          detail = `HTTP ${res.status}`
        }
        throw new Error(detail)
      }
      window.location.reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 bg-base/95 flex items-center justify-center px-4"
      data-testid="login-gate"
    >
      <form
        onSubmit={onSubmit}
        className="w-full max-w-[420px] panel"
        aria-labelledby="login-title"
      >
        <div className="panel-header">
          <h2 id="login-title" className="panel-title">
            operator login
          </h2>
        </div>
        <p className="text-xs text-dim mb-3">
          This dashboard is protected. Enter the operator token to open a
          session. The token is exchanged for an HttpOnly session cookie and is
          never stored in the browser.
        </p>
        <label className="stat-label block mb-1" htmlFor="operator-token">
          admin token
        </label>
        <input
          id="operator-token"
          type="password"
          autoComplete="current-password"
          autoFocus
          value={token}
          onChange={(e) => setToken(e.target.value)}
          className="w-full bg-base border border-line-strong rounded px-3 py-2 font-mono text-sm text-bright outline-none focus:border-live"
        />
        {error && (
          <div role="alert" className="mt-2 text-xs text-fail">
            login failed: {error}
          </div>
        )}
        <button
          type="submit"
          disabled={busy || !token.trim()}
          className="mt-3 w-full bg-live-soft border border-live text-live font-mono text-xs font-semibold tracking-[0.06em] rounded px-3 py-2 disabled:opacity-50"
        >
          {busy ? 'authenticating…' : 'authenticate'}
        </button>
      </form>
    </div>
  )
}
