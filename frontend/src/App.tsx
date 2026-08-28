import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, WifiOff } from 'lucide-react'
import { createModule, getPortalDashboard, setAuthToken, setUnauthenticatedHandler, type Runtime } from './api/netciClient'
import { DashboardPage, ServersPage, SystemPage, SystemsPage } from './GeneralPages'
import { LoginPage, type AuthSession } from './LoginPage'
import { ModulePage } from './ModulePage'
import { ModuleSettings } from './ModuleSettings'
import { NewModuleWizard } from './NewModuleWizard'
import { PortalFeedbackProvider } from './PortalFeedback'
import { PortalShell, type Navigate } from './PortalShell'
import { ProductionRequestsPage } from './ProductionRequestsPage'
import { dcimModules, type PageId } from './portalData'
import './styles.css'

const pipelineTemplateForRuntime: Record<Runtime, string> = {
  docker: 'container-ci-cd-v1',
  kubernetes: 'kubernetes-ci-cd-v1',
  systemd: 'systemd-ansible-ci-cd-v1',
}

type RouteState = { page: PageId; systemId: string; moduleId: string; settingsOpen: boolean }
const AUTH_SESSION_KEY = 'netci.auth-session'

function readAuthSession(): AuthSession | null {
  try {
    const value = window.sessionStorage.getItem(AUTH_SESSION_KEY)
    if (!value) return null
    const parsed = JSON.parse(value) as Partial<AuthSession>
    const principal = parsed.identity?.principal
    if (!principal || typeof principal.subject !== 'string' || !Array.isArray(principal.roles)) return null
    // The token is what the API actually checks; the stored identity is only a cached
    // label for the UI. If the token has been revoked since the last page load, the first
    // request fails with 401 and `setUnauthenticatedHandler` ends the session.
    if (parsed.token) setAuthToken(parsed.token)
    return parsed as AuthSession
  } catch {
    return null
  }
}

