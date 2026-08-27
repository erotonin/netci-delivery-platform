import { useState, type FormEvent } from 'react'
import { ArrowLeft, Eye, EyeOff, KeyRound, LogIn, ShieldCheck } from 'lucide-react'

export type AuthSession = {
  displayName: string
  email: string
  method: 'local-sso' | 'local-password'
}

export function LoginPage({ onLogin }: { onLogin: (session: AuthSession) => void }) {
  const [passwordMode, setPasswordMode] = useState(false)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState('')

  const loginWithPassword = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!username.trim() || !password) {
      setError('Nhập đầy đủ tên đăng nhập và mật khẩu để tiếp tục.')
      return
    }
    onLogin({
      displayName: username.trim(),
      email: `${username.trim().toLowerCase().replace(/\s+/g, '.')}@netchat.io`,
      method: 'local-password',
    })
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
        <p>Vui lòng chọn hình thức đăng nhập.</p>
        {!passwordMode ? <div className="login-options">
          <button className="login-primary" onClick={() => onLogin({ displayName: 'Admin', email: 'admin@netchat.io', method: 'local-sso' })}><ShieldCheck size={18} />Đăng nhập bằng SSO</button>
          <button className="login-secondary" onClick={() => { setPasswordMode(true); setError('') }}><KeyRound size={18} />Đăng nhập bằng mật khẩu</button>
        </div> : <form className="login-form" onSubmit={loginWithPassword} noValidate>
          <div className="login-field"><label htmlFor="login-username">Tên đăng nhập</label><input id="login-username" autoFocus autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} placeholder="ten.dang.nhap" /></div>
          <div className="login-field"><label htmlFor="login-password">Mật khẩu</label><div className="password-field"><input id="login-password" autoComplete="current-password" type={showPassword ? 'text' : 'password'} value={password} onChange={(event) => setPassword(event.target.value)} placeholder="••••••••" /><button type="button" aria-label={showPassword ? 'Ẩn mật khẩu' : 'Hiện mật khẩu'} onClick={() => setShowPassword((current) => !current)}>{showPassword ? <EyeOff size={17} /> : <Eye size={17} />}</button></div></div>
          {error && <div className="login-error" role="alert">{error}</div>}
          <button className="login-primary" type="submit"><LogIn size={18} />Đăng nhập</button>
          <button className="login-back" type="button" onClick={() => { setPasswordMode(false); setError(''); setPassword('') }}><ArrowLeft size={16} />Quay lại đăng nhập SSO</button>
        </form>}
        <div className="login-preview-note"><ShieldCheck size={15} /><span>Windows preview dùng phiên đăng nhập local; SSO/OIDC thật được cấu hình ở runtime Ubuntu.</span></div>
        <small className="login-copyright">© 2026 Release Portal · netCI Platform</small>
      </div>
    </section>
  </main>
}
