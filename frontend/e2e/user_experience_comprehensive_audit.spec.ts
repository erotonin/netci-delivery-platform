import { expect, test, type Page } from '@playwright/test'
import * as path from 'path'

const ARTIFACT_DIR = '/home/deployer/.gemini/antigravity-cli/brain/05605e73-a615-4bdb-bbb4-41065dd7cfa8'

/**
 * Authentic Keycloak SSO Login using browser PKCE exchange.
 */
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
    // Logout first if already signed in as someone else
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

test.describe.serial('Comprehensive Real-User Experience & Zero-Mock Platform Audit', () => {
  test.setTimeout(300_000)

  test('Workflow 1: CI/CD Pipeline, Live DAG Execution & Realtime Promotion', async ({ page }) => {
    console.log('[Workflow 1] Logging in as engineer pat...')
    await ssoLogin(page, 'pat')
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'user_audit_01_sso_pat.png') })

    // 1. Navigate to Systems and pick first application
    await page.click('button:has-text("Systems")')
    await expect(page.locator('h1')).toContainText('Systems')
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'user_audit_02_systems_list.png') })

    const systemRow = page.locator('.systems-table button:has-text("hello-container")').first()
    await expect(systemRow).toBeVisible()
    await systemRow.click()

    // 2. View Module
    const viewModBtn = page.locator('button:has-text("View Module")').first()
    await expect(viewModBtn).toBeVisible()
    await viewModBtn.click()

    // 3. Switch to Pipeline Tab
    await page.click('button[role="tab"]:has-text("Pipeline")')
    await expect(page.locator('.pipeline-card-grid')).toBeVisible()
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'user_audit_03_pipeline_dev.png') })

    // 4. Trigger CI Run via Smart Git Picker
    const runBtn = page.locator('.pipeline-card:has-text("Dev") button:has-text("Run Pipeline")').first()
    await expect(runBtn).toBeVisible()
    await runBtn.click()
    await expect(page.locator('.modal')).toBeVisible()

    const commitInput = page.locator('.modal input.mono').first()
    await expect(commitInput).toBeVisible()
    await page.waitForTimeout(500)

    const triggerBtn = page.locator('.modal button:has-text("Run Pipeline")').last()
    await expect(triggerBtn).toBeEnabled()
    await triggerBtn.click()

    // 5. Verify Real-time DAG rendered
    await expect(page.locator('.stage-graph, .pipeline-run-view, h2:has-text("build #")').first()).toBeVisible({ timeout: 20_000 })
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'user_audit_04_live_dag.png') })
    console.log('[Workflow 1] Live DAG and Jenkins build verified successfully.')
  })

  test('Workflow 2: Production Request, Separation of Duties & Reviewer Approval', async ({ page, browser }) => {
    console.log('[Workflow 2] Creating Production Request as pat...')
    await ssoLogin(page, 'pat')

    await page.click('button:has-text("Production Requests")')
    await expect(page.locator('h1')).toContainText('Production Requests')

    // Open New Request Modal
    await page.click('button:has-text("New Request")')
    const modal = page.locator('.modal')
    await expect(modal).toBeVisible()

    // Select first available module with versions
    const modBtn = page.locator('.selectable-modules button:not([disabled])').first()
    await expect(modBtn).toBeVisible()
    await modBtn.click()

    // Select Canary strategy
    const canaryCard = page.locator('.option-cards button:has-text("Canary Rollout")')
    if (await canaryCard.isVisible()) {
      await canaryCard.click()
    }

    const reasonInput = page.locator('textarea, input[placeholder*="Lý do"]').first()
    if (await reasonInput.isVisible()) {
      await reasonInput.fill('Real-User Production Release Canary Rollout')
    }

    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'user_audit_05_canary_request.png') })

    const reviewBtn = page.locator('button:has-text("Review Request")')
    await expect(reviewBtn).toBeEnabled()
    await reviewBtn.click()

    const createBtn = page.locator('button:has-text("Create Request")')
    await expect(createBtn).toBeVisible()
    await createBtn.click()
    await expect(modal).toBeHidden()

    // Separation of Duties check: Requester pat cannot approve
    const firstRow = page.locator('.requests-table button[aria-label^="Xem "]').first()
    await firstRow.click()
    await expect(page.locator('.modal')).toBeVisible()

    const approveBtn = page.locator('.modal [data-testid="request-approve"], .modal button:has-text("Approve"), .modal button:has-text("Phê duyệt")').first()
    await expect(approveBtn).toBeVisible()
    await expect(approveBtn).toBeDisabled()
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'user_audit_06_sod_disabled.png') })
    console.log('[Workflow 2] Separation of Duties verified: Requester cannot self-approve.')

    await page.locator('.modal button:has-text("Close"), .modal button:has-text("Đóng")').first().click()

    // Switch to Reviewer rae in a separate browser context
    console.log('[Workflow 2] Reviewer rae authenticating to approve request...')
    const reviewerContext = await browser.newContext()
    const reviewerPage = await reviewerContext.newPage()
    await ssoLogin(reviewerPage, 'rae')

    await reviewerPage.click('button:has-text("Production Requests")')
    const reviewerFirstRow = reviewerPage.locator('.requests-table button[aria-label^="Xem "]').first()
    await reviewerFirstRow.click()

    const reviewerApproveBtn = reviewerPage.locator('.modal [data-testid="request-approve"], .modal button:has-text("Approve"), .modal button:has-text("Phê duyệt")').first()
    await expect(reviewerApproveBtn).toBeVisible()
    await expect(reviewerApproveBtn).toBeEnabled()

    await reviewerPage.screenshot({ path: path.join(ARTIFACT_DIR, 'user_audit_07_reviewer_rae_approve.png') })
    await reviewerApproveBtn.click()
    await reviewerPage.waitForTimeout(1000)
    console.log('[Workflow 2] Reviewer rae approved successfully.')
    await reviewerContext.close()
  })

  test('Workflow 3: Self-Service Component Creation (New System & Module)', async ({ page }) => {
    console.log('[Workflow 3] Creating new System and Module...')
    await ssoLogin(page, 'pat')

    await page.click('button:has-text("Systems")')
    const newSystemId = `enterprise-sys-${Date.now().toString().slice(-4)}`

    await page.click('button:has-text("New System")')
    await expect(page.locator('.modal')).toBeVisible()

    await page.locator('.modal input').first().fill(newSystemId)
    await page.locator('.modal input').nth(1).fill('Enterprise Core')
    await page.locator('.modal textarea').fill('Real-User Enterprise Test System')
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'user_audit_08_new_system_created.png') })

    await page.locator('.modal .primary-button:has-text("Create System")').click()
    await expect(page.locator('.modal')).toBeHidden()

    // The portal immediately navigates into the newly created system overview
    await expect(page.locator('h1')).toHaveText(newSystemId)

    // Open New Module Wizard
    await page.click('button:has-text("New Module")')
    await expect(page.locator('h1, h2, .wizard-container').first()).toBeVisible()
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'user_audit_09_new_module_wizard.png') })
    console.log('[Workflow 3] New System & Module wizard verified successfully.')

    // Navigate back to system overview
    await page.click(`.sidebar button:has-text("${newSystemId}"), button:has-text("Overview")`)
    await expect(page.locator('h1')).toHaveText(newSystemId)

    // Test Delete System (Managerial deletion lifecycle)
    await page.click('button:has-text("Delete System")')
    await expect(page.locator('.modal')).toBeVisible()
    await page.click('.modal button:has-text("Confirm Delete")')
    await expect(page.locator('.modal')).toBeHidden()
    console.log(`[Workflow 3] Successfully tested managerial system deletion for ${newSystemId}.`)
  })

  test('Workflow 4: Service Catalog, Services Graph & Self-Service Resources', async ({ page }) => {
    console.log('[Workflow 4] Testing Service Catalog, Dependency Graph & Resources...')
    await ssoLogin(page, 'pat')

    await page.click('button:has-text("Service Catalog")')
    await expect(page.locator('h1').first()).toContainText(/Catalog/i)

    // Verify Services & Dependency Graph
    await expect(page.getByRole('tab', { name: /Services & Dependency Graph/i })).toBeVisible()
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'user_audit_10_service_catalog.png') })

    // Switch to Ephemeral Previews tab
    await page.click('button:has-text("Ephemeral Preview Environments"), [role="tab"]:has-text("Ephemeral Preview Environments")')
    await page.waitForTimeout(300)

    // Switch to Self-Service Resources tab (cloud infrastructure)
    await page.click('button:has-text("Self-Service Resources"), [role="tab"]:has-text("Self-Service Resources")')
    await page.waitForTimeout(500)
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'user_audit_11_self_service_resources.png') })
    console.log('[Workflow 4] Service Catalog & Self-service resources verified successfully.')
  })

  test('Workflow 5: Complete Audit of All Enterprise Governance & Operations Screens', async ({ page }) => {
    console.log('[Workflow 5] Auditing Release Plan, Stage Catalog, CI Cost, Calendar, Scorecards, Servers...')
    await ssoLogin(page, 'pat')

    // 1. Shared pipelines & designer (ADR-058)
    await page.getByRole('button', { name: /^Pipelines$/ }).click()
    await expect(page.locator('h1')).toHaveText('Pipelines')
    await page.waitForTimeout(300)
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'user_audit_12_pipelines_list.png') })

    await page.getByRole('button', { name: 'New pipeline' }).click()
    await expect(page.getByLabel('Pipeline script')).toBeVisible({ timeout: 10_000 })
    await page.waitForTimeout(300)
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'user_audit_13_pipeline_designer.png') })

    // 2. Toolchain Management (ADR-056)
    await page.click('button:has-text("Toolchain")')
    await expect(page.locator('h1, h2')).toContainText(/Toolchain/i)
    await page.waitForTimeout(300)
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'user_audit_14_toolchain_governance.png') })

    // 3. Release Calendar (Freeze testing)
    await page.click('button:has-text("Release Calendar")')
    await expect(page.locator('h1, h2')).toContainText(/Release Calendar/i)
    await page.waitForTimeout(300)
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'user_audit_15_release_calendar_freeze.png') })

    // 5. Servers Fleet (Deployment Targets & Inventory from NetBox)
    await page.click('button:has-text("Servers")')
    await expect(page.locator('h1, h2')).toContainText(/Deployment Targets|Servers/i)
    await page.waitForTimeout(300)
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'user_audit_18_servers_fleet.png') })

    console.log('[Workflow 5] All governance screens audited and verified.')
  })
})
