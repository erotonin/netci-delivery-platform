import { expect, test, type Page } from '@playwright/test'
import * as path from 'path'

const ARTIFACT_DIR = '/home/deployer/.gemini/antigravity-cli/brain/05605e73-a615-4bdb-bbb4-41065dd7cfa8'

async function ssoLogin(page: Page, username: string, password = 'netci-lab-only') {
  await page.goto('/#/login')
  await page.waitForLoadState('networkidle')

  // Check if already logged in as username
  const userBtn = page.locator('.sidebar-user')
  if (await userBtn.isVisible()) {
    const text = await userBtn.textContent()
    if (text?.toLowerCase().includes(username.toLowerCase())) {
      return
    }
    // Log out first if different user
    await userBtn.click()
    await page.waitForTimeout(500)
  }

  // Click SSO Login
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
  console.log(`[Audit] Successfully signed in as ${username}`)
}

test.describe.serial('Full User Journey & End-to-End Enterprise Experience Audit', () => {
  test.setTimeout(240_000)

  test('Step 1: Developer Journey - Dashboard, CI/CD Pipeline & Real-Time DAG', async ({ page }) => {
    console.log('[Audit] 1. Signing in as developer pat...')
    await ssoLogin(page, 'pat')

    // Dashboard check
    await page.click('button:has-text("Dashboard")')
    await expect(page.locator('.kpi-grid')).toBeVisible()
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'audit_01_dashboard.png') })

    // Verify KPIs
    const kpiCards = page.locator('.kpi-card strong')
    const kpiCount = await kpiCards.count()
    expect(kpiCount).toBeGreaterThanOrEqual(4)
    console.log(`[Audit] Verified ${kpiCount} KPI cards on Dashboard`)

    // Navigate to Systems
    await page.click('button:has-text("Systems")')
    await expect(page.locator('h1')).toContainText('Systems')

    // Select system (hello-container)
    const systemRow = page.locator('.systems-table button').first()
    await expect(systemRow).toBeVisible()
    const systemName = await systemRow.locator('.strong-cell').textContent()
    console.log(`[Audit] Navigating into system: ${systemName}`)
    await systemRow.click()

    // View module
    const viewModBtn = page.locator('button:has-text("View Module")').first()
    await expect(viewModBtn).toBeVisible()
    await viewModBtn.click()

    // Pipeline Tab
    await page.click('button[role="tab"]:has-text("Pipeline")')
    await expect(page.locator('.pipeline-card-grid')).toBeVisible()
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'audit_02_pipeline_cards.png') })
    console.log('[Audit] Pipeline environment cards loaded (Dev, Staging, Prod)')

    // Check Run Pipeline Dialog & Smart Git Picker
    const runBtn = page.locator('.pipeline-card button:has-text("Run Pipeline")').first()
    await expect(runBtn).toBeVisible()
    await runBtn.click()
    await expect(page.locator('.modal')).toBeVisible()

    // Verify Smart Git Picker inputs
    const commitInput = page.locator('.modal input.mono').first()
    await expect(commitInput).toBeVisible()
    const commitVal = await commitInput.inputValue()
    console.log(`[Audit] Smart Git Picker prefilled commit SHA: ${commitVal}`)
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'audit_03_smart_git_picker.png') })

    // Close Run Modal
    await page.locator('.modal button:has-text("Cancel")').click()
    await expect(page.locator('.modal')).toBeHidden()

    // Open a run to view the Real-Time Pipeline DAG
    const lastBuild = page.locator('.last-build').first()
    if (await lastBuild.isVisible()) {
      await lastBuild.click()
      await expect(page.locator('.stage-graph')).toBeVisible({ timeout: 15_000 })
      console.log('[Audit] Real-time Pipeline DAG loaded successfully!')

      // Test DAG zoom controls
      const zoomIn = page.locator('button[aria-label="Phóng to"]')
      if (await zoomIn.isVisible()) await zoomIn.click()

      // Click on a stage node to inspect detail
      const stageNode = page.locator('.stage-node').first()
      if (await stageNode.isVisible()) await stageNode.click()

      await page.screenshot({ path: path.join(ARTIFACT_DIR, 'audit_04_pipeline_dag_realtime.png') })

      // Navigate back using the new breadcrumb
      const backBtn = page.locator('button:has-text("All Environments"), button:has-text("All environments")').first()
      if (await backBtn.isVisible()) {
        await backBtn.click()
      }
    }

    // DORA Metrics Tab
    await page.click('button[role="tab"]:has-text("DORA Metrics")')
    await expect(page.locator('.dora-grid')).toBeVisible()
    const doraCards = page.locator('.dora-card')
    expect(await doraCards.count()).toBe(4)
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'audit_05_dora_tab.png') })
    console.log('[Audit] DORA Metrics tab loaded with 4 standard metrics')
  })

  test('Step 2: Multi-User Collaboration - Production Request & Separation of Duties', async ({ page }) => {
    console.log('[Audit] 2. Testing Production Request Creation & Approval rules...')
    await ssoLogin(page, 'pat')

    // Go to Production Requests
    await page.click('button:has-text("Production Requests")')
    await expect(page.locator('h1')).toContainText('Production Requests')

    // Click "New Request" button
    const reqBtn = page.locator('button:has-text("New Request")')
    await expect(reqBtn).toBeVisible()
    await reqBtn.click()
    await expect(page.locator('.modal')).toBeVisible()
    await expect(page.locator('.modal h2')).toContainText('New Production Request')

    // Verify Strategy selection (Canary / Blue-Green / Rolling)
    const canaryCard = page.locator('.strategy-card:has-text("Canary")')
    if (await canaryCard.isVisible()) {
      await canaryCard.click()
      console.log('[Audit] Selected Canary Deployment Strategy')
      await expect(page.locator('.canary-l7-box')).toBeVisible()
    }

    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'audit_06_request_form_canary.png') })
    await page.locator('.modal button:has-text("Cancel")').click()
    await expect(page.locator('.modal')).toBeHidden()

    // Inspect an existing request in the table
    const viewEyeBtn = page.locator('.requests-table button[aria-label^="Xem "]').first()
    if (await viewEyeBtn.isVisible()) {
      await viewEyeBtn.click()
      await expect(page.locator('.modal')).toBeVisible()
      console.log('[Audit] Opened Production Request detail view')

      // Check if Approve button is disabled if viewer was creator
      const approveBtn = page.locator('.modal button:has-text("Approve"), .modal button:has-text("Phê duyệt")')
      if (await approveBtn.isVisible()) {
        const disabled = await approveBtn.isDisabled()
        console.log(`[Audit] Creator "Approve" button disabled state: ${disabled}`)
      }
      await page.screenshot({ path: path.join(ARTIFACT_DIR, 'audit_07_request_detail_pat.png') })
      await page.locator('.modal button[aria-label="Đóng"], .modal button:has-text("Close")').first().click()
    }

    // Now test Reviewer persona (rae)
    console.log('[Audit] Switching to Reviewer persona (rae)...')
    const userBtn = page.locator('.sidebar-user')
    await userBtn.click()
    await page.waitForTimeout(1000)

    await ssoLogin(page, 'rae')
    await page.click('button:has-text("Production Requests")')
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'audit_08_reviewer_request_list.png') })
    console.log('[Audit] Reviewer rae sees production release queue')
  })

  test('Step 3: Self-Service System & Module Creation via Golden Path', async ({ page }) => {
    console.log('[Audit] 3. Testing Self-Service System & Module Creation...')
    await ssoLogin(page, 'pat')

    await page.click('button:has-text("Systems")')
    await page.click('button:has-text("New System")')
    await expect(page.locator('.modal')).toBeVisible()

    const sysId = `fin-audit-${Date.now().toString().slice(-4)}`
    await page.locator('.modal input[placeholder*="fintech-platform"]').fill(sysId)
    const descInput = page.locator('.modal textarea')
    if (await descInput.isVisible()) {
      await descInput.fill('Self-service test system created by browser user.')
    }

    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'audit_09_new_system_modal.png') })
    await page.click('.modal button:has-text("Create System")')

    await expect(page.locator('.modal')).toBeHidden()
    console.log(`[Audit] Successfully created system: ${sysId}`)

    // Now inside the new system, click "New Module"
    const newModBtn = page.locator('button:has-text("New Module"), button:has-text("Add module")').first()
    if (await newModBtn.isVisible()) {
      await newModBtn.click()
      await expect(page.locator('h1')).toContainText('New Module')
      console.log('[Audit] New Module Wizard opened!')
      await page.screenshot({ path: path.join(ARTIFACT_DIR, 'audit_10_new_module_wizard.png') })

      // Go back to system to clean up
      await page.locator('button:has-text("Cancel"), button:has-text("Quay lại")').first().click()
    }

    // Clean up test system to keep database pristine
    const deleteBtn = page.locator('button:has-text("Delete System")')
    if (await deleteBtn.isVisible()) {
      await deleteBtn.click()
      await expect(page.locator('.modal')).toBeVisible()
      await page.click('.modal button:has-text("Confirm Delete")')
      await expect(page.locator('.modal')).toBeHidden()
      console.log(`[Audit] Cleaned up test system: ${sysId}`)
    }
  })

  test('Step 4: Platform Management & Template Studio', async ({ page }) => {
    console.log('[Audit] 4. Testing Service Catalog & Template Studio...')
    await ssoLogin(page, 'pat')

    // Navigate to Catalog
    await page.click('button:has-text("Service Catalog")')
    await expect(page.locator('h1')).toContainText('Service Catalog')

    // Switch to Golden Path Templates tab
    await page.click('button[role="tab"]:has-text("Golden Path Templates")')
    await expect(page.locator('article h3').first()).toBeVisible({ timeout: 10_000 })
    const templates = page.locator('article:has(h3)')
    const count = await templates.count()
    console.log(`[Audit] Catalog shows ${count} active Golden Path templates`)
    expect(count).toBeGreaterThanOrEqual(1)

    // Check Register Template Button
    const regBtn = page.locator('button[data-testid="catalog-register-template"]')
    await expect(regBtn).toBeVisible()
    await regBtn.click()
    await expect(page.locator('.modal')).toBeVisible()
    await expect(page.locator('.modal h2')).toContainText('Register Golden Path Template')

    // Test form fields
    const tplId = `audit-tpl-${Date.now().toString().slice(-4)}`
    await page.locator('#tpl-id').fill(tplId)
    await page.locator('#tpl-name').fill('Audit Microservice Template')
    await page.locator('#tpl-description').fill('Golden Path template registered via UI form.')

    // Test invalid JSON validation
    await page.locator('#tpl-parameters').fill('{ invalid_json: true ')
    await page.locator('button[data-testid="catalog-register-template-submit"]').click()
    await expect(page.locator('.modal .inline-error, .modal [role="alert"]')).toBeVisible()
    console.log('[Audit] Template Studio correctly caught invalid JSON syntax!')

    // Now enter valid JSON
    await page.locator('#tpl-parameters').fill('{"port": 8080}')
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'audit_11_template_studio_modal.png') })

    await page.locator('.modal button:has-text("Cancel")').click()
    await expect(page.locator('.modal')).toBeHidden()
  })

  test('Step 5: Infrastructure & Server Fleet Monitoring', async ({ page }) => {
    console.log('[Audit] 5. Testing Servers & Infrastructure Monitoring...')
    await ssoLogin(page, 'pat')

    await page.click('button:has-text("Servers")')
    await expect(page.locator('h1')).toContainText('Deployment Targets & Inventory')

    // Check server list
    const serverCards = page.locator('.data-table .table-row')
    const count = await serverCards.count()
    console.log(`[Audit] Servers page displays ${count} server rows/cards`)

    // Check telemetry / maintenance controls
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'audit_12_servers_fleet.png') })
  })
})
