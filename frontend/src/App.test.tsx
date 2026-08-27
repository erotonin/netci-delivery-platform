import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    getPortalDashboard: vi.fn().mockResolvedValue({}),
    listSystems: vi.fn().mockResolvedValue([{ id: 'netChat', status: 'healthy', modules: [{ id: 'notification-worker', name: 'Notification Worker' }] }]),
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

    expect(screen.getByRole('heading', { name: /Track every release/i })).toBeTruthy()
    await user.click(screen.getByRole('button', { name: /SSO/i }))
    await screen.findByText('Dashboard test page')
    expect(JSON.parse(window.sessionStorage.getItem('netci.auth-session') ?? '{}').method).toBe('local-sso')

    const search = screen.getByRole('textbox', { name: /Tìm kiếm toàn cục/i })
    await user.type(search, 'Notification Worker{Enter}')
    await screen.findByText('Module test page')
    expect(window.location.hash).toBe('#/systems/netChat/modules/notification-worker')

    await user.type(search, 'Servers{Enter}')
    await screen.findByText('Servers test page')
    expect(window.location.hash).toBe('#/servers')

    await user.click(screen.getByRole('button', { name: /Đăng xuất/i }))
    await waitFor(() => expect(window.sessionStorage.getItem('netci.auth-session')).toBeNull())
    expect(window.location.hash).toBe('#/login')
    expect(screen.getByRole('button', { name: /SSO/i })).toBeTruthy()
  })
})
