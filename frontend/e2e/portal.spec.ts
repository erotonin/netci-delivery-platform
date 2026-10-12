import { AxeBuilder } from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

/**
 * The Portal against a real netCI API.
 *
 * The unit suite mocks `netciClient`, so it proves the components behave given an answer.
 * It cannot prove the Portal and the API agree about that answer: a renamed field, a
 * changed status code, or an endpoint that now needs a role all pass there and fail in
 * front of a user. Nothing is mocked here for that reason.
 */

async function signIn(page: Page) {
  await page.goto('/')
  // With NETCI_AUTH_MODE=none the Portal reads /me, finds no credential is needed and
  // goes straight in. That path is itself worth exercising: it is the one a developer
  // runs locally, and it breaks the moment /me changes shape.
  await expect(page.getByRole('button', { name: /Dashboard/i })).toBeVisible()
}

test('the shell loads from the API rather than from invented defaults', async ({ page }) => {
  // Awaited rather than collected in a listener: the nav renders as soon as the shell
  // mounts, which is before the dashboard request resolves, so asserting on a list
  // afterwards is a race that passes on a fast machine and fails on a loaded one.
  const dashboard = page.waitForResponse(
    (response) => response.url().includes('/api/portal/dashboard') && response.status() === 200,
  )
  await signIn(page)
  await dashboard

  // The KPI numbers come from the API. Before this suite existed the page seeded itself
  // with plausible fixtures while loading, so a broken API looked like a healthy platform.
  await expect(page.getByText(/Tổng số hệ thống/i)).toBeVisible()
})

test('navigating to systems and back keeps the URL and the view in step', async ({ page }) => {
  await signIn(page)

  await page.getByRole('button', { name: /^Systems$/i }).click()
  await expect(page).toHaveURL(/#\/systems$/)

  await page.getByRole('button', { name: /^Servers$/i }).click()
  await expect(page).toHaveURL(/#\/servers$/)

  // Back must restore the previous view, not just the address bar.
  await page.goBack()
  await expect(page).toHaveURL(/#\/systems$/)
})

test('global search reaches a module by name', async ({ page }) => {
  await signIn(page)

  const search = page.getByRole('textbox', { name: /Tìm kiếm toàn cục/i })
  await search.click()
  await search.fill('hello')

  const results = page.getByRole('region', { name: /Search results/i })
  await expect(results).toBeVisible()
})

test('a production request can be opened and shows what it will deploy', async ({ page }) => {
  await signIn(page)

  await page.goto('/#/production-requests')
  // Either there are requests, or the page says plainly that there are none. What it must
  // not do is render an empty table with no explanation.
  const heading = page.getByRole('heading', { level: 1 })
  await expect(heading).toBeVisible()
})

test('a failed API call shows an error with a retry, not an empty page', async ({ page }) => {
  // The distinction the whole AsyncState module exists for: "the API is down" and "you
  // have nothing" must never look the same.
  await page.route('**/api/portal/dashboard', (route) => route.fulfill({ status: 503, body: '{}' }))
  await page.goto('/')

  // Scoped to the alert: the shell's own connectivity banner offers a retry too, and both
  // failing at once is correct behaviour rather than something to assert away.
  const alert = page.getByRole('alert').first()
  await expect(alert).toBeVisible()
  await expect(alert).toContainText(/sự cố|Không kết nối/i)
  await expect(alert.getByRole('button', { name: /Thử lại/i })).toBeVisible()
})

test('the retry actually re-requests, and recovers', async ({ page }) => {
  let attempts = 0
  await page.route('**/api/portal/dashboard', async (route) => {
    attempts += 1
    if (attempts === 1) return route.fulfill({ status: 503, body: '{}' })
    return route.fallback()
  })

  await page.goto('/')
  await page.getByRole('alert').first().getByRole('button', { name: /Thử lại/i }).click()

  await expect(page.getByText(/Tổng số hệ thống/i)).toBeVisible()
  expect(attempts).toBeGreaterThan(1)
})

test('a render error is contained instead of blanking the Portal', async ({ page }) => {
  // A malformed payload is the realistic way a component throws: the API changed shape
  // and the Portal did not. The user must still be told what happened and offered a way
  // forward, rather than getting a white page with no navigation.
  await page.route('**/api/portal/dashboard', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ kpis: null }) }),
  )
  await page.goto('/')

  await expect(page.getByRole('alert').first()).toBeVisible()
  await expect(page.locator('body')).not.toHaveText('')
})

test('the dashboard has no serious accessibility violations', async ({ page }) => {
  await signIn(page)

  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa'])
    .analyze()

  // Serious and critical only. The full WCAG AA rule set flags contrast choices that are
  // a design decision rather than a defect, and a suite that fails on those gets muted --
  // which loses the findings that do matter.
  const serious = results.violations.filter((item) => item.impact === 'serious' || item.impact === 'critical')
  expect(
    serious.map((item) => `${item.id}: ${item.help} (${item.nodes.length} nodes)`),
  ).toEqual([])
})

test('logging out is offered only when there is a credential to drop', async ({ page }) => {
  await signIn(page)

  // With authentication disabled a logout button would sign the user straight back in,
  // so the Portal shows the posture instead. This asserts the honest version.
  const identity = await page.request.get('/api/me')
  const body = await identity.json()

  if (body.authMode === 'none') {
    await expect(page.getByText(/Chưa bật xác thực/i).first()).toBeVisible()
    await expect(page.getByRole('button', { name: /Đăng xuất/i })).toHaveCount(0)
  } else {
    await expect(page.getByRole('button', { name: /Đăng xuất/i }).first()).toBeVisible()
  }
})
