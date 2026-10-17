import { expect, test, type ConsoleMessage } from '@playwright/test'

/**
 * E2E QA — DESIGN.md §6 checklist (§81 Phase F-3).
 *
 * Deterministic tests using fixtures via page.route() alongside unmocked smoke check:
 *   1. Unmocked smoke check: loads with zero console errors
 *   2. Complete-equity fixture: hero displays exact equity and live book displays Open value
 *   3. Unavailable fixture: live book displays explicit unavailable state
 *   4. Populated journal fixture: proof button expands and displays commit hash and memo
 *   5. Empty journal fixture: displays documented empty state
 *   6. Populated holdings fixture: displays open positions
 *   7. Empty holdings fixture: displays documented empty state
 *   8. Feed expand/collapse & keyboard interaction with deterministic feed fixture
 *   9. Offline banner appears when API is unreachable
 *  10. Panel error states when specific endpoints fail
 */

const APP = '/'

const SAMPLE_FEED_EVENT = {
  id: 1,
  ts: '2026-10-17T12:00:00Z',
  symbol: 'BONK',
  mint_address: 'DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263',
  candidate_snapshot: {},
  verdict: 'pass',
  thesis: 'High volume and clear momentum across pairs.',
  rule_breakdown: [
    { rule_id: 'liquidity', passed: true, detail: 'liq $50k', value: 50000 },
  ],
  failed_rule_ids: [],
  regime_ok: true,
  grounding_flags: [],
  narration_source: 'model',
  led_to_trade_id: null,
}

const COMPLETE_PORTFOLIO_FIXTURE = {
  enabled: true,
  armed: true,
  manual_confirmation: false,
  wallet: '7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU',
  cash_usd: 100.0,
  sol_balance: 1.5,
  equity_usd: 1250.5,
  open_value_usd: 250.5,
  unrealized_pnl_usd: 5.0,
  realized_pnl_usd: 12.0,
  deployed_today_usd: 20.0,
  closed_trades: 3,
  positions: [
    {
      mint_address: 'DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263',
      symbol: 'BONK',
      cost_usd: 45.0,
      tokens: 1000000.0,
      entry_price_usd: 0.000045,
      current_price_usd: 0.00005,
      value_usd: 250.5,
      unrealized_pnl_usd: 5.0,
      opened_at: '2026-10-17T12:00:00Z',
    },
  ],
  chain_excluded: [],
  count: 1,
  generated_at_utc: '2026-10-17T12:00:00Z',
}

const COMPLETE_STATS_FIXTURE = {
  initial_cash_usd: 10000.0,
  cash_usd: 100.0,
  cash_status: 'live',
  equity_usd: 10050.0,
  wallet_equity_usd: 1250.5,
  equity_status: 'complete',
  missing: [],
  open_positions: 1,
  closed_trades: 3,
  win_rate: 0.6667,
  profit_factor: 1.5,
  max_drawdown_pct: 2.5,
  total_pnl_usd: 50.0,
  realized_pnl_usd: 12.0,
  unrealized_pnl_usd: 5.0,
  total_spend_usd: 45.0,
  equity_curve: [],
  paper_trading_only: false,
}

const POPULATED_JOURNAL_FIXTURE = {
  enabled: true,
  generated_at_utc: '2026-10-17T12:00:00Z',
  commits: [
    {
      hash: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
      sealed_at: 1729166400,
      kind: 'buy',
      status: 'bound',
      fail_reason: null,
      memo_signature: '5w6vYyA111111111111111111111111111111111111',
      memo_slot: 12345678,
      signature: '3xYzA222222222222222222222222222222222222222',
      payload: {
        symbol: 'BONK',
        mint: 'DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263',
        usd: 25.0,
      },
    },
  ],
  records: [],
  totals: {
    commits: 1,
    bound: 1,
    failed: 0,
    published_unfilled: 0,
    buys: 1,
    closes: 0,
  },
}

