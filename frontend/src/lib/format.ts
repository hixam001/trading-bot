/**
 * Formatting helpers — DESIGN.md §5: render backend values verbatim, never
 * do client-side money math, and render null/undefined as an em dash (never
 * `$0.00`, never blank). These only shape strings for display.
 */

/** `$1,234.56` — or `—` when the value is missing. */
export function usd(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `$${v.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`
}

/** Signed money: `+$1.23` / `−$1.23` / `$0.00` — or `—` when missing. */
export function signedUsd(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const abs = usd(Math.abs(v), digits)
  if (v > 0) return `+${abs}`
  if (v < 0) return `−${abs}`
  return usd(0, digits)
}

/** Semantic class for a signed value (pos/neg). Empty string when missing. */
export function pnlClass(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return ''
  return v >= 0 ? 'text-pos' : 'text-neg'
}

/** Small prices keep precision: `$0.00001234` (4 significant figures). */
export function price(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v) || v === 0) return '—'
  if (Math.abs(v) >= 1) return usd(v, 2)
  return `$${v.toPrecision(4)}`
}

/** Percent with sign: `+12.3%` / `−4.5%` — or `—` when missing. */
export function signedPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const abs = `${Math.abs(v).toFixed(digits)}%`
  if (v > 0) return `+${abs}`
  if (v < 0) return `−${abs}`
  return `0.${'0'.repeat(digits)}%`
}

/** `1234.5678` plain number (SOL balances, token counts) — or `—`. */
export function num(v: number | null | undefined, digits = 4): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return v.toLocaleString('en-US', {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  })
}

/** `14:03:22` local clock time from an ISO string. */
export function clock(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleTimeString('en-GB', { hour12: false })
}

/** `AbCd…WxYz` — short address form. Full value stays in the DOM/title. */
export function shortAddr(addr: string | null | undefined, head = 4, tail = 4): string {
  if (!addr) return '—'
  if (addr.length <= head + tail + 1) return addr
  return `${addr.slice(0, head)}…${addr.slice(-tail)}`
}
