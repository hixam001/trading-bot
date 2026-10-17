import { expect, test } from '@playwright/test'

/**
 * §63 E2E — operator upgrades, run against the live backend on :8000.
 *
 *   1. command palette opens on Ctrl/Cmd-K, closes on Esc
 *   2. palette navigates to a view and is read-only (no state writes)
 *   3. palette journal filter reaches the journal view with a filter note
 *   4. refusal funnel renders on the system view
 *   5. feed j/k/enter/esc cursor navigation (when rows exist)
 *
 * Live-data tolerant throughout: same row-or-empty discipline as
 * dashboard.spec.ts — the feed legitimately empties between cycles.
 */

const APP = '/'

test.describe('command palette (§63.1)', () => {
  test('opens on Ctrl-K, closes on Esc, and is a modal dialog', async ({ page }) => {
    await page.goto(APP)
    await expect(page.getByTestId('live-feed')).toBeVisible({ timeout: 15_000 })

    await page.keyboard.press('Control+k')
    const dialog = page.getByRole('dialog', { name: 'command palette' })
    await expect(dialog).toBeVisible()
    await expect(page.getByTestId('palette-input')).toBeFocused()
    // A11y contract: modal semantics while open.
    await expect(dialog).toHaveAttribute('aria-modal', 'true')

    await page.keyboard.press('Escape')
    await expect(dialog).toBeHidden()
  })

  test('navigates to a view via keyboard only (read-only palette)', async ({ page }) => {
    await page.goto(APP)
    await page.keyboard.press('Control+k')
    await page.getByTestId('palette-input').fill('go: system')
    await page.keyboard.press('Enter')

    await expect(page.getByTestId('system-status')).toBeVisible({ timeout: 15_000 })
    // Palette closed itself after running the command.
    await expect(page.getByRole('dialog', { name: 'command palette' })).toBeHidden()
  })

  test('journal filter command reaches the journal view with a filter note', async ({
    page,
  }) => {
    await page.goto(APP)
    await page.keyboard.press('Control+k')
    await page.getByTestId('palette-input').fill('journal: search demo')
    await page.keyboard.press('Enter')

    const journal = page.getByTestId('journal')
    await expect(journal).toBeVisible({ timeout: 15_000 })
    await expect(page.getByTestId('journal-filter-note')).toContainText('demo')
    // Clearing restores the unfiltered view.
    await page.getByRole('button', { name: 'show all' }).click()
    await expect(page.getByTestId('journal-filter-note')).toBeHidden()
  })
})

test.describe('refusal funnel (§63.2)', () => {
  test('renders on the system view with deterministic fixture', async ({ page }) => {
    await page.route('**/api/funnel*', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          period: '24h',
          funnel: {
            candidates_seen: 100,
            gate_passed: 20,
            model_approved: 5,
            filled: 3,
            gate_refused: 80,
            model_refused: 15,
            model_refusal_rate_of_gate_passers: 0.75,
            candidates_seen_total: 100,
          },
        }),
      }),
    )
    await page.goto(APP)
    await page.getByTestId('tab-system').click()
    const stages = page.getByTestId('funnel-stages')
    await expect(stages).toBeVisible({ timeout: 15_000 })
    await expect(stages.getByText('candidates seen')).toBeVisible()
    await expect(stages.getByTestId('funnel-stage-gate_passed')).toBeVisible()
    await expect(stages.getByText('model approved')).toBeVisible()
    await expect(stages.getByText('filled')).toBeVisible()
    await expect(stages.getByText('model refusal · share of gate-passers')).toBeVisible()
  })
})

test.describe('feed keyboard navigation (§63.4)', () => {
  test('j/k move the cursor, enter expands, esc collapses', async ({ page }) => {
    await page.route('**/api/feed*', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          total: 2,
          limit: 50,
          offset: 0,
          events: [
            {
              id: 1,
              ts: '2026-10-17T12:00:00Z',
              symbol: 'SOL',
              mint_address: 'So11111111111111111111111111111111111111112',
              candidate_snapshot: {},
              verdict: 'pass',
              thesis: 'SOL thesis',
              rule_breakdown: [],
              failed_rule_ids: [],
              regime_ok: true,
              grounding_flags: [],
              narration_source: 'model',
              led_to_trade_id: null,
            },
            {
              id: 2,
              ts: '2026-10-17T12:01:00Z',
              symbol: 'BONK',
              mint_address: 'DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263',
              candidate_snapshot: {},
              verdict: 'pass',
              thesis: 'BONK thesis',
              rule_breakdown: [],
              failed_rule_ids: [],
              regime_ok: true,
              grounding_flags: [],
              narration_source: 'model',
              led_to_trade_id: null,
            },
          ],
        }),
      }),
    )
    await page.goto(APP)
    const feed = page.getByTestId('live-feed')
    const list = page.getByTestId('feed-list')
    await expect(feed).toBeVisible({ timeout: 15_000 })

    const rows = feed.locator('button[aria-expanded]')
    await expect(rows.first()).toBeVisible({ timeout: 15_000 })
    expect(await rows.count()).toBeGreaterThanOrEqual(2)

    await list.focus()
    await page.keyboard.press('j')
    await expect(list.locator('[data-feed-row="1"]')).toHaveAttribute('aria-selected', 'true')
    await page.keyboard.press('Enter')
    const cursorRowButton = list
      .locator('[data-feed-row="1"]')
      .locator('button[aria-expanded]')
    await expect(cursorRowButton).toHaveAttribute('aria-expanded', 'true')
    await page.keyboard.press('Escape')
    await expect(cursorRowButton).toHaveAttribute('aria-expanded', 'false')
  })
})
