import { defineConfig, devices } from '@playwright/test'

/**
 * Browser tests for the Portal, against a real netCI API.
 *
 * The unit tests mock `netciClient`, so they prove the components behave given an answer.
 * They cannot prove the Portal and the API agree about that answer -- a renamed field, a
 * changed status code or an endpoint that now needs a role all pass the unit suite and
 * fail in front of a user. That gap is what these tests cover, which is why there is no
 * mocking here and no fixture server.
 *
 * NETCI_API_URL must point at a running API (`make lab-up`). Vite proxies /api to it, so
 * the browser talks to the same origin the Portal is served from, exactly as in a real
 * deployment.
 */
const apiUrl = process.env.NETCI_API_URL ?? 'http://127.0.0.1:8100'
const port = Number(process.env.NETCI_PORTAL_PORT ?? 4173)

export default defineConfig({
  testDir: './e2e',
  // Every expectation is about a real network round trip; the defaults are tuned for
  // mocked pages and produce flaky failures against a real API under load.
  timeout: 60_000,
  expect: { timeout: 10_000 },
  // A test that only passes on the second attempt is a test that has told you something.
  // Retries here would hide exactly the Portal/API disagreements this suite exists for.
  retries: 0,
  workers: 1,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : [['list']],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    // `preview` serves the production build: the thing that actually ships, minified and
    // tree-shaken, rather than the dev server's instrumented output.
    command: `npm run build && npm run preview -- --port ${port} --strictPort`,
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
    env: { NETCI_API_URL: apiUrl },
  },
})