function readRoute(): RouteState {
  const parts = window.location.hash.replace(/^#\/?/, '').split('/').filter(Boolean)
  if (parts[0] === 'systems' && parts[1] && parts[2] === 'modules' && parts[3]) {
    return { page: 'module', systemId: parts[1], moduleId: parts[3], settingsOpen: parts[4] === 'settings' }
  }
  if (parts[0] === 'systems' && parts[1] && parts[2] === 'requests') return { page: 'requests', systemId: parts[1], moduleId: '', settingsOpen: false }
  if (parts[0] === 'systems' && parts[1] && parts[2] === 'new-module') return { page: 'new-module', systemId: parts[1], moduleId: '', settingsOpen: false }
  if (parts[0] === 'systems' && parts[1]) return { page: 'system', systemId: parts[1], moduleId: '', settingsOpen: false }
  if (parts[0] === 'systems') return { page: 'systems', systemId: '', moduleId: '', settingsOpen: false }
  if (parts[0] === 'servers') return { page: 'servers', systemId: '', moduleId: '', settingsOpen: false }
  return { page: 'dashboard', systemId: '', moduleId: '', settingsOpen: false }
}

function routePath(route: RouteState): string {
  if (route.page === 'systems') return '/systems'
  if (route.page === 'servers') return '/servers'
  if (route.page === 'system') return `/systems/${route.systemId}`
  if (route.page === 'requests') return `/systems/${route.systemId}/requests`
  if (route.page === 'new-module') return `/systems/${route.systemId}/new-module`
  if (route.page === 'module') return `/systems/${route.systemId}/modules/${route.moduleId}${route.settingsOpen ? '/settings' : ''}`
  return '/dashboard'
}

function PortalApp({ session, onLogout }: { session: AuthSession; onLogout: () => void }) {
  const [route, setRoute] = useState<RouteState>(readRoute)
  const [apiState, setApiState] = useState<'checking' | 'online' | 'offline'>('checking')

  const checkApi = useCallback(() => {
    setApiState('checking')
    getPortalDashboard().then(() => setApiState('online')).catch(() => setApiState('offline'))
  }, [])

  useEffect(() => {
    checkApi()
  }, [checkApi])

  useEffect(() => {
    const syncFromHistory = () => setRoute(readRoute())
    if (!window.location.hash) window.history.replaceState(null, '', '#/dashboard')
    window.addEventListener('popstate', syncFromHistory)
    window.addEventListener('hashchange', syncFromHistory)
    return () => {
      window.removeEventListener('popstate', syncFromHistory)
      window.removeEventListener('hashchange', syncFromHistory)
    }
  }, [])

  const moveTo = (next: RouteState) => {
    window.history.pushState(null, '', `#${routePath(next)}`)
    setRoute(next)
    window.scrollTo({ top: 0, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })
  }

  const navigate: Navigate = (nextPage, options) => {
    moveTo({ page: nextPage, systemId: options?.systemId ?? route.systemId, moduleId: options?.moduleId ?? route.moduleId, settingsOpen: false })
  }

  const { page, systemId, moduleId, settingsOpen } = route
  return <PortalShell page={page} systemId={systemId} moduleId={moduleId} session={session} navigate={navigate} onLogout={onLogout} onSettings={() => moveTo({ page: 'module', systemId, moduleId, settingsOpen: true })}>
    {apiState === 'offline' && <div className="connection-banner" role="status"><WifiOff size={16} /><span><strong>Backend chưa kết nối.</strong> Dữ liệu demo vẫn dùng được trong phiên; các thao tác cần tích hợp sẽ hiển thị lỗi rõ ràng.</span><button onClick={checkApi}><RefreshCw size={15} />Thử lại</button></div>}
    {settingsOpen ? <ModuleSettings systemId={systemId} moduleId={moduleId} onClose={() => moveTo({ ...route, settingsOpen: false })} onDeleted={() => navigate('system', { systemId })} /> : <>
      {page === 'dashboard' && <DashboardPage navigate={navigate} />}
      {page === 'systems' && <SystemsPage navigate={navigate} />}
      {page === 'servers' && <ServersPage />}
      {page === 'system' && <SystemPage systemId={systemId} navigate={navigate} />}
      {page === 'requests' && <ProductionRequestsPage systemId={systemId} />}
      {page === 'module' && <ModulePage moduleId={moduleId} onSettings={() => moveTo({ ...route, settingsOpen: true })} />}
      {page === 'new-module' && <NewModuleWizard onCancel={() => navigate('system', { systemId })} onCreate={async (selectedId, configuration) => {
        const selected = dcimModules.find((item) => item.id === selectedId)
        if (!selected) throw new Error('Không tìm thấy module đã chọn trong DCIM.')
        await createModule(systemId, {
          name: selected.id,
          displayName: configuration.displayName,
          repositoryUrl: selected.repo,
          pipelineTemplate: pipelineTemplateForRuntime[configuration.runtime],
          runtime: configuration.runtime,
          moduleType: configuration.moduleType,
          description: configuration.description,
          defaultEnvironment: configuration.defaultEnvironment,
          deploymentEnvironments: configuration.deploymentEnvironments,
          stages: configuration.stages,
          pipelineConfig: configuration.pipelineConfig,
        })
        navigate('module', { systemId, moduleId: selected.id })
      }} />}
    </>}
  </PortalShell>
}

function App() {
  const [session, setSession] = useState<AuthSession | null>(readAuthSession)
  const login = useCallback((nextSession: AuthSession) => {
    window.sessionStorage.setItem(AUTH_SESSION_KEY, JSON.stringify(nextSession))
    setAuthToken(nextSession.token)
    setSession(nextSession)
    if (!window.location.hash || window.location.hash === '#/login') window.history.replaceState(null, '', '#/dashboard')
  }, [])
  const logout = useCallback(() => {
    window.sessionStorage.removeItem(AUTH_SESSION_KEY)
    setAuthToken(null)
    setSession(null)
    window.history.replaceState(null, '', '#/login')
  }, [])

  // Any request answered with 401 -- a revoked or expired token -- returns the whole
  // shell to the login screen, instead of leaving a signed-out user looking at a page
  // where every panel has failed for its own apparent reason.
  useEffect(() => {
    setUnauthenticatedHandler(logout)
    return () => setUnauthenticatedHandler(null)
  }, [logout])
  return <PortalFeedbackProvider>{session ? <PortalApp session={session} onLogout={logout} /> : <LoginPage onLogin={login} />}</PortalFeedbackProvider>
}

export default App
