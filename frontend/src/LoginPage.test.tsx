import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { LoginPage } from './LoginPage'
import { NetciApiError, request, whoami } from './api/netciClient'

vi.mock('./api/netciClient', async () => {
  const actual = await vi.importActual<typeof import('./api/netciClient')>('./api/netciClient')
  return { ...actual, whoami: vi.fn(), request: vi.fn() }
})

const whoamiMock = vi.mocked(whoami)
const requestMock = vi.mocked(request)

const identity = (mode: string, roles: string[] = ['developer'], teams: string[] = ['payments']) => ({
  principal: { subject: 'dana', displayName: 'Dana Developer', email: 'dana@corp.example', roles, teams, method: mode },
  authMode: mode,
  separationOfDuties: true,
})

describe('LoginPage', () => {
  beforeEach(() => {
    window.location.hash = ''
    whoamiMock.mockReset()
    requestMock.mockReset()
    // /auth/config: token mode unless a test says otherwise.
    requestMock.mockResolvedValue({ authMode: 'token', oidc: null })
  })

  it('asks the server how it is configured before drawing a form', async () => {
    render(<LoginPage onLogin={vi.fn()} />)
    // /me is not asked in token mode: it would only answer 401. /auth/config decides.
    expect(whoamiMock).not.toHaveBeenCalled()

    // No credential field exists until the server says one is needed. A login box that
    // appears regardless is the theatre this screen used to be.
    expect(screen.queryByLabelText('Access token')).toBeNull()
    await waitFor(() => expect(screen.getByLabelText('Access token')).toBeTruthy())
  })

  it('signs in straight through when netCI runs without authentication', async () => {
    const onLogin = vi.fn()
    whoamiMock.mockResolvedValue(identity('none', ['platform-admin'], []))
    requestMock.mockResolvedValue({ authMode: 'none', oidc: null })
    render(<LoginPage onLogin={onLogin} />)

    await waitFor(() => expect(onLogin).toHaveBeenCalledWith({ token: null, identity: identity('none', ['platform-admin'], []) }))
    expect(screen.queryByLabelText('Access token')).toBeNull()
  })

  it('distinguishes a reachable API with unsafe auth configuration from an outage', async () => {
    // The server says "no auth" but refuses the proxied caller on /me: unsafe, not down.
    requestMock.mockResolvedValue({ authMode: 'none', oidc: null })
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
    render(<LoginPage onLogin={vi.fn()} />)
    const field = await screen.findByLabelText('Access token')

    await user.type(field, 'super-secret-token')
    expect(field.getAttribute('type')).toBe('password')
    expect(document.body.textContent).not.toContain('super-secret-token')
  })
})


describe('LoginPage with OIDC', () => {
  const oidc = {
    issuer: 'http://idp.example/realms/netci',
    clientId: 'netci-portal',
    authorizationEndpoint: 'http://idp.example/realms/netci/protocol/openid-connect/auth',
    tokenEndpoint: 'http://idp.example/realms/netci/protocol/openid-connect/token',
    scopes: ['openid', 'profile', 'email'],
    pkce: 'S256',
  }

  beforeEach(() => {
    window.location.hash = ''
    whoamiMock.mockReset()
    requestMock.mockReset()
    window.sessionStorage.clear()
  })

  it('offers SSO only when the server names a provider, and sends the browser there with PKCE', async () => {
    const user = userEvent.setup()
    requestMock.mockResolvedValue({ authMode: 'oidc', oidc })
    const assign = vi.fn()
    const original = window.location
    Object.defineProperty(window, 'location', { configurable: true, value: { ...original, assign, origin: 'http://127.0.0.1:4173', pathname: '/', search: '', hash: '' } })
    try {
      render(<LoginPage onLogin={vi.fn()} />)
      const button = await screen.findByTestId('sso-login')
      await user.click(button)
      await waitFor(() => expect(assign).toHaveBeenCalledTimes(1))
      const target = new URL(assign.mock.calls[0][0] as string)
      expect(`${target.origin}${target.pathname}`).toBe(oidc.authorizationEndpoint)
      expect(target.searchParams.get('client_id')).toBe('netci-portal')
      expect(target.searchParams.get('response_type')).toBe('code')
      expect(target.searchParams.get('code_challenge_method')).toBe('S256')
      expect(target.searchParams.get('redirect_uri')).toBe('http://127.0.0.1:4173/')
      // The verifier never travels; only its hash does.
      expect(target.searchParams.get('code_challenge')).not.toBe(window.sessionStorage.getItem('netci.oidc.verifier'))
      expect(window.sessionStorage.getItem('netci.oidc.state')).toBe(target.searchParams.get('state'))
      // The paste-a-token fallback stays available.
      expect(screen.getByLabelText('Access token')).toBeTruthy()
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: original })
    }
  })

  it('refuses a callback whose state this tab did not issue', async () => {
    requestMock.mockResolvedValue({ authMode: 'oidc', oidc })
    window.sessionStorage.setItem('netci.oidc.state', 'expected')
    window.sessionStorage.setItem('netci.oidc.verifier', 'v')
    const original = window.location
    Object.defineProperty(window, 'location', { configurable: true, value: { ...original, origin: 'http://127.0.0.1:4173', pathname: '/', search: '?code=abc&state=forged', hash: '' } })
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    try {
      const onLogin = vi.fn()
      render(<LoginPage onLogin={onLogin} />)
      await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/state mismatch/))
      expect(onLogin).not.toHaveBeenCalled()
      // No code exchange happened: the token endpoint was never called.
      expect(fetchSpy.mock.calls.filter(([url]) => String(url).includes('/token'))).toHaveLength(0)
    } finally {
      fetchSpy.mockRestore()
      Object.defineProperty(window, 'location', { configurable: true, value: original })
    }
  })
})
