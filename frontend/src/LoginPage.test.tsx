import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { LoginPage } from './LoginPage'
import { NetciApiError, whoami } from './api/netciClient'

vi.mock('./api/netciClient', async () => {
  const actual = await vi.importActual<typeof import('./api/netciClient')>('./api/netciClient')
  return { ...actual, whoami: vi.fn() }
})

const whoamiMock = vi.mocked(whoami)

const identity = (mode: string, roles: string[] = ['developer'], teams: string[] = ['payments']) => ({
  principal: { subject: 'dana', displayName: 'Dana Developer', email: 'dana@corp.example', roles, teams, method: mode },
  authMode: mode,
  separationOfDuties: true,
})

describe('LoginPage', () => {
  beforeEach(() => {
    window.location.hash = ''
    whoamiMock.mockReset()
  })

  it('asks the server how it is configured before drawing a form', async () => {
    whoamiMock.mockImplementationOnce(() => Promise.reject(new NetciApiError(401, null, 'unauthenticated')))
    render(<LoginPage onLogin={vi.fn()} />)

    // No credential field exists until the server says one is needed. A login box that
    // appears regardless is the theatre this screen used to be.
    expect(screen.queryByLabelText('Access token')).toBeNull()
    await waitFor(() => expect(screen.getByLabelText('Access token')).toBeTruthy())
  })

  it('signs in straight through when netCI runs without authentication', async () => {
    const onLogin = vi.fn()
    whoamiMock.mockResolvedValue(identity('none', ['platform-admin'], []))
    render(<LoginPage onLogin={onLogin} />)

    await waitFor(() => expect(onLogin).toHaveBeenCalledWith({ token: null, identity: identity('none', ['platform-admin'], []) }))
    expect(screen.queryByLabelText('Access token')).toBeNull()
  })

  it('distinguishes a reachable API with unsafe auth configuration from an outage', async () => {
    whoamiMock.mockImplementationOnce(() => Promise.reject(new NetciApiError(
      403,
      { code: 'AUTH_NOT_CONFIGURED' },
      'auth is not configured for a proxied caller',
    )))

    render(<LoginPage onLogin={vi.fn()} />)

    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/API đang chạy/i))
    expect(screen.getByRole('alert').textContent).toMatch(/NETCI_AUTH_MODE=token/)
    expect(screen.queryByLabelText('Access token')).toBeNull()
  })

  it('only creates a session from a token the server accepted', async () => {
    const user = userEvent.setup()
    const onLogin = vi.fn()
    whoamiMock.mockImplementationOnce(() => Promise.reject(new NetciApiError(401, null, 'unauthenticated')))
    render(<LoginPage onLogin={onLogin} />)
    const field = await screen.findByLabelText('Access token')

    // A rejected token must not become a session.
    whoamiMock.mockImplementationOnce(() => Promise.reject(new NetciApiError(401, null, 'unknown or revoked token')))
    await user.type(field, 'bad-token')
    await user.click(screen.getByRole('button', { name: /Đăng nhập/i }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/không hợp lệ|thu hồi/i))
    expect(onLogin).not.toHaveBeenCalled()

    whoamiMock.mockResolvedValueOnce(identity('token'))
    await user.clear(field)
    await user.type(field, 'good-token')
    await user.click(screen.getByRole('button', { name: /Đăng nhập/i }))

    await waitFor(() => expect(onLogin).toHaveBeenCalledWith({ token: 'good-token', identity: identity('token') }))
  })

  it('says so plainly when a valid token carries no netCI role', async () => {
    const user = userEvent.setup()
    const onLogin = vi.fn()
    whoamiMock.mockImplementationOnce(() => Promise.reject(new NetciApiError(401, null, 'unauthenticated')))
    render(<LoginPage onLogin={onLogin} />)
    const field = await screen.findByLabelText('Access token')

    whoamiMock.mockImplementationOnce(() => Promise.reject(new NetciApiError(403, null, 'no group maps to a netCI role')))
    await user.type(field, 'roleless-token')
    await user.click(screen.getByRole('button', { name: /Đăng nhập/i }))

    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/chưa được cấp quyền/i))
    expect(onLogin).not.toHaveBeenCalled()
  })

  it('never keeps the typed credential in the DOM as readable text', async () => {
    const user = userEvent.setup()
    whoamiMock.mockImplementationOnce(() => Promise.reject(new NetciApiError(401, null, 'unauthenticated')))
    render(<LoginPage onLogin={vi.fn()} />)
    const field = await screen.findByLabelText('Access token')

    await user.type(field, 'super-secret-token')
    expect(field.getAttribute('type')).toBe('password')
    expect(document.body.textContent).not.toContain('super-secret-token')
  })
})
