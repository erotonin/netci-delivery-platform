import { useEffect, useState, type FormEvent } from 'react'
import { KeyRound, Loader2, LogIn, ShieldAlert, ShieldCheck, UserCheck, UserCog } from 'lucide-react'
import { NetciApiError, whoami, type Identity } from './api/netciClient'
import { BrandMark } from './PortalShell'
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
    // How to sign in is the server's to say: the mode, and for OIDC the public client
    // and the provider's endpoints. The Portal bundle carries none of it. /me is asked
    // only in open mode, where it answers; asking it first put a 401 in the console on
    // every visit to the login page for nothing.
    fetchAuthConfig()
      .then((config) => {
        if (cancelled) return
        setOidc(config.oidc)
        if (config.authMode === 'none' && !manualMode) {
          return whoami().then((identity) => {
            if (cancelled) return
            setAuthMode(identity.authMode)
            window.sessionStorage.removeItem('netci.manual_login')
            onLogin({ token: null, identity })
          })
        }
        setAuthMode(config.authMode === 'token' || config.authMode === 'oidc' || config.authMode === 'none' ? config.authMode : 'auth-required')
      })
      .catch((cause) => {
        if (cancelled) return
        if (cause instanceof NetciApiError && cause.code === 'AUTH_NOT_CONFIGURED') setAuthMode('auth-required')
        else if (cause instanceof NetciApiError && cause.status === 401) setAuthMode('token')
        else setAuthMode('unreachable')
      })
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
        if (cause instanceof NetciApiError && cause.status === 403) setError('SSO login succeeded, but the account has not been assigned any roles in netCI.')
        else setError(`SSO login failed: ${cause instanceof Error ? cause.message : String(cause)}`)
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
        setError('Invalid credentials or token has been revoked.')
      } else if (cause instanceof NetciApiError && cause.status === 403) {
        setError('Account is valid, but has not been assigned any roles in netCI.')
      } else {
        setError('Cannot connect to netCI API. Check service status and try again.')
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
      setError('Please enter a username (admin or dev).')
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
      setError('Incorrect username or password. Please use admin/admin or dev/dev.')
    }
  }

  const signInToken = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!token.trim()) {
      setError('Enter an access token issued by the platform team.')
      return
    }
    await loginWithPersona(token.trim())
  }

  return <main className="login-page">
    <section className="login-story" aria-labelledby="login-story-title">
      <div className="login-brand"><BrandMark size={36} /><strong>netCI</strong></div>
      <div className="login-story-copy">
        <span className="login-eyebrow"><ShieldCheck size={15} />Internal Developer Platform</span>
        <h1 id="login-story-title">Every release, <em>from commit to production.</em></h1>
        <p>Build once, sign and scan the artifact, then promote that same digest through dev, staging and production -- with the evidence and approvals each step needs.</p>
      </div>
      <small>netCI · build, verify, promote</small>
    </section>
    <section className="login-panel">
      <div className="login-card">
        <div className="login-mark"><BrandMark size={40} /></div>
        <h2>Sign in to netCI</h2>

        {authMode === null && <p className="login-checking" role="status"><Loader2 size={16} className="spin" />Checking authentication configuration…</p>}

        {authMode === 'unreachable' && <div className="login-error" role="alert">
          <ShieldAlert size={16} />netCI API is unreachable. The portal cannot authenticate without calling <code>/me</code>.
        </div>}

        {authMode === 'auth-required' && <div className="login-error" role="alert">
          <ShieldAlert size={16} />API is running but rejected unauthenticated proxy mode. Configure <code>NETCI_AUTH_MODE=token</code> or <code>oidc</code> for this topology.
        </div>}

        {authMode === 'none' && !manualMode && (
          <p className="login-checking" role="status"><Loader2 size={16} className="spin" />netCI is running in demo mode — loading Portal…</p>
        )}

        {(authMode === 'none' && manualMode) && (
          <>
            <p style={{ color: 'var(--text-muted)', fontSize: '0.9rem', marginBottom: '16px' }}>
              Select a demo persona to test <strong>Separation of Duties</strong>:
            </p>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', marginBottom: '20px' }}>
              <button
                type="button"
                className="login-primary"
                onClick={() => loginWithPersona('admin')}
                disabled={busy}
                style={{
                  background: 'linear-gradient(135deg, var(--info), var(--info))',
                  borderColor: 'var(--info)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'flex-start',
                  padding: '12px 16px',
                  gap: '12px',
                  textAlign: 'left'
                }}
              >
                <UserCog size={24} style={{ color: 'var(--info-text)' }} />
                <div>
                  <div style={{ fontWeight: 600, fontSize: '0.95rem' }}>1. Persona: admin / admin</div>
                  <div style={{ fontSize: '0.8rem', opacity: 0.85 }}>Alexander Admin (Platform Lead & Reviewer - Approves Releases)</div>
                </div>
              </button>

              <button
                type="button"
                className="login-primary"
                onClick={() => loginWithPersona('dev')}
                disabled={busy}
                style={{
                  background: 'linear-gradient(135deg, var(--success), var(--success))',
                  borderColor: 'var(--success)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'flex-start',
                  padding: '12px 16px',
                  gap: '12px',
                  textAlign: 'left'
                }}
              >
                <UserCheck size={24} style={{ color: 'var(--success-text)' }} />
                <div>
                  <div style={{ fontWeight: 600, fontSize: '0.95rem' }}>2. Persona: dev / dev</div>
                  <div style={{ fontSize: '0.8rem', opacity: 0.85 }}>David Developer (Creates Modules, CI Builds, Submits Releases)</div>
                </div>
              </button>
            </div>

            <div style={{ position: 'relative', textAlign: 'center', margin: '16px 0' }}>
              <hr style={{ borderColor: 'rgba(255,255,255,0.1)' }} />
              <span style={{ position: 'absolute', top: '-10px', left: '50%', transform: 'translateX(-50%)', background: 'var(--nav-surface)', padding: '0 8px', fontSize: '0.75rem', color: 'var(--text-3)' }}>OR MANUAL SIGN IN</span>
            </div>

            <form className="login-form" onSubmit={handleCredentialsSubmit} noValidate>
              <div className="login-field">
                <label htmlFor="login-username">Username or email</label>
                <input
                  id="login-username"
                  type="text"
                  autoComplete="username"
                  spellCheck={false}
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="Username or email (e.g. admin, dev)"
                />
              </div>

              <div className="login-field">
                <label htmlFor="login-password">Password</label>
                <input
                  id="login-password"
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="Password"
                />
              </div>

              {error && <div className="login-error" role="alert">{error}</div>}

              <button className="login-primary" type="submit" disabled={busy} style={{ backgroundColor: 'var(--accent)', borderColor: 'var(--accent)' }}>
                {busy ? <Loader2 size={18} className="spin" /> : <LogIn size={18} />}
                {busy ? 'Authenticating…' : 'Sign In'}
              </button>
            </form>
          </>
        )}

        {authMode === 'oidc' && oidc && !oidc.error && (
          <div className="login-sso">
            <button className="login-primary" type="button" onClick={signInWithProvider} disabled={busy} data-testid="sso-login" style={{ backgroundColor: 'var(--accent)', borderColor: 'var(--accent)', fontWeight: 700 }}>
              {busy ? <Loader2 size={18} className="spin" /> : <LogIn size={18} />}
              {busy ? 'Authenticating…' : 'Sign in with SSO (Keycloak)'}
            </button>
            <small>Redirects to <code>{oidc.issuer}</code> to authenticate; netCI validates the returned token and assigns roles.</small>
          </div>
        )}
        {authMode === 'oidc' && oidc?.error && <div className="login-error" role="alert">
          <ShieldAlert size={16} />Identity provider <code>{oidc.issuer}</code> discovery failed; paste token below.
        </div>}

        {(authMode === 'token' || authMode === 'oidc') && <>
          <p>{authMode === 'oidc' ? 'Or paste a token from identity provider.' : 'Paste an access token issued by the platform team.'} Your permissions are determined by netCI, not the Portal.</p>
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
              {busy ? 'Authenticating…' : 'Sign In'}
            </button>
          </form>
          <div className="login-preview-note">
            <KeyRound size={15} />
            <span>
              {authMode === 'oidc'
                ? 'netCI authenticates via OIDC: use an access token from enterprise identity provider.'
                : 'netCI authenticates via token: see docs/security-model.md on how tokens are issued and revoked.'}
            </span>
          </div>
        </>}

        <small className="login-copyright">© 2026 Release Portal · netCI Platform</small>
      </div>
    </section>
  </main>
}

