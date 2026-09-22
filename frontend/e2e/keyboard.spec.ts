import { expect, test, type Page } from '@playwright/test'

/**
 * §65 E2E — full keyboard vocabulary + mint-history drill-down, HERMETIC:
 * static frontend with mocked API/WS (same discipline as audit.spec.ts —
 * the engine is REAL-MONEY ARMED and is never started for UI tests).
 *
 *   1. typing guard: j/k/g/G/? never fire while typing in the palette input
 *   2. global j/k/g/G move the tape cursor without focusing the list
 *   3. Enter expands the focused row; Esc collapses it
 *   4. `?` help overlay: opens, lists real shortcuts, Esc / click-outside close
 *   5. mint-history drill-down: opens from Holdings, j/k inside it, Esc backs
 *      out (collapse row first, then pop the stack level)
 *   6. drill-down empty + error states (DESIGN.md §3 — never blank)
 */

const safety = {
  armed: true, kill_switch: { engaged: false, reason: '' },
  daily_loss_breaker: { engaged: false, limit_usd: 75, realized_pnl_today_usd: -20, headroom_usd: 55 },
  blocklist: { blocks: 0, auto: 0, manual: 0, latest: null },
  break: { on_break: false, reason: '', break_until_epoch: 0 }, alerts: [],
}

// A syntactically valid base58 mint (base58 has no 0/O/I/l) shared by every
// fixture row, so the drill-down has history to show.
const MINT = '5'.repeat(44)

const events = [3, 2, 1].map((id) => ({
  id, symbol: `S${id}`, verdict: 'pass', failed_rule_ids: [],
  ts: '2026-09-17T12:00:00Z', mint_address: MINT, candidate_snapshot: {},
  rule_breakdown: [{ rule_id: 'liquidity_floor', passed: true, detail: 'ok', value: 1 }],
  grounding_flags: [], thesis: `Fixture decision ${id}`, regime_ok: true,
  narration_source: 'deepseek',
}))

const history = {
  mint: MINT, total: 3, limit: 100, offset: 0, events,
}

const book = {
  enabled: true, wallet: 'Wallet111111111111111111111111111111111111', cash_usd: 10,
  equity_usd: 11, open_value_usd: 1, unrealized_pnl_usd: 0, deployed_today_usd: 1,
  closed_trades: 0, chain_excluded: [],
  positions: [{
    mint_address: MINT, symbol: 'S3', cost_usd: 1, tokens: 100,
    entry_price_usd: 0.01, current_price_usd: 0.01, value_usd: 1,
    unrealized_pnl_usd: 0, opened_at: '2026-09-17T12:00:00Z',
  }],
}

async function mockApi(page: Page, opts: { historyBody?: object; historyStatus?: number } = {}) {
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.startsWith('/api/mint/')) {
      const status = opts.historyStatus ?? 200
      const body = status === 200 ? (opts.historyBody ?? history) : { detail: 'fixture unavailable' }
      await route.fulfill({ status, json: body })
      return
    }
    const body = path === '/api/safety' ? safety
      : path === '/api/feed' ? { events }
      : path === '/api/system-status' ? { main_llm_reachable: true, main_llm_provider: 'deepseek',
        narration_mode: 'live', provider_calls_today: [], llm_usage_recent: [], tick_interval_seconds: 300 }
      : path === '/api/funnel' ? { candidates_seen: 3, candidates_seen_total: 3, gate_refused: 0,
        gate_passed: 3, model_refused: 0, model_approved: 3, filled: 0,
        model_refusal_rate_of_gate_passers: 0, window: { limit: 1000, feed_events: 3 } }
      : path === '/api/funnel/snapshots' ? { snapshots: [], count: 0 }
      : path === '/api/live/executions' ? { enabled: true, commits: [{
          hash: 'h1', kind: 'buy', status: 'bound', sealed_at: 1760000000,
          payload: { symbol: 'S3', mint: MINT, usd: 1 } }], records: [],
          totals: { commits: 1, bound: 1, failed: 0, published_unfilled: 0, buys: 1, closes: 0 } }
      : path === '/api/live/portfolio' ? book
      : path === '/api/market-regime' ? { regimes: [] }
      : path === '/api/stats' ? { cash_usd: 10, equity_usd: 11, win_rate: null,
        profit_factor: null, max_drawdown_pct: null, equity_curve: [], paper_trading_only: false }
      : null
    await route.fulfill({ status: body ? 200 : 503, json: body ?? { detail: 'fixture unavailable' } })
  })
  await page.routeWebSocket('**/ws/feed', () => {})
}

