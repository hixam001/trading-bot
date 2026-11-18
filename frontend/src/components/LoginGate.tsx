import { useEffect, useRef, useState, type FormEvent } from 'react'
import { apiUrl } from '../lib/api'
import { getBotChallenge, solveBotChallenge, type BotChallenge } from '../lib/captcha'

type CaptchaApi = {
  render: (element: HTMLElement, options: Record<string, unknown>) => string | number
  remove: (id: string | number) => void
  reset: (id: string | number) => void
}

export default function LoginGate() {
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [challenge, setChallenge] = useState<BotChallenge | null>(null)
  const [captchaToken, setCaptchaToken] = useState('')
  const widgetElement = useRef<HTMLDivElement>(null)
  const widget = useRef<{ api: CaptchaApi; id: string | number } | null>(null)

  useEffect(() => {
    const abort = new AbortController()
    getBotChallenge(abort.signal).then(setChallenge).catch(err => {
      if (!abort.signal.aborted) setError(err instanceof Error ? err.message : String(err))
    })
    return () => abort.abort()
  }, [])

  useEffect(() => {
    if (!challenge?.enabled || challenge.provider === 'internal') return
    if (!challenge.site_key) {
      setError('Login verification is missing its public site key. Contact the operator.')
      return
    }
    if (!['turnstile', 'hcaptcha'].includes(challenge.provider)) {
      setError('Unsupported login verification provider.')
      return
    }
    let active = true
    const script = document.createElement('script')
    script.src = challenge.provider === 'turnstile'
      ? 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit'
      : 'https://js.hcaptcha.com/1/api.js?render=explicit'
    script.async = true
    script.onload = () => {
      if (!active || !widgetElement.current) return
      const api = (window as unknown as Record<string, CaptchaApi>)[challenge.provider]
      if (!api) { setError('Login verification failed to load. Please reload.'); return }
      widget.current = { api, id: api.render(widgetElement.current, {
        sitekey: challenge.site_key,
        callback: (value: string) => { if (active) setCaptchaToken(value) },
        'expired-callback': () => { if (active) setCaptchaToken('') },
        'error-callback': () => { if (active) { setCaptchaToken(''); setError('Login verification failed. Please retry.') } },
      }) }
    }
    script.onerror = () => { if (active) setError('Login verification failed to load. Please reload.') }
    document.head.appendChild(script)
    return () => {
      active = false
      if (widget.current) widget.current.api.remove(widget.current.id)
      widget.current = null
      script.remove()
    }
  }, [challenge])

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const trimmed = token.trim()
    if (!trimmed || busy || !challenge) return
    setBusy(true)
    setError(null)
    try {
      let verification = captchaToken
      if (challenge.enabled && challenge.provider === 'internal') {
        verification = await solveBotChallenge(await getBotChallenge())
      }
      const res = await fetch(apiUrl('/api/auth/login'), {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: trimmed, captcha_token: verification || undefined }),
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
      setCaptchaToken('')
      if (widget.current) widget.current.api.reset(widget.current.id)
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
        {challenge?.enabled && challenge.provider !== 'internal' && <div ref={widgetElement} className="mt-3" />}
        {error && (
          <div role="alert" className="mt-2 text-xs text-fail">
            login failed: {error}
          </div>
        )}
        <button
          type="submit"
          disabled={busy || !token.trim() || !challenge || (challenge.enabled && challenge.provider !== 'internal' && !captchaToken)}
          className="mt-3 w-full bg-live-soft border border-live text-live font-mono text-xs font-semibold tracking-[0.06em] rounded px-3 py-2 disabled:opacity-50"
        >
          {busy ? 'authenticating…' : 'authenticate'}
        </button>
      </form>
    </div>
  )
}
