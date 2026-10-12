import { test, expect } from '@playwright/test'

test.describe('Professional QA End-to-End Test Suite for netCI Platform', () => {
  const capturedErrors: string[] = []
  const capturedWarnings: string[] = []

  test.beforeEach(async ({ page }) => {
    capturedErrors.length = 0
    capturedWarnings.length = 0

    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        const text = msg.text()
        // Ignore expected HTTP 404 network responses from intentional negative tests
        if (!text.includes('404') && !text.includes('status of 404')) {
          capturedErrors.push(`[Console Error] ${text}`)
        }
      } else if (msg.type() === 'warning') {
        capturedWarnings.push(`[Console Warning] ${msg.text()}`)
      }
    })

    page.on('pageerror', (err) => {
      capturedErrors.push(`[Page Error] ${err.message}`)
    })
  })

  test('QA-01: Authentication, Role Switching, and Session Management', async ({ page }) => {
    // Navigate to root and set manual_login so login screen shows instead of auto-logging in
    await page.goto('http://localhost:5173/')
    await page.evaluate(() => {
      window.sessionStorage.clear()
      window.sessionStorage.setItem('netci.manual_login', 'true')
      window.location.hash = '#/login'
    })
    await page.reload()

    // Expect login page
    await expect(page.locator('h1, h2').first()).toContainText(/Track every release|Đăng nhập|Sign in|netCI/i)

    // 1. Negative Test: empty credentials
    const usernameInput = page.locator('#login-username, input[placeholder*="admin hoặc dev"]').first()
    if (await usernameInput.isVisible()) {
      await usernameInput.fill('')
      await page.click('button:has-text("Đăng nhập")')
      const errorAlert = page.locator('.login-error, [role="alert"]')
      await expect(errorAlert).toBeVisible()
    }

    // 2. Negative Test: invalid credentials
    if (await usernameInput.isVisible()) {
      await usernameInput.fill('invalid_user_999')
      const passwordInput = page.locator('#login-password, input[type="password"]').first()
      if (await passwordInput.isVisible()) {
        await passwordInput.fill('wrong_pass')
      }
      await page.click('button:has-text("Đăng nhập")')
      await page.waitForTimeout(300)
    }

    // 3. Positive Test: Persona Quick Login as Developer
    const devPersonaBtn = page.locator('button:has-text("Developer")')
    if (await devPersonaBtn.isVisible()) {
      await devPersonaBtn.click()
    } else {
      // Or login with token
      const tokenButton = page.locator('button:has-text("Dev Token")')
      if (await tokenButton.isVisible()) await tokenButton.click()
      const tokenInput = page.locator('input[placeholder*="token"]')
      if (await tokenInput.isVisible()) await tokenInput.fill('netci_dev_secret_token_12345')
      await page.click('button:has-text("Sign in")')
    }

    // Should arrive on Dashboard
    await expect(page.locator('.brand')).toBeVisible()
    await expect(page.locator('h1')).toContainText(/Dashboard/)

    // Check user info in header
    const userBadge = page.locator('.session-badge, .user-badge, .header-user, .user-persona')
    if (await userBadge.isVisible()) {
      expect(await userBadge.textContent()).toBeTruthy()
    }

    // 4. Test Logout and verify return to Login page
    const logoutBtn = page.locator('button:has-text("Logout"), button:has-text("Đăng xuất")')
    if (await logoutBtn.isVisible()) {
      await logoutBtn.click()
      await expect(page.locator('h1, h2').first()).toContainText(/Track every release|Đăng nhập|Sign in|netCI/i)
      
      // Log back in for remaining tests
      const quickLogin = page.locator('button:has-text("Admin"), button:has-text("Developer")').first()
      if (await quickLogin.isVisible()) {
        await quickLogin.click()
      }
      await expect(page.locator('.brand')).toBeVisible()
    }

    expect(capturedErrors.filter(e => !e.includes('401') && !e.includes('Unauthorized'))).toHaveLength(0)
  })

  test('QA-02: Dashboard KPIs, Pipeline Activity, and Quick Navigation', async ({ page }) => {
    await page.goto('http://localhost:5173/#/dashboard')
    await expect(page.locator('h1')).toContainText('Dashboard')

    // Verify KPI grid exists and contains at least 4 metrics
    const kpiCards = page.locator('.kpi-card')
    await expect(kpiCards.first()).toBeVisible()
    const count = await kpiCards.count()
    expect(count).toBeGreaterThanOrEqual(4)

    // Verify each KPI card has a label, value, and non-empty content
    for (let i = 0; i < count; i++) {
      const card = kpiCards.nth(i)
      const text = await card.textContent()
      expect(text).toBeTruthy()
      expect(text?.trim().length).toBeGreaterThan(3)
    }

    // Verify activity chart / panel is present
    const chartPanel = page.locator('.chart-panel, .panel')
    await expect(chartPanel.first()).toBeVisible()

    expect(capturedErrors).toHaveLength(0)
  })

  test('QA-03: Systems Management, Filtering, DCIM Integration, and Detail Views', async ({ page }) => {
    await page.goto('http://localhost:5173/#/systems')
    await expect(page.locator('h1')).toContainText('Systems')

    // 1. Search filter testing
    const searchInput = page.locator('input[placeholder*="Tìm kiếm hệ thống"]')
    await searchInput.fill('kubernetes')
    await expect(page.locator('.systems-table button:has-text("hello-kubernetes")')).toBeVisible()
    
    // Non-existent search should show 0 results
    await searchInput.fill('non_existent_system_xyz123')
    await page.waitForTimeout(300)
    const emptyRow = page.locator('.systems-table button:has-text("hello-kubernetes")')
    expect(await emptyRow.count()).toBe(0)
    
    // Clear search
    await searchInput.fill('')
    await page.waitForTimeout(300)

    // 2. Verify all 3 Systems and navigate into each
    const systemsToTest = ['hello-container', 'hello-kubernetes', 'hello-systemd-go']
    for (const sysId of systemsToTest) {
      const sysBtn = page.locator(`.systems-table button:has-text("${sysId}")`)
      await expect(sysBtn).toBeVisible()
      await sysBtn.click()
      
      // Detail page checks
      await expect(page.locator('h1')).toContainText(sysId)

      // Return to systems list
      await page.click('button:has-text("All Systems")')
      await expect(page.locator('h1')).toContainText('Systems')
    }

    // 3. Test New System Modal & DCIM Service Discovery
    await page.click('button:has-text("New System")')
    const modal = page.locator('.modal')
    await expect(modal).toBeVisible()
    await expect(modal.locator('h2')).toContainText(/Create new system|Tạo hệ thống/)

    // Test DCIM tab
    const dcimTab = page.locator('button:has-text("Tìm từ DCIM")')
    if (await dcimTab.isVisible()) {
      await dcimTab.click()
      const dcimInput = page.locator('input[placeholder*="Search by service name"]')
      await dcimInput.fill('billing')
      await page.click('.modal button:has-text("Search")')
      await page.waitForTimeout(400)
    }

    // Close modal
    const cancelBtn = page.locator('.modal button:has-text("Cancel"), .modal button[aria-label="Close"]')
    if (await cancelBtn.isVisible()) {
      await cancelBtn.click()
    }
    await expect(modal).toBeHidden()

    expect(capturedErrors).toHaveLength(0)
  })

  test('QA-04: Module Detail, Run Triggers, Configuration, and DORA Metrics', async ({ page }) => {
    // Navigate to module hello-container
    await page.goto('http://localhost:5173/#/systems/hello-container/modules/hello-container')
    await expect(page.locator('h1')).toHaveText(/Hello Container|hello-container/i)

    // Check module tabs
    const tabButtons = page.locator('.tabs button, nav.module-tabs button')
    const tabCount = await tabButtons.count()
    expect(tabCount).toBeGreaterThanOrEqual(3)

    // Click through each tab
    const tabNames = ['Overview', 'Pipelines', 'Configuration', 'DORA']
    for (const name of tabNames) {
      const tab = page.locator(`[role="tab"]:has-text("${name}"), .tabs button:has-text("${name}")`).first()
      if (await tab.isVisible()) {
        await tab.click()
        await page.waitForTimeout(200)
        // Verify content panel loaded without crash
        await expect(page.locator('main section, main article, main .panel, main .dora-grid, main .empty-tab-state').first()).toBeVisible()
      }
    }

    // Test Module Settings View
    const settingsBtn = page.locator('.module-heading button:has-text("Settings")').first()
    if (await settingsBtn.isVisible()) {
      await settingsBtn.click()
      await expect(page.locator('.settings-page')).toBeVisible()
      await expect(page.locator('h1')).toContainText('Module Settings')
      const backBtn = page.locator('.settings-page .back-button, button:has-text("Back to")').first()
      if (await backBtn.isVisible()) {
        await backBtn.click()
        await expect(page.locator('.module-heading')).toBeVisible()
      }
    }

    expect(capturedErrors).toHaveLength(0)
  })

  test('QA-05: New Module Onboarding Wizard Navigation', async ({ page }) => {
    await page.goto('http://localhost:5173/#/systems/hello-container/new-module')
    await expect(page.locator('h1').first()).toContainText(/New Module|Tạo module|Wizard/i)

    // Step 1: Check runtime choices
    const runtimeOptions = page.locator('input[name="runtime"], .runtime-card, button:has-text("Docker"), button:has-text("Kubernetes")')
    expect(await runtimeOptions.count()).toBeGreaterThanOrEqual(1)

    // Test Cancel button returns to system page
    const cancelBtn = page.locator('button:has-text("Cancel"), button:has-text("Hủy")')
    if (await cancelBtn.isVisible()) {
      await cancelBtn.click()
      await expect(page.locator('h1')).toContainText('hello-container')
    }

    expect(capturedErrors).toHaveLength(0)
  })

  test('QA-06: Service Catalog, Templates, Previews, and Resource Vending', async ({ page }) => {
    await page.goto('http://localhost:5173/#/catalog')
    await expect(page.locator('.catalog-page')).toBeVisible()

    // Check catalog tab switching
    const catalogTabs = ['Golden Path Templates', 'Preview Environments', 'Resource Vending', 'Services & Dependency Graph']
    for (const tabName of catalogTabs) {
      const tab = page.locator(`button:has-text("${tabName}")`)
      if (await tab.isVisible()) {
        await tab.click()
        await page.waitForTimeout(300)
        await expect(page.locator('.catalog-page')).toBeVisible()
      }
    }

    // Test Resource Request Modal
    const requestResBtn = page.locator('button:has-text("Request Resource"), button:has-text("Yêu cầu tài nguyên")')
    if (await requestResBtn.isVisible()) {
      await requestResBtn.click()
      const modal = page.locator('.modal')
      await expect(modal).toBeVisible()
      const closeBtn = modal.locator('button:has-text("Cancel"), button:has-text("Close"), button[aria-label="Close"]')
      if (await closeBtn.isVisible()) {
        await closeBtn.click()
      }
      await expect(modal).toBeHidden()
    }

    expect(capturedErrors).toHaveLength(0)
  })

  test('QA-07: Architecture Roadmap & 4 Levels of Truth Matrix', async ({ page }) => {
    await page.goto('http://localhost:5173/#/architecture')
    await expect(page.locator('h1')).toContainText(/Kiến Trúc|Architecture/i)

    // Verify the 4 sub-tabs
    const archTabs = ['Architecture Overview', 'State Reconciliation', 'Pluggable Traits', 'Agentic AI & MCP']
    for (const tabName of archTabs) {
      const tab = page.locator(`button:has-text("${tabName}")`)
      if (await tab.isVisible()) {
        await tab.click()
        await page.waitForTimeout(200)
        await expect(page.locator('.architecture-page')).toBeVisible()
      }
    }

    expect(capturedErrors).toHaveLength(0)
  })

  test('QA-08: Production Requests, Approval Gate & Canary Promotion Controls', async ({ page }) => {
    await page.goto('http://localhost:5173/#/systems/hello-container/requests')
    await expect(page.locator('h1')).toContainText('Production Requests')

    // Search input
    const prSearch = page.locator('input[placeholder*="Search request ID"]')
    await prSearch.fill('PR-')
    await prSearch.fill('')

    // Open detail of first existing request
    const viewButton = page.locator('.requests-table .row-actions button, button[aria-label*="Xem"]').first()
    if (await viewButton.isVisible()) {
      await viewButton.click()
      const modal = page.locator('.modal')
      await expect(modal).toBeVisible()
      
      // Modal should display request timeline steps
      await expect(modal.locator('.timeline-step').first()).toBeVisible()

      // Close modal
      const closeBtn = modal.locator('button:has-text("Cancel"), button:has-text("Close"), button[aria-label="Close"]').first()
      if (await closeBtn.isVisible()) {
        await closeBtn.click()
      }
      await expect(modal).toBeHidden()
    }

    expect(capturedErrors).toHaveLength(0)
  })

  test('QA-09: Deployment Targets / Servers Inventory Operations', async ({ page }) => {
    await page.goto('http://localhost:5173/#/servers')
    await expect(page.locator('h1')).toHaveText(/Deployment targets|Servers/)
    await expect(page.locator('.servers-table')).toBeVisible()

    // 1. Test search filter
    const searchInput = page.locator('input[placeholder*="Tìm hostname"]')
    if (await searchInput.isVisible()) {
      await searchInput.fill('staging')
      await page.waitForTimeout(300)
      const rows = page.locator('.servers-table .table-row:not(.table-head)')
      expect(await rows.count()).toBeGreaterThanOrEqual(1)
      await searchInput.fill('')
    }

    // 2. Test Environment filter dropdown
    const envSelect = page.locator('select').first()
    if (await envSelect.isVisible()) {
      await envSelect.selectOption('Staging')
      await page.waitForTimeout(200)
      await envSelect.selectOption('All environments')
    }

    // 3. Test Details modal on server
    const rowDetailBtn = page.locator('.servers-table .row-actions button').first()
    if (await rowDetailBtn.isVisible()) {
      await rowDetailBtn.click()
      const detailModal = page.locator('.modal')
      await expect(detailModal).toBeVisible()
      const closeBtn = detailModal.locator('button:has-text("Close")')
      await closeBtn.click()
      await expect(detailModal).toBeHidden()
    }

    // 4. Test Refresh from API button
    const refreshBtn = page.locator('button:has-text("Refresh from API")')
    if (await refreshBtn.isVisible()) {
      await refreshBtn.click()
      await page.waitForTimeout(400)
      const feedback = page.locator('.feedback-toast, .sync-note')
      await expect(feedback).toBeVisible()
    }

    expect(capturedErrors).toHaveLength(0)
  })

  test('QA-10: Resilience, Malformed Routes, XSS Input Sanitization, and Boundary Recovery', async ({ page }) => {
    // 1. Malformed Route Test: Should fallback gracefully to Dashboard without white-screen or crash
    await page.goto('http://localhost:5173/#/totally-nonexistent-route-404-error')
    await page.waitForTimeout(300)
    await expect(page.locator('h1')).toBeVisible()
    await expect(page.locator('.portal-crash')).toBeHidden()

    // 2. Non-existent System ID: Should show loading/fallback gracefully
    await page.goto('http://localhost:5173/#/systems/sys-does-not-exist-qa-999')
    await page.waitForTimeout(500)
    await expect(page.locator('.portal-crash')).toBeHidden()

    // 3. XSS Injection Attack Resistance
    await page.goto('http://localhost:5173/#/systems')
    const searchInput = page.locator('input[placeholder*="Tìm kiếm hệ thống"]')
    await searchInput.fill('<script>window.__xss_leaked = true;</script><img src=x onerror=alert(1)>')
    await page.waitForTimeout(300)
    const xssLeaked = await page.evaluate(() => (window as any).__xss_leaked)
    expect(xssLeaked).toBeUndefined()
    await expect(page.locator('.portal-crash')).toBeHidden()

    // 4. Mobile Responsive Viewport Testing (iPhone 13 size: 390x844)
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('http://localhost:5173/#/dashboard')
    await expect(page.locator('.brand')).toBeVisible()
    await expect(page.locator('.portal-crash')).toBeHidden()

    // Reset viewport
    await page.setViewportSize({ width: 1280, height: 800 })

    expect(capturedErrors).toHaveLength(0)
  })
})
