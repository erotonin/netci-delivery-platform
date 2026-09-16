import { useEffect, useState, type FormEvent } from 'react'
import { KeyRound, Loader2, LogIn, ShieldAlert, ShieldCheck, UserCheck, UserCog } from 'lucide-react'
import { NetciApiError, whoami, type Identity } from './api/netciClient'
import { beginOidcLogin, completeOidcLogin, fetchAuthConfig, isOidcCallback, type OidcBrowserConfig } from './auth/oidc'

/** The signed-in user, as the *server* described them. The browser never decides this. */
export type AuthSession = {
  token: string | null
  identity: Identity
  // Set when the session came from a browser login at the provider: where to end it.
  endSessionEndpoint?: string
}

export const displayNameOf = (session: AuthSession) => session.identity.principal.displayName
export const hasRole = (session: AuthSession, ...roles: string[]) =>
  session.identity.principal.roles.some((role) => roles.includes(role))

export function LoginPage({ onLogin }: { onLogin: (session: AuthSession) => void }) {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('admin')
  const [token, setToken] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [authMode, setAuthMode] = useState<string | null>(null)
  const [manualMode, setManualMode] = useState(() => window.sessionStorage.getItem('netci.manual_login') === 'true')
  const [oidc, setOidc] = useState<OidcBrowserConfig | null>(null)

  useEffect(() => {
    let cancelled = false
    whoami()
      .then((identity) => {
        if (cancelled) return
        setAuthMode(identity.authMode)
        if (identity.authMode === 'none' && !manualMode) {
          window.sessionStorage.removeItem('netci.manual_login')
          onLogin({ token: null, identity })
        }
      })
      .catch((cause) => {
        if (cancelled) return
        // A 401 says only "a credential is needed"; /auth/config says which kind, and
        // may already have answered -- never downgrade its answer to the generic one.
        if (cause instanceof NetciApiError && cause.status === 401) setAuthMode((current) => current ?? 'token')
        else if (cause instanceof NetciApiError && cause.code === 'AUTH_NOT_CONFIGURED') setAuthMode('auth-required')
        else setAuthMode('unreachable')
      })
    // How to sign in is the server's to say: the mode, and for OIDC the public client
    // and the provider's endpoints. The Portal bundle carries none of it.
    fetchAuthConfig()
      .then((config) => {
        if (cancelled) return
        if (config.authMode === 'token' || config.authMode === 'oidc') setAuthMode(config.authMode)
        setOidc(config.oidc)
      })
      .catch(() => { /* /me already told us whether the API is reachable */ })
    return () => {
      cancelled = true
    }
  }, [onLogin, manualMode])

  // Back from the identity provider: finish the code exchange, then log in with the
  // id_token exactly as a pasted token would -- netCI verifies it, not the Portal.
  useEffect(() => {
    if (!oidc || !isOidcCallback()) return
    let cancelled = false
    setBusy(true)
    completeOidcLogin(oidc)
      .then(async (idToken) => {
        const identity = await whoami(idToken)
        if (!cancelled) onLogin({ token: idToken, identity, endSessionEndpoint: oidc.endSessionEndpoint })
      })
      .catch((cause) => {
        if (cancelled) return
        if (cause instanceof NetciApiError && cause.status === 403) setError('Đăng nhập SSO thành công nhưng tài khoản chưa được cấp quyền nào trên netCI.')
        else setError(`Đăng nhập SSO thất bại: ${cause instanceof Error ? cause.message : String(cause)}`)
      })
      .finally(() => { if (!cancelled) setBusy(false) })
    return () => { cancelled = true }
  }, [oidc, onLogin])

  const signInWithProvider = async () => {
    if (!oidc) return
    setError('')
    try {
      await beginOidcLogin(oidc)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  const loginWithPersona = async (credential: string) => {
    setBusy(true)
    setError('')
    try {
      const identity = await whoami(credential)
      onLogin({ token: credential, identity })
    } catch (cause) {
      if (cause instanceof NetciApiError && cause.status === 401) {
        setError('Thông tin đăng nhập không hợp lệ hoặc đã bị thu hồi.')
      } else if (cause instanceof NetciApiError && cause.status === 403) {
        setError('Tài khoản hợp lệ nhưng chưa được cấp quyền nào trên netCI.')
      } else {
        setError('Không kết nối được tới netCI API. Kiểm tra dịch vụ và thử lại.')
      }
    } finally {
      setBusy(false)
    }
  }

  const handleCredentialsSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const trimmedUser = username.trim().toLowerCase()
    const trimmedPass = password.trim()
    if (!trimmedUser) {
      setError('Vui lòng nhập tên đăng nhập (admin hoặc dev).')
      return
    }
    // Check credentials for demo
    if (trimmedUser === 'admin' && trimmedPass === 'admin') {
      await loginWithPersona('admin')
      return
    }
    if (trimmedUser === 'dev' && trimmedPass === 'dev') {
      await loginWithPersona('dev')
      return
    }
    // If not demo credentials, try as token if authMode is token/oidc
    if (authMode === 'token' || authMode === 'oidc') {
      await loginWithPersona(trimmedPass || trimmedUser)
    } else {
      setError('Tài khoản hoặc mật khẩu không chính xác. Hãy dùng admin/admin hoặc dev/dev.')
    }
  }

  const signInToken = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!token.trim()) {
      setError('Nhập access token do platform team cấp.')
      return
    }
    await loginWithPersona(token.trim())
  }

  return <main className="login-page">
    <section className="login-story" aria-labelledby="login-story-title">
      <div className="login-brand"><span>R</span><strong>Release Portal</strong></div>
      <div className="login-story-copy">
        <span className="login-eyebrow"><ShieldCheck size={16} />netCI Platform · Continuous Delivery</span>
        <h1 id="login-story-title">Track every release, from commit to production.</h1>
        <p>Đăng nhập một lần để quản lý pipeline, phiên bản và yêu cầu triển khai trên tất cả hệ thống.</p>
      </div>
      <small>netCI Platform · Continuous Delivery</small>
    </section>
    <section className="login-panel">
      <div className="login-card">
        <div className="login-mark">R</div>
        <h2>Đăng nhập Hệ thống</h2>

        {authMode === null && <p className="login-checking" role="status"><Loader2 size={16} className="spin" />Đang kiểm tra cấu hình xác thực…</p>}

        {authMode === 'unreachable' && <div className="login-error" role="alert">
          <ShieldAlert size={16} />netCI API không phản hồi. Portal không thể đăng nhập khi chưa gọi được <code>/me</code>.
        </div>}

        {authMode === 'auth-required' && <div className="login-error" role="alert">
          <ShieldAlert size={16} />API đang chạy nhưng từ chối chế độ không xác thực qua proxy. Hãy cấu hình <code>NETCI_AUTH_MODE=token</code> hoặc <code>oidc</code> cho topology này.
        </div>}

        {authMode === 'none' && !manualMode && (
          <p className="login-checking" role="status"><Loader2 size={16} className="spin" />netCI đang chạy chế độ demo — đang vào Portal…</p>
        )}

        {(authMode === 'none' && manualMode) && (
          <>
            <p style={{ color: 'var(--text-muted)', fontSize: '0.9rem', marginBottom: '16px' }}>
              Chọn tài khoản demo để trải nghiệm cơ chế <strong>Phân tách quyền hạn (Separation of Duties)</strong>:
            </p>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', marginBottom: '20px' }}>
              <button
                type="button"
                className="login-primary"
                onClick={() => loginWithPersona('admin')}
                disabled={busy}
                style={{
                  background: 'linear-gradient(135deg, #1e3a8a, #3b82f6)',
                  borderColor: '#60a5fa',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'flex-start',
                  padding: '12px 16px',
                  gap: '12px',
                  textAlign: 'left'
                }}
              >
                <UserCog size={24} style={{ color: '#93c5fd' }} />
                <div>
                  <div style={{ fontWeight: 600, fontSize: '0.95rem' }}>1. Tài khoản: admin / admin</div>
                  <div style={{ fontSize: '0.8rem', opacity: 0.85 }}>Alexander Admin (Platform Lead & Reviewer - Duyệt Release)</div>
                </div>
              </button>

              <button
                type="button"
                className="login-primary"
                onClick={() => loginWithPersona('dev')}
                disabled={busy}
                style={{
                  background: 'linear-gradient(135deg, #064e3b, #10b981)',
                  borderColor: '#34d399',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'flex-start',
                  padding: '12px 16px',
                  gap: '12px',
                  textAlign: 'left'
                }}
              >
                <UserCheck size={24} style={{ color: '#a7f3d0' }} />
                <div>
                  <div style={{ fontWeight: 600, fontSize: '0.95rem' }}>2. Tài khoản: dev / dev</div>
                  <div style={{ fontSize: '0.8rem', opacity: 0.85 }}>David Developer (Tạo Module, Build CI, Gửi yêu cầu Release)</div>
                </div>
              </button>
            </div>

            <div style={{ position: 'relative', textAlign: 'center', margin: '16px 0' }}>
              <hr style={{ borderColor: 'rgba(255,255,255,0.1)' }} />
              <span style={{ position: 'absolute', top: '-10px', left: '50%', transform: 'translateX(-50%)', background: '#1e293b', padding: '0 8px', fontSize: '0.75rem', color: '#94a3b8' }}>HOẶC ĐĂNG NHẬP THỦ CÔNG</span>
            </div>

            <form className="login-form" onSubmit={handleCredentialsSubmit} noValidate>
              <div className="login-field">
                <label htmlFor="login-username">Tên đăng nhập (Username)</label>
                <input
                  id="login-username"
                  type="text"
                  autoComplete="username"
                  spellCheck={false}
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="admin hoặc dev"
                />
              </div>

              <div className="login-field">
                <label htmlFor="login-password">Mật khẩu (Password)</label>
                <input
                  id="login-password"
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="admin hoặc dev"
                />
              </div>

              {error && <div className="login-error" role="alert">{error}</div>}

              <button className="login-primary" type="submit" disabled={busy}>
                {busy ? <Loader2 size={18} className="spin" /> : <LogIn size={18} />}
                {busy ? 'Đang xác thực…' : 'Đăng nhập'}
              </button>
            </form>
          </>
        )}

        {authMode === 'oidc' && oidc && !oidc.error && (
          <div className="login-sso">
            <button className="login-primary" type="button" onClick={signInWithProvider} disabled={busy} data-testid="sso-login">
              {busy ? <Loader2 size={18} className="spin" /> : <LogIn size={18} />}
              {busy ? 'Đang xác thực…' : 'Đăng nhập bằng SSO'}
            </button>
            <small>Chuyển tới <code>{oidc.issuer}</code> để xác thực; netCI kiểm tra token trả về và quyết định quyền.</small>
          </div>
        )}
        {authMode === 'oidc' && oidc?.error && <div className="login-error" role="alert">
          <ShieldAlert size={16} />Identity provider <code>{oidc.issuer}</code> không phản hồi discovery; dán token bên dưới.
        </div>}

        {(authMode === 'token' || authMode === 'oidc') && <>
          <p>{authMode === 'oidc' ? 'Hoặc dán một token từ identity provider.' : 'Dán access token do platform team cấp.'} Quyền của bạn do netCI quyết định, không do Portal.</p>
          <form className="login-form" onSubmit={signInToken} noValidate>
            <div className="login-field">
              <label htmlFor="login-token">Access token</label>
              <input
                id="login-token"
                autoFocus
                type="password"
                autoComplete="off"
                spellCheck={false}
                value={token}
                onChange={(event) => setToken(event.target.value)}
                placeholder="Bearer token"
              />
            </div>
            {error && <div className="login-error" role="alert">{error}</div>}
            <button className="login-primary" type="submit" disabled={busy}>
              {busy ? <Loader2 size={18} className="spin" /> : <LogIn size={18} />}
              {busy ? 'Đang xác thực…' : 'Đăng nhập'}
            </button>
          </form>
          <div className="login-preview-note">
            <KeyRound size={15} />
            <span>
              {authMode === 'oidc'
                ? 'netCI đang xác thực bằng OIDC: dùng access token từ identity provider của tập đoàn.'
                : 'netCI đang xác thực bằng token: xem docs/security-model.md để biết cách cấp và thu hồi token.'}
            </span>
          </div>
        </>}

        <small className="login-copyright">© 2026 Release Portal · netCI Platform</small>
      </div>
    </section>
  </main>
}

