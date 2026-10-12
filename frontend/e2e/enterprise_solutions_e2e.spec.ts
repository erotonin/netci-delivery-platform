import { test, expect } from '@playwright/test'

test.describe('Enterprise IDP 3 Core Solutions E2E Suite', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate to root to ensure authenticated session
    await page.goto('/')
    await page.waitForTimeout(500)
    // If on persona selector, login as Admin / Developer
    const adminPersona = page.locator('button:has-text("Admin"), button:has-text("Developer")').first()
    if (await adminPersona.isVisible()) {
      await adminPersona.click()
      await page.waitForTimeout(500)
    }
  })

  test('Solution 1: Multi-Module Orchestration DAG & Wave Calculation', async ({ page }) => {
    await page.goto('/#/production-requests')
    await expect(page.locator('h1')).toContainText(/Production Requests/i)

    // Open New Production Request Wizard
    const newReqBtn = page.locator('button:has-text("New Request")')
    await expect(newReqBtn).toBeVisible({ timeout: 5000 })
    await newReqBtn.click()

    // Verify DAG wave orchestration instructions and multi-module capabilities
    const modal = page.locator('.modal-card, [role="dialog"]')
    await expect(modal).toBeVisible({ timeout: 5000 })
    await expect(modal).toContainText(/DAG wave coordination schedules safe sequential rollouts/i)

    // Verify Rolling DAG Strategy option
    await expect(modal).toContainText(/Rolling DAG/i)
    await expect(modal).toContainText(/Deploy in topological waves/i)

    // Verify Strategy options (Canary, Blue/Green)
    await expect(modal).toContainText(/Canary Rollout/i)
    await expect(modal).toContainText(/Blue \/ Green/i)

    // Close modal
    const closeBtn = modal.locator('button:has-text("Cancel")')
    await closeBtn.click()
  })

  test('Solution 2: Decoupled Lifecycle - Fast-Track Config Deploy (Skip CI Rebuild)', async ({ page }) => {
    // Navigate directly to hello-container system
    await page.goto('/#/systems')
    await page.waitForTimeout(600)

    // Click on hello-container system
    const systemBtn = page.locator('button:has-text("hello-container"), .system-card:has-text("hello-container")').first()
    await expect(systemBtn).toBeVisible({ timeout: 5000 })
    await systemBtn.click()
    await page.waitForTimeout(600)

    // Click on the module
    const moduleBtn = page.locator('button:has-text("Hello Container"), a:has-text("Hello Container"), .module-card').first()
    if (await moduleBtn.isVisible()) {
      await moduleBtn.click()
      await page.waitForTimeout(600)
    }

    // Switch to Configuration tab
    const configTab = page.locator('button[role="tab"]:has-text("Configuration"), button:has-text("Configuration")').first()
    await expect(configTab).toBeVisible({ timeout: 5000 })
    await configTab.click()
    await page.waitForTimeout(600)

    // Verify Fast Apply button is visible
    const fastApplyBtn = page.locator('#btn-fast-apply-config')
    await expect(fastApplyBtn).toBeVisible({ timeout: 5000 })
    await fastApplyBtn.click()

    // Verify Modal appears
    const modalTitle = page.locator('h2:has-text("Redeploy with the active configuration")')
    await expect(modalTitle).toBeVisible({ timeout: 5000 })

    // Select staging environment
    const envSelect = page.locator('#fast-apply-env-select')
    await envSelect.selectOption('staging')

    // Confirm instant apply
    const confirmBtn = page.locator('#btn-confirm-fast-apply')
    await expect(confirmBtn).toBeVisible()
    await confirmBtn.click()

    // The result box says what was established: a deployment was *started* (or is
    // waiting for a reviewer). Nothing is "applied" until the worker reports.
    const resultBox = page.locator('#fast-apply-result-box')
    await expect(resultBox).toBeVisible({ timeout: 10000 })
    await expect(resultBox).toContainText(/waiting for (the worker|a reviewer)/i)
    await expect(resultBox).toContainText(/digest-pinned/i)
    await expect(resultBox).not.toContainText(/Applied Successfully/i)
  })

  test('Solution 3: Outbound Edge Runner Agent (Zero Inbound Port) - Realtime Telemetry & Remote Execution', async ({ page }) => {
    await page.goto('/#/servers')
    await expect(page.locator('h1')).toContainText(/Deployment targets|Inventory|Servers/i)

    // Verify servers table renders
    const rows = page.locator('.servers-table .table-row:not(.table-head)')
    await expect(rows.first()).toBeVisible({ timeout: 5000 })

    // Verify Edge Agent badge on hello-container:dev:localhost
    const srvRow = page.locator('.servers-table .table-row:has-text("hello-container:dev:localhost")').first()
    await expect(srvRow).toBeVisible({ timeout: 5000 })
    await expect(srvRow).toContainText('Edge Agent')

    // Open details modal
    const moreBtn = srvRow.locator('.row-actions button, button[aria-label*="hello-container:dev:localhost"]').first()
    await moreBtn.click()
    await page.waitForTimeout(600)

    // Verify Outbound Edge Runner Agent section
    const agentSection = page.locator('h4:has-text("Outbound Edge Runner Agent")')
    await expect(agentSection).toBeVisible({ timeout: 5000 })
    await expect(page.locator('text=AGENT CONNECTED (WS)')).toBeVisible()

    // Dispatch a diagnostic command through the outbound WebSocket agent
    const cmdInput = page.locator('#agent-command-input')
    await expect(cmdInput).toBeVisible()
    await cmdInput.fill('echo "Enterprise Edge Agent Verified" && uname -s')

    const dispatchBtn = page.locator('#btn-run-agent-cmd')
    await dispatchBtn.click()

    // Verify command output
    const outputBox = page.locator('#agent-command-output')
    await expect(outputBox).toBeVisible({ timeout: 10000 })
    await expect(outputBox).toContainText('Enterprise Edge Agent Verified')
    await expect(outputBox).toContainText('Linux')
    await expect(outputBox).toContainText('Exit Code: 0')
  })
})
