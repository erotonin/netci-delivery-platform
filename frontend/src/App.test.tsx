import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    getPortalDashboard: vi.fn().mockResolvedValue({}),
    listSystems: vi.fn().mockResolvedValue([{ id: 'netChat', status: 'healthy', modules: [{ id: 'notification-worker', name: 'Notification Worker' }] }]),
    // The Portal asks the API who the caller is; here netCI reports that it runs with
    // authentication disabled, which is the local-development path.
    whoami: vi.fn().mockResolvedValue({
      principal: { subject: 'anonymous', displayName: 'Anonymous (auth disabled)', email: '', roles: ['viewer', 'developer', 'reviewer', 'platform-admin'], method: 'none' },
      authMode: 'none',
      separationOfDuties: false,
    }),
  }
})

vi.mock('./GeneralPages', () => ({
  DashboardPage: () => <div>Dashboard test page</div>,
  ServersPage: () => <div>Servers test page</div>,
  SystemPage: () => <div>System test page</div>,
  SystemsPage: () => <div>Systems test page</div>,
}))
vi.mock('./ModulePage', () => ({ ModulePage: () => <div>Module test page</div> }))
vi.mock('./ModuleSettings', () => ({ ModuleSettings: () => <div>Settings test page</div> }))
vi.mock('./NewModuleWizard', () => ({ NewModuleWizard: () => <div>Wizard test page</div> }))
vi.mock('./ProductionRequestsPage', () => ({ ProductionRequestsPage: () => <div>Requests test page</div> }))

import App from './App'

describe('App authentication and navigation', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '#/login')
  })

  it('guards the portal, stores a session, supports search navigation and logs out', async () => {
    const user = userEvent.setup()
    render(<App />)

    // With NETCI_AUTH_MODE=none the server answers /me without a credential, so the
    // Portal goes straight in rather than showing a login form that asks for nothing.
    await screen.findByText('Dashboard test page')
    const stored = JSON.parse(window.sessionStorage.getItem('netci.auth-session') ?? '{}')
    expect(stored.identity.authMode).toBe('none')
    expect(stored.token).toBeNull()

    const search = screen.getByRole('textbox', { name: /Tìm kiếm toàn cục/i })
    await user.type(search, 'Notification Worker{Enter}')
    await screen.findByText('Module test page')
    expect(window.location.hash).toBe('#/systems/netChat/modules/notification-worker')

    await user.type(search, 'Servers{Enter}')
    await screen.findByText('Servers test page')
    expect(window.location.hash).toBe('#/servers')

    // No logout control exists when netCI runs without authentication: there is no
    // credential to drop, and a button that signs the user straight back in is a lie.
    expect(screen.queryByRole('button', { name: /Đăng xuất/i })).toBeNull()
    expect(screen.getAllByText(/Chưa bật xác thực/i).length).toBeGreaterThan(0)
  })
})
