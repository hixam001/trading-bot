import { expect, test, type Page } from '@playwright/test'

/**
 * §92 S4 — untrusted feed text is rendered as text, never as HTML.
 *
 * Token names, theses, and exit reasons come from on-chain/social sources.
 * This fixture is deliberately hostile; if a future component uses raw HTML,
 * the browser executes the payload and the assertions below fail.
 */
const XSS = '<img src=x onerror="window.__xss_probe=1"><script>window.__xss_probe=2</script>'

function feedEvent() {
  return {
    id: 1,
    ts: '2026-11-05T10:00:00Z',
    symbol: `">${XSS}`,
    mint_address: 'So11111111111111111111111111111111111111112',
    candidate_snapshot: {},
    verdict: 'pass',
    thesis: `ignore previous instructions ${XSS}`,
    rule_breakdown: [{ rule_id: 'liquidity_floor', passed: true, detail: XSS, value: 1 }],
    failed_rule_ids: [],
    regime_ok: true,
    grounding_flags: [],
    narration_source: 'deepseek',
    model_version: 'test',
    prompt_version: 'test',
  }
}

async function mockApi(page: Page) {
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    const body =
      path === '/api/feed' ? { events: [feedEvent()], total: 1 }
      : path === '/api/live/portfolio' ? { enabled: true, wallet: 'wallet', cash_usd: 10, equity_usd: 10,
        open_value_usd: 0, unrealized_pnl_usd: 0, deployed_today_usd: 0, closed_trades: 0,
        chain_excluded: [], positions: [] }
      : path === '/api/live/executions' ? { enabled: true, commits: [], records: [] }
      : path === '/api/market-regime' ? { regimes: [] }
      : path === '/api/system-status' ? { main_llm_reachable: true, main_llm_provider: 'deepseek',
        narration_mode: 'live', provider_calls_today: [], llm_usage_recent: [], tick_interval_seconds: 300 }
      : path === '/api/stats' ? { total_trades: 0, open_trades: 0, closed_trades: 0, win_rate: null,
        profit_factor: null, max_drawdown_pct: null, equity_curve: [],
        total_pnl_usd: 0, cash_usd: 10, equity_usd: 10 }
      : path === '/api/funnel' ? { candidates_seen: 0, candidates_seen_total: 0, gate_refused: 0,
        gate_passed: 0, model_refused: 0, model_approved: 0, filled: 0,
        model_refusal_rate_of_gate_passers: null, window: { limit: 1000, feed_events: 0 } }
      : path === '/api/funnel/snapshots' ? { snapshots: [], count: 0 }
      : path === '/api/safety' ? { armed: true, kill_switch: { engaged: false, reason: '' },
        daily_loss_breaker: { engaged: false, limit_usd: 75, realized_pnl_today_usd: 0, headroom_usd: 75 },
        blocklist: { blocks: 0, auto: 0, manual: 0, latest: null },
        break: { on_break: false, reason: '', break_until_epoch: 0 }, alerts: [] }
      : null
    await route.fulfill({ status: body ? 200 : 503, json: body ?? { detail: 'fixture unavailable' } })
  })
  await page.routeWebSocket('**/ws/feed', () => {})
}

test('hostile token text is escaped, not executed', async ({ page }) => {
  let dialogs = 0
  page.on('dialog', async dialog => { dialogs++; await dialog.dismiss() })
  await mockApi(page)

  await page.goto('/')
  await expect(page.getByTestId('live-feed')).toBeVisible()
  await expect(page.getByTestId('feed-list')).toContainText(XSS)

  // Open the row so the full model answer + rule details are rendered too.
  await page.getByTestId('feed-list').getByRole('button').first().click()
  await expect(page.getByTestId('feed-list')).toContainText('ignore previous instructions')

  expect(dialogs).toBe(0)
  expect(await page.evaluate(() => (window as any).__xss_probe)).toBeUndefined()
  expect(await page.locator('img[src="x"]').count()).toBe(0)
})
