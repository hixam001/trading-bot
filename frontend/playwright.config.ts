import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright E2E config — runs against the already-running backend on :8000
 * (start.sh keeps it up; it serves the built frontend). We do NOT spawn the
 * Python backend from here: it is a long-lived service with live provider
 * credentials, and the QA goal is the real dashboard, not a synthetic one.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: [['list']],
  timeout: 30_000,
  use: {
    baseURL: process.env.BASE_URL ?? 'http://localhost:8000',
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
