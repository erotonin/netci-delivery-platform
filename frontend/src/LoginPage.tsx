import { useEffect, useState, type FormEvent } from 'react'
import { KeyRound, Loader2, LogIn, ShieldAlert, ShieldCheck } from 'lucide-react'
import { NetciApiError, whoami, type Identity } from './api/netciClient'

/** The signed-in user, as the *server* described them. The browser never decides this. */
export type AuthSession = {
  token: string | null
  identity: Identity
}

export const displayNameOf = (session: AuthSession) => session.identity.principal.displayName
export const hasRole = (session: AuthSession, ...roles: string[]) =>
  session.identity.principal.roles.some((role) => roles.includes(role))

export function LoginPage({ onLogin }: { onLogin: (session: AuthSession) => void }) {
  const [token, setToken] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  // `null` while we are still asking the API how it is configured.
  const [authMode, setAuthMode] = useState<string | null>(null)

  // Ask the server what it requires before drawing a form. When netCI runs with
  // NETCI_AUTH_MODE=none there is no credential to collect, and presenting a login box
  // that accepts anything is exactly the theatre this screen used to be.
  useEffect(() => {
    let cancelled = false
    whoami()
      .then((identity) => {
        if (cancelled) return
        setAuthMode(identity.authMode)
        if (identity.authMode === 'none') onLogin({ token: null, identity })
      })
      .catch((cause) => {
        if (cancelled) return
        if (cause instanceof NetciApiError && cause.status === 401) setAuthMode('token')
        else if (cause instanceof NetciApiError && cause.code === 'AUTH_NOT_CONFIGURED') setAuthMode('auth-required')
        else setAuthMode('unreachable')
      })
    return () => {
      cancelled = true
    }
  }, [onLogin])

  const signIn = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!token.trim()) {
      setError('Nhập access token do platform team cấp.')
      return
    }
    setBusy(true)
    setError('')
    try {
      // The token is verified by the server before it becomes a session: the Portal
      // never stores a credential it has not seen work.
      const identity = await whoami(token.trim())
      onLogin({ token: token.trim(), identity })
    } catch (cause) {
      if (cause instanceof NetciApiError && cause.status === 401) {
        setError('Token không hợp lệ hoặc đã bị thu hồi.')
      } else if (cause instanceof NetciApiError && cause.status === 403) {
        setError('Token hợp lệ nhưng tài khoản chưa được cấp quyền nào trên netCI.')
      } else {
        setError('Không kết nối được tới netCI API. Kiểm tra dịch vụ và thử lại.')
      }
    } finally {
      setBusy(false)
    }
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
        <h2>Đăng nhập</h2>

        {authMode === null && <p className="login-checking" role="status"><Loader2 size={16} className="spin" />Đang kiểm tra cấu hình xác thực…</p>}

        {authMode === 'unreachable' && <div className="login-error" role="alert">
          <ShieldAlert size={16} />netCI API không phản hồi. Portal không thể đăng nhập khi chưa gọi được <code>/me</code>.
        </div>}

        {authMode === 'auth-required' && <div className="login-error" role="alert">
          <ShieldAlert size={16} />API đang chạy nhưng từ chối chế độ không xác thực qua proxy. Hãy cấu hình <code>NETCI_AUTH_MODE=token</code> hoặc <code>oidc</code> cho topology này.
        </div>}

        {authMode === 'none' && <p className="login-checking" role="status"><Loader2 size={16} className="spin" />netCI đang chạy không bật xác thực — đang vào Portal…</p>}

        {(authMode === 'token' || authMode === 'oidc') && <>
          <p>Dán access token do platform team cấp. Quyền của bạn do netCI quyết định, không do Portal.</p>
          <form className="login-form" onSubmit={signIn} noValidate>
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
