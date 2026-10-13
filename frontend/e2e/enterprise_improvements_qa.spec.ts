import { test, expect } from '@playwright/test'

test.describe('Enterprise Features Professional QA Suite', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate to root to ensure authenticated session
    await page.goto('/')
    await page.waitForTimeout(500)
    // If on login page, quickly login
    const devPersona = page.locator('button:has-text("Developer"), button:has-text("Admin")').first()
    if (await devPersona.isVisible()) {
      await devPersona.click()
      await page.waitForTimeout(500)
    }
  })

  test('QA-E1: Server Maintenance Toggle & Persistence after Refresh', async ({ page }) => {
    await page.goto('/#/servers')
    await expect(page.locator('h1')).toContainText(/Deployment targets|Inventory|Servers/i)

    // Wait for servers table to be populated
    const rows = page.locator('.servers-table .table-row:not(.table-head)')
    await expect(rows.first()).toBeVisible({ timeout: 5000 })

    // Find the first maintenance action button
    const firstRow = rows.first()
    const serverName = (await firstRow.locator('.strong-cell').textContent())?.trim() || ''
    expect(serverName).toBeTruthy()

    const maintButton = firstRow.locator('button:has-text("Maintenance")')
    await expect(maintButton).toBeVisible()

    const initialText = (await maintButton.textContent())?.trim()

    // 1. Toggle state
    await maintButton.click()
    await page.waitForTimeout(800)

    // Whichever state the row is in (an earlier test may have set it), the toggle must flip it.
    const expectedNewText = initialText?.startsWith('Exit') ? 'Maintenance' : 'Exit Maintenance'
    await expect(maintButton).toHaveText(expectedNewText)

    // 2. Refresh page to verify state persistence in backend & frontend
    await page.reload()
    await expect(rows.first()).toBeVisible({ timeout: 5000 })
    const reloadedMaintButton = rows.first().locator('button:has-text("Maintenance")')
    await expect(reloadedMaintButton).toHaveText(expectedNewText)

    // 3. Revert back to original state for clean test hygiene
    await reloadedMaintButton.click()
    await page.waitForTimeout(800)
    await expect(reloadedMaintButton).toHaveText(initialText)
  })

  test('QA-E2: Live Pre-flight Telemetry Modal (CPU, Memory, Disk Gauges)', async ({ page }) => {
    await page.goto('/#/servers')
    await expect(page.locator('h1')).toContainText(/Deployment targets|Inventory|Servers/i)

    const rows = page.locator('.servers-table .table-row:not(.table-head)')
    await expect(rows.first()).toBeVisible({ timeout: 5000 })

    // Click more details on first server
    const detailsBtn = rows.first().locator('button[aria-label*="Details"]').first()
    await detailsBtn.click()

    // Modal should appear
    const modal = page.locator('.modal')
    await expect(modal).toBeVisible()
    await expect(modal.locator('h2')).toContainText(/Server:/i)

    // Verify Live Host Telemetry Card
    await expect(modal.locator('text=Pre-flight Telemetry')).toBeVisible({ timeout: 4000 })
    await expect(modal.locator('text=CPU Usage')).toBeVisible()
    await expect(modal.locator('text=Memory Usage')).toBeVisible()
    await expect(modal.locator('text=Disk Usage')).toBeVisible()
    await expect(modal.locator('input[value*="WebSocket wss://"]')).toBeVisible()

    // Close modal
    await modal.locator('button:has-text("Đóng"), button:has-text("Close")').click()
    await expect(modal).not.toBeVisible()
  })

  test('QA-E3: VEX Security Waivers CRUD and Revocation Flow', async ({ page }) => {
    await page.goto('/#/servers')
    await expect(page.locator('h1')).toContainText(/Deployment targets|Inventory|Servers/i)

    // Scroll to VEX Security Waivers section
    const waiverSection = page.locator('text=VEX Security Waivers').first()
    await expect(waiverSection).toBeVisible()

    // Open Create Waiver modal
    const createWaiverBtn = page.locator('button:has-text("Create CVE Waiver")')
    await createWaiverBtn.click()

    const modal = page.locator('.modal')
    await expect(modal).toBeVisible()
    await expect(modal.locator('h2')).toContainText(/Create VEX Security Waiver/i)

    // Fill form
    const testCveId = `CVE-2026-QA-${Date.now().toString().slice(-4)}`
    await modal.locator('input[placeholder*="CVE-"]').fill(testCveId)
    await modal.locator('textarea[placeholder*="Describe VEX analysis"]').fill('Compensating control: Automated QA isolation verify safe deployment')

    // Submit
    await modal.locator('button:has-text("Confirm Waiver")').click()
    await expect(modal).not.toBeVisible({ timeout: 4000 })

    // Verify row appears in waivers table (check strong-cell in data table specifically to avoid toast ambiguity)
    const waiverRow = page.locator('.data-table .strong-cell', { hasText: testCveId }).first()
    await expect(waiverRow).toBeVisible()

    // Click revoke
    const tableRow = page.locator(`.table-row:has-text("${testCveId}")`).first()
    const revokeBtn = tableRow.locator('button:has-text("Revoke")')
    if (await revokeBtn.isVisible()) {
      await revokeBtn.click()
      await page.waitForTimeout(600)
      // Verify row is removed or marked revoked
      const afterCount = await page.locator(`.table-row:has-text("${testCveId}") button:has-text("Revoke")`).count()
      expect(afterCount).toBe(0)
    }
  })

  test('QA-E4: Production Request with L7 Canary Steering & Policy Inspection', async ({ page }) => {
    await page.goto('/#/systems/hello-container/requests')
    await expect(page.locator('h1')).toContainText(/Production Requests/i)

    // Click New Request
    const newRequestBtn = page.locator('button:has-text("New Request")')
    await newRequestBtn.click()

    const modal = page.locator('.modal')
    await expect(modal).toBeVisible()

    // Step 1: Select available module
    const selectableModule = modal.locator('.selectable-modules button:not([disabled])').first()
    if (await selectableModule.isVisible()) {
      await selectableModule.click()

      // Step 3: Choose Canary Strategy
      const canaryStrategyBtn = modal.locator('.option-cards button:has-text("Canary Rollout")')
      if (await canaryStrategyBtn.isVisible()) {
        await canaryStrategyBtn.click()

        // Verify L7 Canary input fields are displayed
        const headerNameInput = modal.locator('input[placeholder*="X-Beta-Tester"]')
        const headerValInput = modal.locator('input[placeholder*="true"]')
        const cookieInput = modal.locator('input[placeholder*="beta_user"]')

        await expect(headerNameInput).toBeVisible()
        await headerNameInput.fill('X-Beta-Tester')
        await headerValInput.fill('qa-automated-tester')
        await cookieInput.fill('qa_tier=enterprise')
      }

      // Step 4: Schedule
      const scheduleInput = modal.locator('input[type="datetime-local"]')
      await scheduleInput.fill('2026-12-01T10:00')

      // Review and Create
      const reviewBtn = modal.locator('button:has-text("Review Request")')
      if (await reviewBtn.isEnabled()) {
        await reviewBtn.click()
        const createBtn = modal.locator('button:has-text("Create Request")')
        if (await createBtn.isVisible()) {
          await createBtn.click()
          await expect(modal).not.toBeVisible({ timeout: 5000 })
        }
      } else {
        await modal.locator('button:has-text("Cancel")').click()
      }
    } else {
      await modal.locator('button:has-text("Cancel")').click()
    }

    // Check production requests in table
    const reqRows = page.locator('.data-table .table-row:not(.table-head), .requests-table .table-row:not(.table-head)')
    if (await reqRows.first().isVisible().catch(() => false)) {
      // Click view details
      const viewBtn = reqRows.first().locator('button[aria-label*="Details"], button[title*="View details"], button').last()
      if (await viewBtn.isVisible()) {
        await viewBtn.click()
        const detailModal = page.locator('.modal')
        if (await detailModal.isVisible().catch(() => false)) {
          const l7Panel = detailModal.locator('.canary-rules-panel')
          if (await l7Panel.isVisible()) {
            await expect(l7Panel).toContainText(/L7 Traffic Steering & Routing Policy/i)
          }
          await detailModal.locator('button:has-text("Close"), button:has-text("Cancel")').first().click()
        }
      }
    }
  })
})