/** Collect browser console errors (warnings are expected from WS reconnects). */
async function collectConsoleErrors(page: import('@playwright/test').Page) {
  const errors: string[] = []
  page.on('console', (msg: ConsoleMessage) => {
    if (msg.type() === 'error') errors.push(msg.text())
  })
  page.on('pageerror', (err) => errors.push(String(err)))
  return errors
}

test.describe('dashboard', () => {
  test('unmocked smoke check: loads with zero console errors and renders shell', async ({ page }) => {
    const errors = await collectConsoleErrors(page)
    await page.goto(APP)

    // Header identity + live badge are present.
    await expect(page.getByText('trading-bot', { exact: false }).first()).toBeVisible()
    await expect(page.getByText('LIVE · real money')).toBeVisible()

    await expect(page.getByTestId('live-feed')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByTestId('market-regime')).toBeVisible({ timeout: 15_000 })
    await page.getByTestId('tab-system').click()
    await expect(page.getByTestId('system-status')).toBeVisible({ timeout: 15_000 })
    await page.getByTestId('tab-dashboard').click()

    await expect
      .poll(async () => page.locator('.skeleton').count(), { timeout: 45_000 })
      .toBe(0)

    const realErrors = errors.filter(
      (e) => !e.includes('WebSocket') && !e.includes('Failed to load resource'),
    )
    expect(realErrors).toEqual([])
  })

  test('complete-equity fixture shows Open value and exact equity figure', async ({ page }) => {
    await page.route('**/api/stats', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(COMPLETE_STATS_FIXTURE),
      }),
    )
    await page.route('**/api/live/portfolio', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(COMPLETE_PORTFOLIO_FIXTURE),
      }),
    )

    await page.goto(APP)
    const hero = page.getByTestId('hero')
    await expect(hero).toBeVisible({ timeout: 15_000 })
    await expect(hero.getByText('EQUITY')).toBeVisible()
    await expect(hero.getByText('$1,250.50')).toBeVisible()

    const book = page.getByTestId('live-book')
    await expect(book).toBeVisible({ timeout: 15_000 })
    await expect(book.getByText('Open value')).toBeVisible()
    await expect(book.getByText('$250.50')).toBeVisible()
  })

  test('unavailable fixture shows Live book not available', async ({ page }) => {
    await page.route('**/api/live/portfolio', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          enabled: false,
          reason: 'wallet unavailable',
        }),
      }),
    )

    await page.goto(APP)
    await expect(
      page.getByText('Live book not available: wallet unavailable.', { exact: false }),
    ).toBeVisible({ timeout: 15_000 })
  })

  test('feed rows expand and collapse with aria-expanded using deterministic fixture', async ({
    page,
  }) => {
    await page.route('**/api/feed*', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          total: 1,
          limit: 50,
          offset: 0,
          events: [SAMPLE_FEED_EVENT],
        }),
      }),
    )

    await page.goto(APP)
    const feed = page.getByTestId('live-feed')
    await expect(feed).toBeVisible({ timeout: 15_000 })

    const firstRow = feed.locator('button[aria-expanded]').first()
    await expect(firstRow).toBeVisible({ timeout: 15_000 })

    // Expand
    await firstRow.click()
    await expect(firstRow).toHaveAttribute('aria-expanded', 'true')
    await expect(feed.getByText('contract:').first()).toBeVisible()

    // Collapse
    await firstRow.click()
    await expect(firstRow).toHaveAttribute('aria-expanded', 'false')
  })

  test('empty feed fixture displays documented empty state', async ({ page }) => {
    await page.route('**/api/feed*', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ total: 0, limit: 50, offset: 0, events: [] }),
      }),
    )

    await page.goto(APP)
    const feed = page.getByTestId('live-feed')
    await expect(feed).toBeVisible({ timeout: 15_000 })
    await expect(feed.getByText('No decisions yet', { exact: false })).toBeVisible()
  })

  test('feed row is keyboard operable (Enter toggles)', async ({ page }) => {
    await page.route('**/api/feed*', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          total: 1,
          limit: 50,
          offset: 0,
          events: [SAMPLE_FEED_EVENT],
        }),
      }),
    )

    await page.goto(APP)
    const feed = page.getByTestId('live-feed')
    await expect(feed).toBeVisible({ timeout: 15_000 })

    const firstRow = feed.locator('button[aria-expanded]').first()
    await expect(firstRow).toBeVisible({ timeout: 15_000 })

    await firstRow.focus()
    await page.keyboard.press('Enter')
    await expect(firstRow).toHaveAttribute('aria-expanded', 'true')
    await page.keyboard.press('Enter')
    await expect(firstRow).toHaveAttribute('aria-expanded', 'false')
  })

  test('offline banner appears when the API is unreachable', async ({ page }) => {
    await page.route('**/api/**', (route) => route.abort())
    await page.goto(APP)

    const banner = page.getByTestId('offline-banner')
    await expect(banner).toBeVisible({ timeout: 15_000 })
    await expect(banner).toContainText('API unreachable')
  })
})

