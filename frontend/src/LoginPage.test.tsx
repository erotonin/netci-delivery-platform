import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { LoginPage } from './LoginPage'

describe('LoginPage', () => {
  it('creates the local SSO preview session from the artifact entry point', async () => {
    const onLogin = vi.fn()
    render(<LoginPage onLogin={onLogin} />)

    await userEvent.click(screen.getByRole('button', { name: /SSO/i }))

    expect(onLogin).toHaveBeenCalledWith({
      displayName: 'Admin',
      email: 'admin@netchat.io',
      method: 'local-sso',
    })
  })

  it('validates credentials and never returns the password in the session', async () => {
    const user = userEvent.setup()
    const onLogin = vi.fn()
    render(<LoginPage onLogin={onLogin} />)

    await user.click(screen.getByRole('button', { name: /mật khẩu/i }))
    await user.click(screen.getByRole('button', { name: /^Đăng nhập$/i }))
    expect(screen.getByRole('alert').textContent).toMatch(/đầy đủ/i)

    await user.type(screen.getByPlaceholderText('ten.dang.nhap'), 'Trung TT')
    await user.type(screen.getByPlaceholderText('••••••••'), 'secret-value')
    await user.click(screen.getByRole('button', { name: /^Đăng nhập$/i }))

    expect(onLogin).toHaveBeenCalledWith({
      displayName: 'Trung TT',
      email: 'trung.tt@netchat.io',
      method: 'local-password',
    })
    expect(JSON.stringify(onLogin.mock.calls[0][0])).not.toContain('secret-value')
  })
})
