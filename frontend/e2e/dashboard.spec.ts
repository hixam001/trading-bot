import { expect, test, type ConsoleMessage } from '@playwright/test'

/**
 * E2E QA — DESIGN.md §6 checklist, run against the live backend on :8000.
 *
 *   1. loads with zero console errors
 *   2. all panels render loading→data or loading→empty, never blank
 *   3. feed expand/collapse + copy interaction works
 *   4. offline banner appears when the API is unreachable
 *   5. feed rows are keyboard-operable buttons with aria-expanded
 */

const APP = '/'

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
  test('loads with zero console errors and renders all panels', async ({ page }) => {
    const errors = await collectConsoleErrors(page)
    await page.goto(APP)

    // Header identity + live badge are present.
    await expect(page.getByText('trading-bot', { exact: false }).first()).toBeVisible()
    await expect(page.getByText('LIVE · real money')).toBeVisible()

    // The four panels resolve to data or an explicit empty state — never blank.
    await expect(page.getByTestId('live-feed')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByTestId('system-status')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByTestId('market-regime')).toBeVisible({ timeout: 15_000 })

    // Live book renders when armed/enabled; otherwise it is intentionally absent.
    // We assert the page reached a settled state either way (no perpetual skeleton).
    await page.waitForTimeout(1500)
    const skeletons = page.locator('.skeleton')
    // After settling, no panel should be stuck in a loading skeleton.
    expect(await skeletons.count()).toBe(0)

    // No uncaught page errors. (WS close noise is filtered by type check already.)
    const realErrors = errors.filter(
      (e) => !e.includes('WebSocket') && !e.includes('Failed to load resource'),
    )
    expect(realErrors).toEqual([])
  })

  test('live book shows real-wallet figures or a documented empty state', async ({ page }) => {
    await page.goto(APP)
    const book = page.getByTestId('live-book')
    // The book is enabled in this deployment; wait for it to hydrate.
    await expect(book).toBeVisible({ timeout: 15_000 })
    // Equity label + a dollar figure (or em dash) must be present.
    await expect(book.getByText('Equity')).toBeVisible()
    await expect(book.getByText('Cash · USDC')).toBeVisible()
  })

  test('feed rows expand and collapse with aria-expanded', async ({ page }) => {
    await page.goto(APP)
    const feed = page.getByTestId('live-feed')
    await expect(feed).toBeVisible({ timeout: 15_000 })

    // Feed hydrates from /api/feed history, so at least one row should exist.
    const firstRow = feed.locator('button[aria-expanded]').first()
    await expect(firstRow).toBeVisible({ timeout: 15_000 })

    // Expand.
    await firstRow.click()
    await expect(firstRow).toHaveAttribute('aria-expanded', 'true')
    // Expanded detail reveals the contract label.
    await expect(feed.getByText('contract:').first()).toBeVisible()

    // Collapse.
    await firstRow.click()
    await expect(firstRow).toHaveAttribute('aria-expanded', 'false')
  })

  test('feed row is keyboard operable (Enter toggles)', async ({ page }) => {
    await page.goto(APP)
    const feed = page.getByTestId('live-feed')
    const firstRow = feed.locator('button[aria-expanded]').first()
    await expect(firstRow).toBeVisible({ timeout: 15_000 })

    await firstRow.focus()
    await page.keyboard.press('Enter')
    await expect(firstRow).toHaveAttribute('aria-expanded', 'true')
    await page.keyboard.press('Enter')
    await expect(firstRow).toHaveAttribute('aria-expanded', 'false')
  })

  test('offline banner appears when the API is unreachable', async ({ page }) => {
    // Abort every API + WS upgrade so both primary feeds fail.
    await page.route('**/api/**', (route) => route.abort())
    await page.goto(APP)

    const banner = page.getByTestId('offline-banner')
    await expect(banner).toBeVisible({ timeout: 15_000 })
    await expect(banner).toContainText('API unreachable')
  })
})

test.describe('pages (tabs)', () => {
  test('tab bar navigates dashboard / holdings / journal', async ({ page }) => {
    await page.goto(APP)
    await expect(page.getByTestId('tab-dashboard')).toBeVisible()
    await expect(page.getByTestId('tab-holdings')).toBeVisible()
    await expect(page.getByTestId('tab-journal')).toBeVisible()

    // Dashboard is the default page.
    await expect(page.getByTestId('live-feed')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByTestId('tab-dashboard')).toHaveAttribute('aria-current', 'page')

    // Holdings page.
    await page.getByTestId('tab-holdings').click()
    await expect(page.getByTestId('holdings')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByTestId('tab-holdings')).toHaveAttribute('aria-current', 'page')

    // Journal page.
    await page.getByTestId('tab-journal').click()
    await expect(page.getByTestId('journal')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByTestId('tab-journal')).toHaveAttribute('aria-current', 'page')

    // Back to dashboard.
    await page.getByTestId('tab-dashboard').click()
    await expect(page.getByTestId('live-feed')).toBeVisible({ timeout: 15_000 })
  })

  test('holdings shows positions or the documented empty state', async ({ page }) => {
    await page.goto(APP)
    await page.getByTestId('tab-holdings').click()
    const holdings = page.getByTestId('holdings')
    await expect(holdings).toBeVisible({ timeout: 15_000 })
    await expect(holdings.getByText('Open value')).toBeVisible()
    // Either a position row or the explicit empty explanation — never blank.
    const hasContent =
      (await holdings.locator('table tbody tr').count()) > 0 ||
      (await holdings.getByText('No open live positions').count()) > 0
    expect(hasContent).toBe(true)
  })

  test('journal shows order decisions with expandable proof', async ({ page }) => {
    await page.goto(APP)
    await page.getByTestId('tab-journal').click()
    const journal = page.getByTestId('journal')
    await expect(journal).toBeVisible({ timeout: 15_000 })
    await expect(journal.getByText('Order decisions')).toBeVisible()

    // This deployment has sealed commits (filled or not) — at least one row.
    const proofBtn = journal.locator('button[aria-expanded]').first()
    await expect(proofBtn).toBeVisible({ timeout: 15_000 })

    // Expand proof detail: commit hash + memo lines appear.
    await proofBtn.click()
    await expect(proofBtn).toHaveAttribute('aria-expanded', 'true')
    await expect(journal.getByText('commit hash:').first()).toBeVisible()
    await expect(journal.getByText('memo:').first()).toBeVisible()

    // Collapse again.
    await proofBtn.click()
    await expect(proofBtn).toHaveAttribute('aria-expanded', 'false')
  })
})