test.describe('pages (tabs)', () => {
  test('tab bar navigates live / holdings / journal / market / system', async ({ page }) => {
    await page.goto(APP)
    await expect(page.getByTestId('tab-dashboard')).toBeVisible()
    await expect(page.getByTestId('tab-holdings')).toBeVisible()
    await expect(page.getByTestId('tab-journal')).toBeVisible()
    await expect(page.getByTestId('tab-market')).toBeVisible()
    await expect(page.getByTestId('tab-system')).toBeVisible()

    // Live (dashboard)
    await expect(page.getByTestId('live-feed')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByTestId('tab-dashboard')).toHaveAttribute('aria-current', 'page')

    // Holdings
    await page.getByTestId('tab-holdings').click()
    await expect(page.getByTestId('holdings')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByTestId('tab-holdings')).toHaveAttribute('aria-current', 'page')

    // Journal
    await page.getByTestId('tab-journal').click()
    await expect(page.getByTestId('journal')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByTestId('tab-journal')).toHaveAttribute('aria-current', 'page')

    // Market
    await page.getByTestId('tab-market').click()
    await expect(page.getByTestId('market-regime')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByTestId('tab-market')).toHaveAttribute('aria-current', 'page')

    // System
    await page.getByTestId('tab-system').click()
    await expect(page.getByTestId('system-status')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByTestId('tab-system')).toHaveAttribute('aria-current', 'page')

    // Back to live
    await page.getByTestId('tab-dashboard').click()
    await expect(page.getByTestId('live-feed')).toBeVisible({ timeout: 15_000 })
  })

  test('populated holdings fixture shows open positions', async ({ page }) => {
    await page.route('**/api/live/portfolio', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(COMPLETE_PORTFOLIO_FIXTURE),
      }),
    )

    await page.goto(APP)
    await page.getByTestId('tab-holdings').click()
    const holdings = page.getByTestId('holdings')
    await expect(holdings).toBeVisible({ timeout: 15_000 })
    await expect(holdings.getByText('Open value')).toBeVisible()
    await expect(holdings.getByText('$BONK')).toBeVisible()
  })

  test('empty holdings fixture shows documented empty state', async ({ page }) => {
    await page.route('**/api/live/portfolio', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          enabled: true,
          armed: true,
          manual_confirmation: false,
          wallet: '7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU',
          cash_usd: 100.0,
          sol_balance: 1.5,
          equity_usd: 100.0,
          open_value_usd: 0.0,
          unrealized_pnl_usd: 0.0,
          realized_pnl_usd: 0.0,
          deployed_today_usd: 0.0,
          closed_trades: 0,
          positions: [],
          chain_excluded: [],
          count: 0,
          generated_at_utc: '2026-10-17T12:00:00Z',
        }),
      }),
    )

    await page.goto(APP)
    await page.getByTestId('tab-holdings').click()
    const holdings = page.getByTestId('holdings')
    await expect(holdings).toBeVisible({ timeout: 15_000 })
    await expect(
      holdings.getByText('No open live positions', { exact: false }),
    ).toBeVisible({ timeout: 15_000 })
  })

  test('populated journal fixture expands proof detail showing commit hash and memo', async ({
    page,
  }) => {
    await page.route('**/api/live/executions', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(POPULATED_JOURNAL_FIXTURE),
      }),
    )

    await page.goto(APP)
    await page.getByTestId('tab-journal').click()
    const journal = page.getByTestId('journal')
    await expect(journal).toBeVisible({ timeout: 15_000 })
    await expect(journal.getByText('Order decisions', { exact: true })).toBeVisible()

    const proofBtn = journal.locator('button[aria-expanded]').first()
    await expect(proofBtn).toBeVisible({ timeout: 15_000 })

    await proofBtn.click()
    await expect(proofBtn).toHaveAttribute('aria-expanded', 'true')
    await expect(journal.getByText('commit hash:').first()).toBeVisible()
    await expect(journal.getByText('memo:').first()).toBeVisible()

    await proofBtn.click()
    await expect(proofBtn).toHaveAttribute('aria-expanded', 'false')
  })

  test('empty journal fixture shows documented empty state', async ({ page }) => {
    await page.route('**/api/live/executions', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          enabled: true,
          generated_at_utc: '2026-10-17T12:00:00Z',
          commits: [],
          records: [],
          totals: {
            commits: 0,
            bound: 0,
            failed: 0,
            published_unfilled: 0,
            buys: 0,
            closes: 0,
          },
        }),
      }),
    )

    await page.goto(APP)
    await page.getByTestId('tab-journal').click()
    const journal = page.getByTestId('journal')
    await expect(journal).toBeVisible({ timeout: 15_000 })
    await expect(journal.getByText('Order decisions', { exact: true })).toBeVisible()
    await expect(
      journal.getByText('No sealed order decisions yet', { exact: false }),
    ).toBeVisible({ timeout: 15_000 })
  })
})

