import { expect, test } from '@playwright/test'

/**
 * The browser login against a real identity provider (Keycloak on the lab).
 *
 * The password grant the acceptance harness uses proves netCI verifies tokens; it does
 * not prove a person can sign in. This does: the Portal sends the browser to the
 * provider with a PKCE challenge, the person types their password *there*, and the
 * Portal comes back with an id_token that netCI accepts -- roles included.
 *
 * Skips itself, loudly, when the API is not in OIDC mode: a green run must mean the
 * flow was exercised. Requires NETCI_E2E_OIDC_USER / NETCI_E2E_OIDC_PASSWORD.
 */

const apiUrl = process.env.NETCI_API_URL ?? 'http://127.0.0.1:8100'
const user = process.env.NETCI_E2E_OIDC_USER ?? ''
const password = process.env.NETCI_E2E_OIDC_PASSWORD ?? ''

test('a person signs in through the identity provider and netCI decides their roles', async ({ page, request }) => {
  const config = await (await request.get(`${apiUrl}/auth/config`)).json()
  test.skip(config.authMode !== 'oidc' || !config.oidc, `API auth mode is ${config.authMode}; this test needs oidc`)
  test.skip(!user || !password, 'set NETCI_E2E_OIDC_USER and NETCI_E2E_OIDC_PASSWORD')

  await page.goto('/')
  const sso = page.getByTestId('sso-login')
  await expect(sso).toBeVisible()
  // Nothing is signed in yet: the shell must not be there.
  await expect(page.getByRole('button', { name: /Dashboard/i })).toHaveCount(0)

  await sso.click()
  // Now at the provider. The URL is the provider's authorization endpoint, and the
  // request carries a PKCE challenge, never a secret.
  await page.waitForURL((url) => url.href.startsWith(config.oidc.issuer))
  const providerUrl = new URL(page.url())
  expect(providerUrl.origin).toBe(new URL(config.oidc.authorizationEndpoint).origin)
  const sessionCookies = await page.context().cookies()
  expect(sessionCookies.length).toBeGreaterThan(0)

  await page.locator('#username').fill(user)
  await page.locator('#password').fill(password)
  await page.locator('#kc-login').click()

  // Back in the Portal, signed in as the server describes us; code and state are gone
  // from the address bar.
  await expect(page.getByRole('button', { name: /Dashboard/i })).toBeVisible({ timeout: 20_000 })
  expect(new URL(page.url()).searchParams.has('code')).toBe(false)
  expect(new URL(page.url()).searchParams.has('state')).toBe(false)

  // What the Portal holds is an id_token the API accepts, and the identity is the API's
  // answer, not something the browser assembled.
  const token = await page.evaluate(() => window.sessionStorage.getItem('netci.auth.token'))
  expect(token).toBeTruthy()
  const me = await request.get(`${apiUrl}/me`, { headers: { Authorization: `Bearer ${token}` } })
  expect(me.status()).toBe(200)
  const identity = await me.json()
  expect(identity.authMode).toBe('oidc')
  expect(identity.principal.method).toBe('oidc')
  expect(identity.principal.roles.length).toBeGreaterThan(0)
  await expect(page.getByText(identity.principal.displayName)).toBeVisible()
  // The PKCE material is spent.
  expect(await page.evaluate(() => window.sessionStorage.getItem('netci.oidc.verifier'))).toBeNull()
})

test('a forged callback is refused before any token exchange', async ({ page, request }) => {
  const config = await (await request.get(`${apiUrl}/auth/config`)).json()
  test.skip(config.authMode !== 'oidc' || !config.oidc, `API auth mode is ${config.authMode}; this test needs oidc`)

  let tokenEndpointCalls = 0
  await page.route(`${config.oidc.tokenEndpoint}*`, (route) => { tokenEndpointCalls += 1; return route.continue() })
  await page.goto('/?code=stolen&state=not-ours')
  await expect(page.getByRole('alert')).toContainText(/state mismatch/i)
  await expect(page.getByRole('button', { name: /Dashboard/i })).toHaveCount(0)
  expect(tokenEndpointCalls).toBe(0)
})

test('signing out ends the session at the provider: the next sign-in asks for the password again', async ({ page, request }) => {
  const config = await (await request.get(`${apiUrl}/auth/config`)).json()
  test.skip(config.authMode !== 'oidc' || !config.oidc, `API auth mode is ${config.authMode}; this test needs oidc`)
  test.skip(!user || !password, 'set NETCI_E2E_OIDC_USER and NETCI_E2E_OIDC_PASSWORD')

  await page.goto('/')
  await page.getByTestId('sso-login').click()
  await page.waitForURL((url) => url.href.startsWith(config.oidc.issuer))
  await page.locator('#username').fill(user)
  await page.locator('#password').fill(password)
  await page.locator('#kc-login').click()
  await expect(page.getByRole('button', { name: /Dashboard/i })).toBeVisible({ timeout: 20_000 })

  // Control: with the provider session alive, a second sign-in needs no password.
  // (A fresh tab in the same context shares the provider's cookie.)
  const second = await page.context().newPage()
  await second.goto('/')
  await second.getByTestId('sso-login').click()
  await expect(second.getByRole('button', { name: /Dashboard/i })).toBeVisible({ timeout: 20_000 })
  expect(second.url().startsWith(config.oidc.issuer)).toBe(false)
  await second.close()

  // Sign out from the Portal: the browser is sent to the provider's end-session
  // endpoint and comes back to the login page.
  const endSession = page.waitForRequest((req) => req.url().startsWith(config.oidc.endSessionEndpoint))
  await page.getByTitle(/Đăng xuất/).first().click()
  const logoutRequest = await endSession
  expect(new URL(logoutRequest.url()).searchParams.get('id_token_hint')).toBeTruthy()
  await expect(page.getByTestId('sso-login')).toBeVisible({ timeout: 20_000 })
  expect(await page.evaluate(() => window.sessionStorage.getItem('netci.auth.token'))).toBeNull()

  // Now the provider asks for the password again: the session there is gone.
  await page.getByTestId('sso-login').click()
  await page.waitForURL((url) => url.href.startsWith(config.oidc.issuer))
  await expect(page.locator('#kc-login')).toBeVisible()
})
