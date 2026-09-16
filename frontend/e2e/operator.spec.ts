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
  test('renders on the system view', async ({ page }) => {
    await page.goto(APP)
    await page.getByTestId('tab-system').click()
    // Either the funnel (server counts) or its error panel — never blank,
    // and never a client-side approximation of the numbers.
    const stages = page.getByTestId('funnel-stages')
    const failed = page.getByText('Refusal funnel', { exact: true }).first()
    await expect(stages.or(failed).first()).toBeVisible({ timeout: 15_000 })
    if (await stages.isVisible()) {
      // All four stages present, in order, with the model-refusal rate line.
      await expect(stages.getByText('candidates seen')).toBeVisible()
      await expect(stages.getByText('gate passed')).toBeVisible()
      await expect(stages.getByText('model approved')).toBeVisible()
      await expect(stages.getByText('filled')).toBeVisible()
      await expect(stages.getByText('model refusal · share of gate-passers')).toBeVisible()
    }
  })
})

test.describe('feed keyboard navigation (§63.4)', () => {
  test('j/k move the cursor, enter expands, esc collapses', async ({ page }) => {
    await page.goto(APP)
    const feed = page.getByTestId('live-feed')
    const list = page.getByTestId('feed-list')
    await expect(feed).toBeVisible({ timeout: 15_000 })

    const firstRow = feed.locator('button[aria-expanded]').first()
    const emptyState = feed.getByText('No decisions yet', { exact: false })
    await Promise.race([
      firstRow.waitFor({ state: 'visible', timeout: 20_000 }),
      emptyState.waitFor({ state: 'visible', timeout: 20_000 }),
    ]).catch(() => undefined)

    if ((await firstRow.count()) > 1 && (await firstRow.isVisible())) {
      await list.focus()
      await page.keyboard.press('j')
      // Cursor moved to row 1 (0-indexed): it carries the selection marker.
      await expect(list.locator('[data-feed-row="1"]')).toHaveAttribute('aria-selected', 'true')
      await page.keyboard.press('Enter')
      // Enter expands the cursor row: its own button reports aria-expanded.
      const cursorRowButton = list
        .locator('[data-feed-row="1"]')
        .locator('button[aria-expanded]')
      await expect(cursorRowButton).toHaveAttribute('aria-expanded', 'true')
      await page.keyboard.press('Escape')
      await expect(cursorRowButton).toHaveAttribute('aria-expanded', 'false')
    } else {
      await expect(emptyState).toBeVisible()
    }
  })
})