test.describe('§65 global keyboard vocabulary', () => {
  test('j/k/g/G never fire while typing in the palette input', async ({ page }) => {
    await mockApi(page)
    await page.goto('/')
    const list = page.getByTestId('feed-list')
    await expect(list.locator('[data-feed-row="0"]')).toBeVisible()

    // Baseline: one global j moves the cursor to row 1.
    await page.keyboard.press('j')
    await expect(list.locator('[data-feed-row="1"]')).toHaveAttribute('aria-selected', 'true')

    // Open the palette and TYPE — every one of these keystrokes must be
    // swallowed by the input (the classic global-shortcut failure mode).
    await page.keyboard.press('Control+k')
    const input = page.getByTestId('palette-input')
    await expect(input).toBeFocused()
    await input.pressSequentially('jjkkgG?')
    await page.keyboard.press('Escape')
    await expect(page.getByRole('dialog', { name: 'command palette' })).toBeHidden()

    // The cursor never moved past row 1, and no help overlay opened.
    await expect(list.locator('[data-feed-row="1"]')).toHaveAttribute('aria-selected', 'true')
    await expect(list.locator('[data-feed-row="2"]')).toHaveAttribute('aria-selected', 'false')
    await expect(page.getByTestId('shortcut-overlay')).toHaveCount(0)
  })

  test('j/k/g/G move the tape cursor without focusing the list', async ({ page }) => {
    await mockApi(page)
    await page.goto('/')
    const list = page.getByTestId('feed-list')
    await expect(list.locator('[data-feed-row="0"]')).toBeVisible()

    await page.keyboard.press('j')
    await expect(list.locator('[data-feed-row="1"]')).toHaveAttribute('aria-selected', 'true')
    await page.keyboard.press('k')
    await expect(list.locator('[data-feed-row="0"]')).toHaveAttribute('aria-selected', 'true')
    await page.keyboard.press('j')
    await page.keyboard.press('j')
    await expect(list.locator('[data-feed-row="2"]')).toHaveAttribute('aria-selected', 'true')
    await page.keyboard.press('g')
    await expect(list.locator('[data-feed-row="0"]')).toHaveAttribute('aria-selected', 'true')
    await page.keyboard.press('G')
    await expect(list.locator('[data-feed-row="2"]')).toHaveAttribute('aria-selected', 'true')
  })

  test('Enter expands the focused row; Esc collapses it', async ({ page }) => {
    await mockApi(page)
    await page.goto('/')
    const list = page.getByTestId('feed-list')
    await expect(list.locator('[data-feed-row="0"]')).toBeVisible()

    await page.keyboard.press('j')
    const rowButton = list.locator('[data-feed-row="1"]').locator('button[aria-expanded]')
    await page.keyboard.press('Enter')
    await expect(rowButton).toHaveAttribute('aria-expanded', 'true')
    // The expanded row carries the mint + its §65 history affordance.
    await expect(list.getByTestId('mint-history-button').first()).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(rowButton).toHaveAttribute('aria-expanded', 'false')
  })

  test('? opens the help overlay; Esc and click-outside close it', async ({ page }) => {
    await mockApi(page)
    await page.goto('/')
    await expect(page.getByTestId('feed-list')).toBeVisible()

    await page.keyboard.press('?')
    const overlay = page.getByTestId('shortcut-overlay')
    await expect(overlay).toBeVisible()
    // Rendered from the same table the dispatcher implements: every real
    // binding is listed, including the palette itself.
    await expect(page.getByTestId('shortcut-list')).toContainText('⌘K / Ctrl-K')
    await expect(page.getByTestId('shortcut-list')).toContainText('j / k')
    await expect(page.getByTestId('shortcut-list')).toContainText('?')

    await page.keyboard.press('Escape')
    await expect(overlay).toBeHidden()

    await page.keyboard.press('?')
    await expect(overlay).toBeVisible()
    await page.mouse.click(10, 400) // outside the centered panel
    await expect(overlay).toBeHidden()
  })
})

test.describe('§65 mint-history drill-down', () => {
  test('opens from a Holdings row, j/k/Enter operate it, Esc backs out', async ({ page }) => {
    await mockApi(page)
    await page.goto('/')
    await page.getByTestId('tab-holdings').click()
    const btn = page.getByTestId('mint-history-button').first()
    await expect(btn).toBeVisible()
    // Copy and history are separate targets: the copy button still exists
    // (CopyText's own title-attribute button, name = the full address).
    await expect(page.locator('button[title="click to copy"]').first()).toBeVisible()
    await btn.click()

    const historyPanel = page.getByTestId('mint-history')
    await expect(historyPanel).toBeVisible()
    await expect(historyPanel).toContainText(MINT.slice(0, 10))
    const list = page.getByTestId('mint-history-list')
    await expect(list.locator('[data-mh-row="0"]')).toHaveAttribute('aria-selected', 'true')

    // The drill-down is the registered list now: j moves ITS cursor.
    await page.keyboard.press('j')
    await expect(list.locator('[data-mh-row="1"]')).toHaveAttribute('aria-selected', 'true')
    await page.keyboard.press('Enter')
    const rowButton = list.locator('[data-mh-row="1"]').locator('button[aria-expanded]')
    await expect(rowButton).toHaveAttribute('aria-expanded', 'true')

    // Esc first collapses the expanded row…
    await page.keyboard.press('Escape')
    await expect(rowButton).toHaveAttribute('aria-expanded', 'false')
    await expect(historyPanel).toBeVisible()
    // …then backs out of the stack level.
    await page.keyboard.press('Escape')
    await expect(historyPanel).toBeHidden()
    await expect(page.getByTestId('holdings')).toBeVisible()
  })

  test('empty history renders the documented empty state, not blank', async ({ page }) => {
    await mockApi(page, { historyBody: { mint: MINT, total: 0, limit: 100, offset: 0, events: [] } })
    await page.goto('/')
    await page.getByTestId('tab-holdings').click()
    await page.getByTestId('mint-history-button').first().click()
    const historyPanel = page.getByTestId('mint-history')
    await expect(historyPanel).toBeVisible()
    await expect(historyPanel).toContainText('No recorded decisions')
    await expect(historyPanel).toContainText('0 events')
  })

  test('a failed history fetch renders the error state with auto-retry', async ({ page }) => {
    await mockApi(page, { historyStatus: 503 })
    await page.goto('/')
    await page.getByTestId('tab-holdings').click()
    await page.getByTestId('mint-history-button').first().click()
    const historyPanel = page.getByTestId('mint-history')
    await expect(historyPanel).toContainText('mint history unavailable', { timeout: 10_000 })
    await expect(historyPanel).toContainText('Retrying automatically')
  })
})
