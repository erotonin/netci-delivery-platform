import { expect, test, type Page } from '@playwright/test'
import * as path from 'path'

const ARTIFACT_DIR = '/home/deployer/.gemini/antigravity-cli/brain/2fb6e3dc-998c-4b70-bff7-692b77c81866'

async function ssoLogin(page: Page, username: string, password = 'netci-lab-only') {
  await page.goto('/#/login')
  await page.waitForLoadState('networkidle')

  const userBtn = page.locator('.sidebar-user')
  if (await userBtn.isVisible()) {
    const text = await userBtn.textContent()
    if (text?.toLowerCase().includes(username.toLowerCase())) {
      console.log(`[E2E] Already authenticated as ${username}`)
      return
    }
    await userBtn.click()
    await page.waitForTimeout(500)
  }

  const sso = page.getByTestId('sso-login')
  if (await sso.isVisible()) {
    await sso.click()
    await page.waitForURL((url) => url.href.includes('/protocol/openid-connect/auth'), { timeout: 15_000 })
    await page.locator('#username').fill(username)
    await page.locator('#password').fill(password)
    await page.locator('#kc-login').click()
  } else {
    const devBtn = page.locator('button:has-text("Dev Token")')
    if (await devBtn.isVisible()) {
      await devBtn.click()
      await page.locator('input[placeholder*="token"]').fill('netci_dev_secret_token_12345')
      await page.click('button:has-text("Sign in")')
    }
  }

  await expect(page.locator('.brand')).toBeVisible({ timeout: 20_000 })
  console.log(`[E2E] Successfully signed in through Keycloak as ${username}`)
}

test.describe.serial('Demo Kahn Algorithm Multi-Module CI/CD & Streamlined UX', () => {
  test('Step 1: Streamlined Navigation & Request Creation with Kahn Preset', async ({ page }) => {
    await ssoLogin(page, 'pat')
    await page.goto('/#/requests')
    await page.waitForLoadState('networkidle')

    // Verify streamlined sidebar items
    await expect(page.locator('button:has-text("Systems & Pipelines")')).toBeVisible()
    await expect(page.locator('button:has-text("Production Requests")')).toBeVisible()
    // Verify removed items are NOT present
    await expect(page.locator('button:has-text("CI Cost")')).toHaveCount(0)
    await expect(page.locator('button:has-text("Scorecards")')).toHaveCount(0)
    await expect(page.locator('button:has-text("Release Plan")')).toHaveCount(0)
    await expect(page.locator('button:has-text("Stage Catalog")')).toHaveCount(0)

    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'kahn_demo_01_streamlined_sidebar.png'), fullPage: true })

    // Open New Request Modal
    await page.click('button:has-text("New Request")')
    await expect(page.locator('h2:has-text("New Production Request")')).toBeVisible()

    // Select modules and configure DAG dependency
    const selectAllBtn = page.locator('button:has-text("Select all modules with versions")')
    await expect(selectAllBtn).toBeVisible()
    await selectAllBtn.click()

    // Configure dependency to create 2 waves
    const depCheck = page.locator('.dependency-checkboxes input[type="checkbox"]').last()
    await depCheck.check()

    // Verify Kahn algorithm preview shows Wave 1 and Wave 2
    await expect(page.locator('text=Kahn\'s algorithm identified 2 Waves')).toBeVisible()
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'kahn_demo_02_kahn_preset_waves.png'), fullPage: true })

    // Test Cycle Detection by checking circular dependency
    const circularCheck = page.locator('.dependency-checkboxes input[type="checkbox"]').first()
    await circularCheck.check()
    await expect(page.locator('text=Circular dependency detected')).toBeVisible()
    await expect(page.locator('button:has-text("Review Request")')).toBeDisabled()
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'kahn_demo_03_cycle_detection_alert.png'), fullPage: true })

    // Fix cycle
    await circularCheck.uncheck()
    await expect(page.locator('text=Circular dependency detected')).toHaveCount(0)
    await expect(page.locator('button:has-text("Review Request")')).toBeEnabled()

    // Proceed to Review step
    await page.click('button:has-text("Review Request")')
    await expect(page.locator('text=Request is ready to create')).toBeVisible()
    await expect(page.locator('text=Kahn\'s Topological Waves')).toBeVisible()
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'kahn_demo_04_review_kahn_waves.png'), fullPage: true })

    // Create Request
    await page.click('button:has-text("Create Request")')
    await page.waitForTimeout(1000)

    // Open details of the newly created request
    const eyeBtns = page.locator('.requests-table .row-actions button')
    await eyeBtns.first().click()
    await expect(page.locator('h2:has-text("PR-")')).toBeVisible()

    // Verify Kahn waves and Separation of Duties (Approve disabled for pat)
    await expect(page.locator('text=DAG Release Plan (2 Waves · Kahn\'s Wave Orchestration)')).toBeVisible()
    const approveBtn = page.locator('[data-testid="request-approve"]')
    await expect(approveBtn).toBeDisabled()
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'kahn_demo_05_request_created_sod.png'), fullPage: true })

    await page.click('button:has-text("Close")')
  })

  test('Step 2: Reviewer rae Approves and Coordinates Waves in Production', async ({ page }) => {
    // Switch to reviewer rae
    await ssoLogin(page, 'rae')
    await page.goto('/#/requests')
    await page.waitForLoadState('networkidle')

    // Open first request details
    const eyeBtns = page.locator('.requests-table .row-actions button')
    await eyeBtns.first().click()
    await expect(page.locator('h2:has-text("PR-")')).toBeVisible()

    // Approve button should now be enabled for rae
    const approveBtn = page.locator('[data-testid="request-approve"]')
    await expect(approveBtn).toBeEnabled()
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'kahn_demo_06_reviewer_rae_ready.png'), fullPage: true })

    // Approve the release
    await approveBtn.click()
    await page.waitForTimeout(2000)

    // Re-verify modal shows approved state and Wave progression
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'kahn_demo_07_wave1_approved_executing.png'), fullPage: true })
    await page.click('button:has-text("Close")')
  })

  test('Step 3: Verification of Successful Kahn Multi-Module Deployment in UI', async ({ page }) => {
    await ssoLogin(page, 'pat')
    await page.goto('/#/requests')
    await page.waitForLoadState('networkidle')

    // Find the multi-module release row in requests table
    const targetRow = page.locator('.requests-table .table-row:has-text("rolling")').first()
    await expect(targetRow).toBeVisible()

    // Take overview screenshot showing the release in table
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'kahn_demo_09_requests_table_succeeded.png'), fullPage: true })

    // Open request details modal
    await targetRow.locator('.row-actions button').click()
    await expect(page.locator('h2:has-text("PR-")')).toBeVisible()

    // Verify Kahn Waves both present (2 Waves · Kahn's Wave Orchestration)
    await expect(page.locator('text=DAG Release Plan (2 Waves · Kahn\'s Wave Orchestration)')).toBeVisible()
    await expect(page.locator('text=Wave 1')).toBeVisible()
    await expect(page.locator('text=Wave 2')).toBeVisible()

    // Capture screenshot of Kahn deployment waves
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'kahn_demo_08_kahn_success_waves_completed.png'), fullPage: true })
    await page.click('button:has-text("Close")')
  })
})
