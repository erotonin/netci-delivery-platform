import { defineConfig, devices } from '@playwright/test'

/**
 * The same browser tests, against netCI on the company-shaped lab (infra/corp): the Portal
 * at https://netci.corp.local through the ingress, TLS from the lab CA.
 *
 * Chromium reaches the name through --host-resolver-rules (the lab has no DNS) and trusts
 * exactly the netci certificate by its public-key pin (NETCI_CORP_SPKI) -- certificate
 * checking stays on for everything else. NETCI_API_URL is the API for the Node-side calls.
 *
 *   NETCI_CORP_SPKI=<base64 sha256> \
 *   NETCI_API_URL=http://127.0.0.1:18100 npx playwright test -c playwright.corp.config.ts e2e/oidc-login.spec.ts
 */
const ingress = process.env.NETCI_CORP_INGRESS_IP ?? '172.17.255.200'  // the ingress VIP
const spki = process.env.NETCI_CORP_SPKI ?? ''

export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  retries: 0,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: 'https://netci.corp.local',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    launchOptions: {
      args: [`--host-resolver-rules=MAP netci.corp.local ${ingress}`, `--ignore-certificate-errors-spki-list=${spki}`],
    },
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})
