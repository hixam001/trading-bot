import { apiUrl } from './api'

export type BotChallenge = {
  enabled: boolean
  provider: 'internal' | 'turnstile' | 'hcaptcha'
  site_key: string
  nonce: string
  timestamp: number
  signature: string
  difficulty: number
}

export async function getBotChallenge(signal?: AbortSignal): Promise<BotChallenge> {
  const response = await fetch(apiUrl('/api/auth/challenge'), { signal, credentials: 'include' })
  if (!response.ok) throw new Error('Unable to load login verification. Please retry.')
  return response.json() as Promise<BotChallenge>
}

export async function solveBotChallenge(challenge: BotChallenge): Promise<string> {
  if (!Number.isInteger(challenge.difficulty) || challenge.difficulty < 1 || challenge.difficulty > 4) {
    throw new Error('Invalid login verification challenge.')
  }
  const encoder = new TextEncoder()
  const deadline = Date.now() + 20_000
  for (let solution = 0; solution < 1_000_000 && Date.now() < deadline; solution++) {
    const bytes = new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(`${challenge.nonce}:${solution}`)))
    const digest = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
    if (digest.startsWith('0'.repeat(challenge.difficulty))) {
      return `${challenge.nonce}.${challenge.timestamp}.${challenge.signature}.${solution}`
    }
  }
  throw new Error('Login verification timed out. Please retry.')
}