test.describe('panel error states (DESIGN.md §3.3)', () => {
  test('system tab shows an explicit error panel when only its feed fails', async ({ page }) => {
    await page.route('**/api/system-status*', (route) => route.abort())
    await page.goto(APP)
    await page.getByTestId('tab-system').click()

    const panel = page.getByTestId('error-panel')
    await expect(panel).toBeVisible({ timeout: 15_000 })
    await expect(panel).toContainText('System status')
    await expect(panel).toContainText('Retrying automatically')
    await expect(page.getByTestId('offline-banner')).toHaveCount(0)
  })

  test('journal tab shows an explicit error panel when its feed fails', async ({ page }) => {
    await page.route('**/api/live/executions*', (route) => route.abort())
    await page.goto(APP)
    await page.getByTestId('tab-journal').click()

    const panel = page.getByTestId('error-panel')
    await expect(panel).toBeVisible({ timeout: 15_000 })
    await expect(panel).toContainText('Journal')
    await expect(panel).toContainText('Retrying automatically')
  })

  test('every tab shows an explicit error panel when the API is unreachable', async ({ page }) => {
    await page.route('**/api/**', (route) => route.abort())
    await page.goto(APP)

    await expect(
      page.getByTestId('error-panel').filter({ hasText: 'Performance' }),
    ).toBeVisible({ timeout: 15_000 })
    await expect(
      page.getByTestId('error-panel').filter({ hasText: 'Market regime' }),
    ).toBeVisible()

    for (const t of ['holdings', 'journal', 'market', 'system'] as const) {
      await page.getByTestId(`tab-${t}`).click()
      const panel = page.getByTestId('error-panel').first()
      await expect(panel).toBeVisible({ timeout: 15_000 })
      await expect(panel).toContainText('Retrying automatically')
    }
  })
})
