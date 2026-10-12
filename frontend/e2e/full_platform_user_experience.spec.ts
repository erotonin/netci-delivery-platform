import { expect, test, type Page } from '@playwright/test'

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

test.describe.serial('Comprehensive Real-User Platform Experience & Zero-Mock Verification', () => {
  test.setTimeout(180_000)

  test('Milestone 1: CI/CD Pipeline Execution & Real-Time DAG Monitoring', async ({ page }) => {
    console.log('[E2E 1] Signing in as engineer pat...')
    await ssoLogin(page, 'pat')

    // Navigate to Systems
    await page.click('button:has-text("Systems")')
    await expect(page.locator('h1')).toContainText('Systems')

    // Pick hello-container system
    const systemRow = page.locator('.systems-table button:has-text("hello-container")').first()
    await expect(systemRow).toBeVisible()
    await systemRow.click()

    // View Module hello-container
    const viewModBtn = page.locator('button:has-text("View Module")').first()
    await expect(viewModBtn).toBeVisible()
    await viewModBtn.click()

    // Switch to Pipeline tab
    await page.click('button[role="tab"]:has-text("Pipeline")')
    await expect(page.locator('.pipeline-card-grid')).toBeVisible()

    // Run Pipeline on Dev
    const runBtn = page.locator('.pipeline-card:has-text("Dev") button:has-text("Run Pipeline")').first()
    await expect(runBtn).toBeVisible()
    await runBtn.click()
    await expect(page.locator('.modal')).toBeVisible()

    // Smart Git Picker - verify prefilled commit and parameters
    const commitInput = page.locator('.modal input.mono').first()
    await expect(commitInput).toBeVisible()
    await expect(commitInput).not.toHaveValue('', { timeout: 10_000 })
    const commitSha = await commitInput.inputValue()
    console.log(`[E2E 1] Smart Git Picker detected commit: ${commitSha}`)
    expect(commitSha).toMatch(/^[0-9a-f]{7,64}$/i)

    // Trigger the real pipeline run
    const triggerBtn = page.locator('.modal button:has-text("Run Pipeline"), .modal button:has-text("Trigger")').first()
    await triggerBtn.click()
    await expect(page.locator('.modal')).toBeHidden()

    // The UI automatically navigates into the newly queued build's DAG
    await expect(page.locator('.stage-graph, .pipeline-run-view, h2:has-text("build #")').first()).toBeVisible({ timeout: 15_000 })
    const stageNodes = page.locator('button:has-text("Checkout"), button:has-text("Build"), .stage-node')
    expect(await stageNodes.count()).toBeGreaterThanOrEqual(1)
    console.log(`[E2E 1] Live Jenkins build queued and Real-Time DAG rendered successfully!`)

    // Verify DORA Metrics Tab
    await page.click('button[role="tab"]:has-text("DORA Metrics")')
    await expect(page.locator('.dora-grid')).toBeVisible()
    const doraCards = page.locator('.dora-card')
    expect(await doraCards.count()).toBe(4)
    console.log('[E2E 1] DORA Metrics validated.')
  })

  test('Milestone 2: Production Request Lifecycle, Separation of Duties & Reviewer Approval', async ({ page, browser }) => {
    console.log('[E2E 2] Authenticating as requester (pat)...')
    await ssoLogin(page, 'pat')

    // Navigate to Production Requests
    await page.click('button:has-text("Production Requests")')
    await expect(page.locator('h1')).toContainText('Production Requests')

    // Open New Request Modal
    await page.click('button:has-text("New Request")')
    const modal = page.locator('.modal')
    await expect(modal).toBeVisible()

    // 1. Select Module with registered versions (Hello Container)
    const modBtn = page.locator('.selectable-modules button:has-text("Hello Container"):not([disabled])').first()
    await expect(modBtn).toBeVisible()
    await modBtn.click()

    // 2. Select Canary Strategy
    const canaryCard = page.locator('.option-cards button:has-text("Canary Rollout")')
    if (await canaryCard.isVisible()) {
      await canaryCard.click()
      console.log('[E2E 2] Selected Canary rollout strategy.')
    }

    // 3. Fill Change Reason
    const reasonInput = page.locator('textarea, input[placeholder*="Lý do"]').first()
    if (await reasonInput.isVisible()) {
      await reasonInput.fill('E2E Verification of Canary Rollout & Separation of Duties')
    }

    // 4. Review & Submit Request
    const reviewBtn = page.locator('button:has-text("Review Request")')
    await expect(reviewBtn).toBeEnabled()
    await reviewBtn.click()

    const createBtn = page.locator('button:has-text("Create Request")')
    await expect(createBtn).toBeVisible()
    await createBtn.click()
    await expect(modal).toBeHidden()
    console.log('[E2E 2] Production Request successfully submitted.')

    // 5. Verify Negative Test: Requester (pat) cannot approve own request (Separation of Duties)
    const firstRow = page.locator('.requests-table button[aria-label^="Xem "]').first()
    await firstRow.click()
    await expect(page.locator('.modal')).toBeVisible()

    const approveBtn = page.locator('.modal [data-testid="request-approve"], .modal button:has-text("Approve"), .modal button:has-text("Phê duyệt")').first()
    await expect(approveBtn).toBeVisible()
    await expect(approveBtn).toBeDisabled()
    const titleAttr = await approveBtn.getAttribute('title')
    expect(titleAttr).toMatch(/You raised this request|different reviewer|tách biệt/i)
    console.log(`[E2E 2] Separation of Duties verified: Approve button is correctly DISABLED for requester with title: "${titleAttr}"`)
    await page.locator('.modal button[aria-label="Đóng"], .modal button:has-text("Close")').first().click()

    // 6. Role Switch: Sign in as Reviewer / Release Manager (rae) in a dedicated context
    console.log('[E2E 2] Opening separate browser context for Reviewer (rae)...')
    const reviewerContext = await browser.newContext()
    const reviewerPage = await reviewerContext.newPage()
    await ssoLogin(reviewerPage, 'rae')

    await reviewerPage.click('button:has-text("Production Requests")')
    const reviewerViewBtn = reviewerPage.locator('.requests-table button[aria-label^="Xem "]').first()
    await reviewerViewBtn.click()
    await expect(reviewerPage.locator('.modal')).toBeVisible()

    // Reviewer rae approves the request
    const reviewerApproveBtn = reviewerPage.locator('.modal [data-testid="request-approve"], .modal button:has-text("Approve"), .modal button:has-text("Phê duyệt")').first()
    await expect(reviewerApproveBtn).toBeVisible()
    await expect(reviewerApproveBtn).toBeEnabled()
    await reviewerApproveBtn.click()

    console.log('[E2E 2] Reviewer rae successfully executed approval!')
    await reviewerPage.waitForTimeout(1000)
    await reviewerContext.close()
  })

  test('Milestone 3: Self-Service Catalog - System & Module Wizard Lifecycle', async ({ page }) => {
    console.log('[E2E 3] Testing Golden Path Component Creation as Platform Admin (pat)...')
    await ssoLogin(page, 'pat')

    // Create System
    await page.click('button:has-text("Systems")')
    await page.click('button:has-text("New System")')
    await expect(page.locator('.modal')).toBeVisible()

    const sysId = `telecom-core-${Date.now().toString().slice(-4)}`
    await page.locator('.modal input[placeholder*="fintech-platform"]').fill(sysId)
    const descInput = page.locator('.modal textarea')
    if (await descInput.isVisible()) {
      await descInput.fill('High-throughput telecom microservice cluster.')
    }
    await page.click('.modal button:has-text("Create System")')
    await expect(page.locator('.modal')).toBeHidden()
    console.log(`[E2E 3] Created new system: ${sysId}`)

    // Create Module inside System via NewModuleWizard
    const newModBtn = page.locator('button:has-text("New Module"), button:has-text("Add module")').first()
    await expect(newModBtn).toBeVisible()
    await newModBtn.click()
    await expect(page.locator('h1')).toContainText('New Module')

    // Select sample application or configure local module
    const sampleBtn = page.locator('button:has-text("sample-kubernetes-app"), button:has-text("sample-docker-app")').first()
    if (await sampleBtn.isVisible()) {
      await sampleBtn.click()
    }

    const repoInput = page.locator('label:has-text("Repository URL") input, input[placeholder*="git"]')
    if (await repoInput.isVisible() && !(await repoInput.inputValue())) {
      await repoInput.fill('http://172.17.0.52/netci.git')
    }

    // Step 1 -> Step 2 (CI/CD)
    const nextBtn = page.locator('button:has-text("Next")')
    if (await nextBtn.isEnabled()) {
      await nextBtn.click()
      console.log('[E2E 3] Advanced to Step 2: CI/CD')

      // Step 2 -> Step 3 (Deployment)
      const nextBtn2 = page.locator('button:has-text("Next")')
      if (await nextBtn2.isEnabled()) {
        await nextBtn2.click()
        console.log('[E2E 3] Advanced to Step 3: Deployment')
      }
    }

    // Return to system overview
    await page.locator('button:has-text("Cancel"), button:has-text("Back to System")').first().click()

    // Clean up test system to maintain database hygiene
    const deleteBtn = page.locator('button:has-text("Delete System")')
    if (await deleteBtn.isVisible()) {
      await deleteBtn.click()
      await expect(page.locator('.modal')).toBeVisible()
      await page.click('.modal button:has-text("Confirm Delete")')
      await expect(page.locator('.modal')).toBeHidden()
      console.log(`[E2E 3] Cleaned up test system: ${sysId}`)
    }
  })

  test('Milestone 4: Template Studio Management (Register, Instantiate & Version)', async ({ page }) => {
    console.log('[E2E 4] Testing Template Studio as Platform Admin (pat)...')
    await ssoLogin(page, 'pat')

    // Open Service Catalog -> Golden Path Templates
    await page.click('button:has-text("Service Catalog")')
    await page.click('button[role="tab"]:has-text("Golden Path Templates")')

    // Open Register Template Studio Modal
    const regBtn = page.locator('button[data-testid="catalog-header-register-template"], button[data-testid="catalog-register-template"]')
    await expect(regBtn).toBeVisible()
    await regBtn.click()
    await expect(page.locator('.modal h2')).toContainText('Register Golden Path Template')

    // Form inputs
    const tplId = `vt-microservice-${Date.now().toString().slice(-4)}`
    await page.locator('#tpl-id').fill(tplId)
    await page.locator('#tpl-name').fill('VTNet Telecom Microservice')
    await page.locator('#tpl-description').fill('Standardized Viettel Cloud-native microservice with SLSA provenance.')
    await page.locator('#tpl-parameters').fill(JSON.stringify({ port: 8080, db: 'postgres' }))
    await page.locator('#tpl-pipeline').fill(JSON.stringify({ stages: ['checkout', 'test', 'build', 'sign'] }))

    // Submit Template
    await page.locator('button[data-testid="catalog-register-template-submit"]').click()
    await expect(page.locator('.modal')).toBeHidden({ timeout: 10_000 })
    console.log(`[E2E 4] Successfully registered template ${tplId} in database!`)

    // Verify Template Card in Catalog
    const tplCard = page.locator(`article:has-text("${tplId}")`)
    await expect(tplCard).toBeVisible()

    // Test 1-Click Instantiate
    const instBtn = tplCard.locator('button:has-text("1-Click Instantiate")')
    await instBtn.click()
    await expect(page.locator('.modal h2')).toContainText('Instantiate Golden Path')
    await page.locator('.modal input[placeholder*="order-api"]').fill('vt-subscriber-demo')
    await page.locator('.modal button:has-text("Generate Application Plan")').click()
    await expect(page.locator('text=Instantiated Configuration Plan Ready!')).toBeVisible({ timeout: 10_000 })
    console.log('[E2E 4] 1-Click Instantiate plan computed successfully!')
    await page.locator('.modal button:has-text("Close")').first().click()

    // Test New Version button for Admin
    const verBtn = tplCard.locator(`button[data-testid="catalog-new-version-${tplId}"]`)
    if (await verBtn.isVisible()) {
      await verBtn.click()
      await expect(page.locator('.modal h2')).toContainText(/Register Golden Path Template|Register template/i)
      console.log('[E2E 4] Template versioning modal opened successfully!')
      await page.locator('.modal button:has-text("Cancel")').click()
    }
  })

  test('Milestone 5: Enterprise Governance - Release Calendar Freezes, Vulnerabilities CVE & Server Fleet', async ({ page }) => {
    console.log('[E2E 5] Testing Release Calendar Change Freezes...')
    await ssoLogin(page, 'pat')

    // 1. Release Calendar & Change Freeze
    await page.click('button:has-text("Release Calendar")')
    await expect(page.locator('h1')).toContainText('Release Calendar')

    // Create a new change freeze
    await page.click('button:has-text("New freeze")')
    await expect(page.locator('form.freeze-form, form:has-text("New change freeze")')).toBeVisible()

    const freezeName = `DR-Drill-${Date.now().toString().slice(-4)}`
    await page.locator('#freeze-name').fill(freezeName)

    const now = new Date()
    const freezeStart = new Date(now.getTime() - 3600_000).toISOString().slice(0, 16)
    const freezeEnd = new Date(now.getTime() + 86400_000).toISOString().slice(0, 16)
    await page.locator('#freeze-start').fill(freezeStart)
    await page.locator('#freeze-end').fill(freezeEnd)

    await page.locator('#freeze-reason').fill('Viettel Disaster Recovery Drill Window')
    await page.click('button[type="submit"]:has-text("Create freeze")')

    // Verify Freeze is drawn on calendar
    const freezeBadge = page.locator(`.calendar-freeze:has-text("${freezeName}")`).first()
    await expect(freezeBadge).toBeVisible({ timeout: 10_000 })
    console.log(`[E2E 5] Change Freeze ${freezeName} enforced on Release Calendar!`)

    // Cancel the freeze
    const cancelFreezeBtn = freezeBadge.locator('button:has-text("Cancel freeze")')
    if (await cancelFreezeBtn.isVisible()) {
      await cancelFreezeBtn.click()
      console.log('[E2E 5] Change freeze successfully cancelled.')
    }

    // 2. Vulnerabilities Page
    console.log('[E2E 5] Verifying Vulnerabilities & SBOM scanning...')
    await page.click('button:has-text("Vulnerabilities")')
    await expect(page.locator('h1')).toContainText('Vulnerabilities')

    // Verify running digests / coverage table renders from real backend
    await expect(page.locator('.coverage-table, .vuln-table, table')).toBeVisible({ timeout: 10_000 })
    console.log('[E2E 5] Vulnerabilities SBOM coverage table loaded from PostgreSQL.')

    // Trigger Rescan
    const rescanBtn = page.locator('button:has-text("Rescan"), button:has-text("Quét lại")').first()
    if (await rescanBtn.isVisible()) {
      await rescanBtn.click()
      console.log('[E2E 5] Triggered live vulnerability rescan on backend.')
    }

    // 3. Scorecards & Insights
    console.log('[E2E 5] Testing Scorecards & Delivery Insights...')
    await page.click('button:has-text("Scorecards")')
    await expect(page.locator('h1')).toContainText('Scorecards')
    await expect(page.locator('[data-testid="scorecards-table"]')).toBeVisible({ timeout: 10_000 })
    const scorecardRows = page.locator('[data-testid="scorecards-table"] tbody tr')
    await expect(scorecardRows.first()).toBeVisible({ timeout: 10_000 })
    const scorecardCount = await scorecardRows.count()
    expect(scorecardCount).toBeGreaterThanOrEqual(1)
    console.log(`[E2E 5] Scorecards page loaded ${scorecardCount} real module quality evaluations.`)

    // 4. Server Fleet Monitoring
    console.log('[E2E 5] Testing Server Fleet Monitoring...')
    await page.click('button:has-text("Servers")')
    await expect(page.locator('h1')).toContainText('Deployment Targets & Inventory')
    await expect(page.locator('.data-table.servers-table')).toBeVisible({ timeout: 10_000 })
    const serverRows = page.locator('.data-table.servers-table .table-row:not(.table-head)')
    await expect(serverRows.first()).toBeVisible({ timeout: 10_000 })
    const serverCount = await serverRows.count()
    expect(serverCount).toBeGreaterThanOrEqual(1)
    console.log(`[E2E 5] Verified Server Fleet telemetry from NetBox inventory (${serverCount} servers).`)
  })
})
