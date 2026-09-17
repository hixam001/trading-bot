import { expect, test, type Page } from '@playwright/test'

// Hermetic UI fixtures: run against a static frontend, NEVER start the engine.
const safety = {
  armed: true, kill_switch: { engaged: false, reason: '' },
  daily_loss_breaker: { engaged: false, limit_usd: 75, realized_pnl_today_usd: -20, headroom_usd: 55 },
  blocklist: { blocks: 0, auto: 0, manual: 0, latest: null },
  break: { on_break: false, reason: '', break_until_epoch: 0 }, alerts: [],
}
const events = [
  { id: 3, symbol: 'APPROVED', verdict: 'pass', failed_rule_ids: [] },
  { id: 2, symbol: 'DECLINED', verdict: 'fail', failed_rule_ids: [] },
  { id: 1, symbol: 'GATED', verdict: 'fail', failed_rule_ids: ['liquidity_floor'] },
].map(row => ({ ...row, ts: '2026-09-17T12:00:00Z', mint_address: row.symbol,
  candidate_snapshot: {}, rule_breakdown: [], grounding_flags: [],
  thesis: 'Fixture decision', regime_ok: true, narration_source: 'deepseek' }))

async function mockApi(page: Page, alerts: object[] = [], snapshots: object[] = []) {
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    const body = path === '/api/safety' ? { ...safety, alerts }
      : path === '/api/feed' ? { events }
      : path === '/api/system-status' ? { main_llm_reachable: true, main_llm_provider: 'deepseek',
        narration_mode: 'live', provider_calls_today: [], llm_usage_recent: [], tick_interval_seconds: 300 }
      : path === '/api/funnel' ? { candidates_seen: 3, candidates_seen_total: 3,
        gate_refused: 1, gate_passed: 2, model_refused: 1, model_approved: 1, filled: 1,
        model_refusal_rate_of_gate_passers: 0.5, window: { limit: 1000, feed_events: 3 } }
      : path === '/api/funnel/snapshots' ? { snapshots, count: snapshots.length }
      : path === '/api/live/executions' ? { enabled: true, commits: [], records: [] }
      : path === '/api/market-regime' ? { regimes: [] }
      : null
    await route.fulfill({ status: body ? 200 : 503, json: body ?? { detail: 'fixture unavailable' } })
  })
  await page.routeWebSocket('**/ws/feed', () => {})
}

test('quiet safety produces no strip; actionable alerts survive every view', async ({ page }) => {
  await mockApi(page)
  await page.goto('/')
  await page.getByTestId('tab-system').click()
  await expect(page.getByTestId('safety-state')).toBeVisible()
  await expect(page.getByTestId('alert-strip')).toHaveCount(0)
  await expect(page.getByTestId('safety-state')).toContainText('watching')
  await expect(page.getByTestId('safety-state').getByRole('button')).toHaveCount(0)
  await mockApi(page, [{ id: 'kill_switch', severity: 'fail', message: 'kill switch engaged' }])
  await page.reload()
  for (const tab of ['dashboard', 'holdings', 'journal', 'market', 'system']) {
    await page.getByTestId(`tab-${tab}`).click()
    await expect(page.getByTestId('alert-kill_switch')).toBeVisible()
  }
})

test('failed safety poll is not all clear; offline banner still renders', async ({ page }) => {
  await mockApi(page)
  await page.route('**/api/safety', route => route.fulfill({ status: 503, json: {} }))
  await page.route('**/api/system-status', route => route.fulfill({ status: 503, json: {} }))
  await page.goto('/')
  await expect(page.getByTestId('alert-safety_unavailable')).toContainText('cannot confirm')
  await expect(page.getByTestId('offline-banner')).toBeVisible()
})

test('funnel stages drill into the exact classification, fills into journal', async ({ page }) => {
  await mockApi(page)
  await page.goto('/')
  for (const [stage, symbols] of [
    ['gate_passed', ['APPROVED', 'DECLINED']], ['gate_refused', ['GATED']],
    ['model_refused', ['DECLINED']], ['model_approved', ['APPROVED']],
  ] as const) {
    await page.getByTestId('tab-system').click()
    await page.getByTestId(`funnel-stage-${stage}`).click()
    const rows = page.getByTestId('live-feed').locator('button[aria-expanded]')
    await expect(rows).toHaveCount(symbols.length)
    for (const symbol of symbols) await expect(page.getByTestId('live-feed')).toContainText(symbol)
  }
  await page.getByTestId('tab-system').click()
  await page.getByTestId('funnel-stage-filled').click()
  await expect(page.getByTestId('journal-filter-note')).toContainText('bound')
})

test('trend requires measured points and tape marks the stored seen boundary', async ({ page }) => {
  await mockApi(page)
  await page.addInitScript(() => sessionStorage.setItem('feed:lastSeenId', '1'))
  await page.goto('/')
  await expect(page.getByTestId('live-feed').getByRole('separator')).toBeVisible()
  await page.getByTestId('tab-system').click()
  await expect(page.getByTestId('funnel-trend-empty')).toBeVisible()
  await mockApi(page, [], [
    { id: 1, ts: '2026-09-17T12:00:00Z', model_refusal_rate: 0.5 },
    { id: 2, ts: '2026-09-17T12:05:00Z', model_refusal_rate: 0.75 },
  ])
  await page.reload()
  await page.getByTestId('tab-system').click()
  await expect(page.getByTestId('funnel-trend').locator('svg')).toBeVisible()
})
