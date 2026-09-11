import { test, expect } from '@playwright/test'

test.describe('Comprehensive UI & Feature Testing across all 3 Systems + DCIM Integration', () => {
  test('Complete walk-through of all UI buttons, pages, and workflows', async ({ page }) => {
    // 1. INITIAL NAVIGATION & CLEAN SESSION
    await page.goto('http://localhost:5173/')
    await page.evaluate(() => window.sessionStorage.clear())
    await page.goto('http://localhost:5173/#/login')
    
    // Select Dev Token mode if on login page
    const loginHeading = page.locator('h1')
    if (await loginHeading.textContent() !== 'Dashboard') {
      const tokenButton = page.locator('button:has-text("Dev Token")')
      if (await tokenButton.isVisible()) {
        await tokenButton.click()
      }
      const tokenInput = page.locator('input[placeholder*="token"]')
      if (await tokenInput.isVisible()) {
        await tokenInput.fill('netci_dev_secret_token_12345')
      }
      await page.click('button:has-text("Sign in")')
    }
    await expect(page.locator('.brand')).toBeVisible()

    // 2. DASHBOARD PAGE
    await page.click('button:has-text("Dashboard")')
    await expect(page.locator('.kpi-grid')).toBeVisible()
    
    const kpiCards = page.locator('.kpi-card')
    expect(await kpiCards.count()).toBeGreaterThanOrEqual(4)

    // 3. SYSTEMS PAGE - TESTING ALL 3 SYSTEMS
    await page.click('button:has-text("Systems")')
    await expect(page.locator('h1')).toContainText('Systems')

    // Test Search input
    const systemSearch = page.locator('input[placeholder*="Tìm kiếm hệ thống"]')
    await systemSearch.fill('container')
    await expect(page.locator('.systems-table button:has-text("hello-container")')).toBeVisible()
    await systemSearch.fill('')

    // System 1: hello-container
    await page.click('.systems-table button:has-text("hello-container")')
    await expect(page.locator('h1')).toContainText('hello-container')
    await page.click('button:has-text("All Systems")')

    // System 2: hello-kubernetes
    await page.click('.systems-table button:has-text("hello-kubernetes")')
    await expect(page.locator('h1')).toContainText('hello-kubernetes')
    await page.click('button:has-text("All Systems")')

    // System 3: hello-systemd-go
    await page.click('.systems-table button:has-text("hello-systemd-go")')
    await expect(page.locator('h1')).toContainText('hello-systemd-go')
    await page.click('button:has-text("All Systems")')

    // CREATE NEW SYSTEM VIA DCIM SEARCH
    await page.click('button:has-text("New System")')
    await expect(page.locator('.modal h2')).toContainText('Create new system')
    const dcimTab = page.locator('button:has-text("Tìm từ DCIM")')
    if (await dcimTab.isVisible()) {
      await dcimTab.click()
    }
    await page.fill('input[placeholder*="Search by service name"]', 'billing')
    await page.click('.modal button:has-text("Search")')
    const dcimResult = page.locator('.dcim-result').first()
    if (await dcimResult.isVisible()) {
      await dcimResult.click()
      await page.click('.modal button:has-text("Create System")')
    }
    
    // If modal is still visible due to duplicate or error, click Cancel
    const modalCancel = page.locator('.modal button:has-text("Cancel")')
    if (await modalCancel.isVisible()) {
      await modalCancel.click()
    }
    await expect(page.locator('.modal')).toBeHidden()

    // 4. PRODUCTION REQUESTS PAGE (inside hello-container)
    await page.goto('http://localhost:5173/#/systems/hello-container/requests')
    await expect(page.locator('h1')).toContainText('Production Requests')

    // Filter controls
    const prSearch = page.locator('input[placeholder*="Search request ID"]')
    await prSearch.fill('PR-')
    await prSearch.fill('')

    // Create New Request
    await page.click('button:has-text("New Request")')
    await expect(page.locator('.modal h2')).toContainText('New Production Request')
    
    // Check if Review Request button is enabled
    const reviewBtn = page.locator('button:has-text("Review Request")')
    if (await reviewBtn.isEnabled()) {
      await reviewBtn.click()
      await page.click('button:has-text("Create Request")')
    } else {
      await page.click('button:has-text("Cancel")')
    }

    // View Details on existing request & test action buttons
    const viewButton = page.locator('.requests-table .row-actions button').first()
    if (await viewButton.isVisible()) {
      await viewButton.click()
      const modal = page.locator('.modal')
      if (await modal.isVisible().catch(() => false)) {
        const approveButton = page.locator('.modal button:has-text("Approve")')
        if (await approveButton.isVisible()) {
          await approveButton.click()
        } else {
          const closeBtn = page.locator('.modal button:has-text("Cancel"), .modal button[aria-label="Close"], .modal button:has-text("Close")').first()
          if (await closeBtn.isVisible()) {
            await closeBtn.click()
          }
        }
      }
    }

    // 5. SERVERS PAGE
    await page.goto('http://localhost:5173/#/servers')
    await expect(page.locator('h1')).toHaveText(/Deployment targets|Servers/)
    await expect(page.locator('.servers-table')).toBeVisible()

    // Test Server status filtering
    const statusSelect = page.locator('select').first()
    if (await statusSelect.isVisible()) {
      await statusSelect.selectOption({ index: 0 })
    }
  })
})
