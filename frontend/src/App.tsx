import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, WifiOff } from 'lucide-react'
import { createModule, getPortalDashboard, setAuthToken, setUnauthenticatedHandler, type Runtime } from './api/netciClient'
import { DashboardPage, ServersPage, SystemPage, SystemsPage } from './GeneralPages'
import { ArchitectureRoadmapPage } from './ArchitectureRoadmapPage'
import { CatalogPage } from './CatalogPage'
import { CiCostPage } from './CiCostPage'
import { ErrorBoundary } from './AsyncState'
import { LoginPage, type AuthSession } from './LoginPage'
import { endOidcSession } from './auth/oidc'
import { ModulePage } from './ModulePage'
import { ModuleSettings } from './ModuleSettings'
import { NewModuleWizard } from './NewModuleWizard'
import { PortalFeedbackProvider } from './PortalFeedback'
import { Modal, PortalShell, type Navigate } from './PortalShell'
import { ProductionRequestsPage } from './ProductionRequestsPage'
import { ReleaseCalendarPage } from './ReleaseCalendarPage'
import { ScorecardsPage } from './ScorecardsPage'
import { ReleasePlanPage } from './ReleasePlanPage'
import { StageCatalogPage } from './StageCatalogPage'
import { ToolchainPage } from './ToolchainPage'
import { PipelinesPage } from './PipelinesPage'
import type { PageId } from './portalTypes'
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
  if (parts[0] === 'requests' || parts[0] === 'production-requests') return { page: 'requests', systemId: '', moduleId: '', settingsOpen: false }
  if (parts[0] === 'catalog') return { page: 'catalog', systemId: '', moduleId: '', settingsOpen: false }
  if (parts[0] === 'calendar') return { page: 'calendar', systemId: '', moduleId: '', settingsOpen: false }
  if (parts[0] === 'scorecards') return { page: 'scorecards', systemId: '', moduleId: '', settingsOpen: false }
  if (parts[0] === 'release-plan') return { page: 'release-plan', systemId: '', moduleId: '', settingsOpen: false }
  if (parts[0] === 'stage-catalog') return { page: 'stage-catalog', systemId: '', moduleId: '', settingsOpen: false }
  if (parts[0] === 'ci-cost') return { page: 'ci-cost', systemId: '', moduleId: '', settingsOpen: false }
  if (parts[0] === 'servers') return { page: 'servers', systemId: '', moduleId: '', settingsOpen: false }
  if (parts[0] === 'toolchain') return { page: 'toolchain', systemId: '', moduleId: '', settingsOpen: false }
  if (parts[0] === 'architecture') return { page: 'architecture', systemId: '', moduleId: '', settingsOpen: false }
  if (parts[0] === 'pipelines' && parts[1]) return { page: 'pipelines', systemId: '', moduleId: parts[1], settingsOpen: false }
  if (parts[0] === 'pipelines') return { page: 'pipelines', systemId: '', moduleId: '', settingsOpen: false }
  return { page: 'dashboard', systemId: '', moduleId: '', settingsOpen: false }
}

function routePath(route: RouteState): string {
  if (route.page === 'systems') return '/systems'
  if (route.page === 'catalog') return '/catalog'
  if (route.page === 'calendar') return '/calendar'
  if (route.page === 'scorecards') return '/scorecards'
  if (route.page === 'release-plan') return '/release-plan'
  if (route.page === 'stage-catalog') return '/stage-catalog'
  if (route.page === 'ci-cost') return '/ci-cost'
  if (route.page === 'servers') return '/servers'
  if (route.page === 'toolchain') return '/toolchain'
  if (route.page === 'architecture') return '/architecture'
  if (route.page === 'pipelines') return route.moduleId ? `/pipelines/${route.moduleId}` : '/pipelines'
  if (route.page === 'system') return route.systemId ? `/systems/${route.systemId}` : '/systems'
  if (route.page === 'requests') return route.systemId ? `/systems/${route.systemId}/requests` : '/requests'
  if (route.page === 'new-module') return route.systemId ? `/systems/${route.systemId}/new-module` : '/systems'
  if (route.page === 'module') return (route.systemId && route.moduleId) ? `/systems/${route.systemId}/modules/${route.moduleId}${route.settingsOpen ? '/settings' : ''}` : '/systems'
  return '/dashboard'
}

function PortalApp({ session, onLogout }: { session: AuthSession; onLogout: () => void }) {
  const [route, setRoute] = useState<RouteState>(readRoute)
  const [apiState, setApiState] = useState<'checking' | 'online' | 'offline'>('checking')
  const [platformSettingsOpen, setPlatformSettingsOpen] = useState(false)

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

  // Counts navigations: a page with views of its own (Pipelines: list, designer, detail)
  // is keyed on it, so choosing it in the sidebar again returns to its first view.
  const [visit, setVisit] = useState(0)
  const moveTo = (next: RouteState) => {
    window.history.pushState(null, '', `#${routePath(next)}`)
    setRoute(next)
    setVisit((n) => n + 1)
    window.scrollTo({ top: 0, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })
  }

  const navigate: Navigate = (nextPage, options) => {
    moveTo({ page: nextPage, systemId: options?.systemId ?? route.systemId, moduleId: options?.moduleId ?? route.moduleId, settingsOpen: false })
  }

  const handleSettings = () => {
    if (route.page === 'module' && route.moduleId && route.systemId) {
      moveTo({ page: 'module', systemId: route.systemId, moduleId: route.moduleId, settingsOpen: true })
    } else {
      setPlatformSettingsOpen(true)
    }
  }

  const { page, systemId, moduleId, settingsOpen } = route
  return <PortalShell page={page} systemId={systemId} moduleId={moduleId} session={session} navigate={navigate} onLogout={onLogout} onSettings={handleSettings}>
    {apiState === 'offline' && <div className="connection-banner" role="status"><WifiOff size={16} /><span><strong>Backend not connected.</strong> The portal does not display fallback data; restore the API to continue.</span><button onClick={checkApi}><RefreshCw size={15} />Retry</button></div>}
    {platformSettingsOpen && (
      <Modal
        title="Platform Settings & Runtime Overview"
        description="Architecture connection info, deployment infrastructure, and security posture of netCI Delivery Platform."
        onClose={() => setPlatformSettingsOpen(false)}
        footer={<button className="primary-button" onClick={() => setPlatformSettingsOpen(false)}>Close</button>}
      >
        <div className="form-grid" style={{ gap: '12px' }}>
          <label className="field full">
            <span>Backend API Core</span>
            <input readOnly value="http://127.0.0.1:8100 (Status: Online · FastAPI Delivery Engine)" />
          </label>
          <label className="field">
            <span>Database Cluster</span>
            <input readOnly value="PostgreSQL 16 HA Pool (netci-lab-postgres:55432)" />
          </label>
          <label className="field">
            <span>Kubernetes Ingress</span>
            <input readOnly value="KinD Cluster NodePort 30080" />
          </label>
          <label className="field">
            <span>Outbound Runner Gateway</span>
            <input readOnly value="WebSocket wss:// (/api/v1/agents/ws)" />
          </label>
          <label className="field">
            <span>Governance & Policy Gate</span>
            <input readOnly value="Trivy VEX Waiver & L7 Canary Steering Active" />
          </label>
          <label className="field full">
            <span>Active Principal</span>
            <input readOnly value={`${session.identity.principal.displayName} (${session.identity.principal.subject}) [Roles: ${session.identity.principal.roles.join(', ') || 'none'}]`} />
          </label>
        </div>
      </Modal>
    )}
    {settingsOpen && moduleId ? <ModuleSettings systemId={systemId} moduleId={moduleId} onClose={() => moveTo({ ...route, settingsOpen: false })} onDeleted={() => navigate('system', { systemId })} /> : <>
      {page === 'dashboard' && <DashboardPage navigate={navigate} />}
      {page === 'systems' && <SystemsPage navigate={navigate} />}
      {page === 'catalog' && <CatalogPage session={session} navigate={navigate} />}
      {page === 'servers' && <ServersPage />}
      {page === 'calendar' && <ReleaseCalendarPage />}
      {page === 'scorecards' && <ScorecardsPage navigate={navigate} />}
      {page === 'release-plan' && <ReleasePlanPage />}
      {page === 'stage-catalog' && <StageCatalogPage session={session} />}
      {page === 'pipelines' && <PipelinesPage key={visit} moduleId={moduleId} navigate={navigate} />}
      {page === 'ci-cost' && <CiCostPage />}
      {page === 'toolchain' && <ToolchainPage />}
      {page === 'architecture' && <ArchitectureRoadmapPage />}
      {page === 'system' && <SystemPage systemId={systemId} navigate={navigate} />}
      {page === 'requests' && <ProductionRequestsPage systemId={systemId} />}
      {page === 'module' && <ModulePage moduleId={moduleId} onSettings={() => moveTo({ ...route, settingsOpen: true })} />}
      {page === 'new-module' && <NewModuleWizard systemId={systemId} ownerTeams={session.identity.principal.teams} canOwnAnyTeam={session.identity.principal.roles.includes('platform-admin')} onCancel={() => navigate('system', { systemId })} onManagePipelines={() => navigate('pipelines')} onCreate={async (selected, configuration) => {
        await createModule(systemId, {
          name: selected.id,
          displayName: configuration.displayName,
          repositoryUrl: selected.repositoryUrl ?? '',
          pipelineTemplate: pipelineTemplateForRuntime[configuration.runtime],
          runtime: configuration.runtime,
          moduleType: configuration.moduleType,
          description: configuration.description,
          defaultEnvironment: configuration.defaultEnvironment,
          deploymentEnvironments: configuration.deploymentEnvironments,
          stages: configuration.stages,
          pipelineConfig: configuration.pipelineConfig,
          ownerTeam: configuration.ownerTeam,
          pipeline: configuration.pipeline,
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
  // `user`: the person chose to sign out -- end the provider's session as well, or the
  // next click on "sign in" on this browser is them again, without a password.
  // `unauthenticated`: the API stopped accepting the token (expired, revoked); the tab
  // forgets it and the login page decides what comes next.
  const logout = useCallback((reason: 'user' | 'unauthenticated' = 'user') => {
    const current = session
    window.sessionStorage.removeItem(AUTH_SESSION_KEY)
    window.sessionStorage.setItem('netci.manual_login', 'true')
    setAuthToken(null)
    setSession(null)
    window.location.hash = '#/login'
    if (reason === 'user' && current?.endSessionEndpoint && current.token) {
      endOidcSession(current.endSessionEndpoint, current.token)
    }
  }, [session])

  // Any request answered with 401 -- a revoked or expired token -- returns the whole
  // shell to the login screen, instead of leaving a signed-out user looking at a page
  // where every panel has failed for its own apparent reason.
  useEffect(() => {
    setUnauthenticatedHandler(() => logout('unauthenticated'))
    return () => setUnauthenticatedHandler(null)
  }, [logout])
  // The boundary is outside the session split on purpose: a render error in the login
  // screen would otherwise blank the page with no way back, which is the one place a
  // user has no navigation to fall back on.
  return <ErrorBoundary>
    <PortalFeedbackProvider>{session ? <PortalApp session={session} onLogout={() => logout('user')} /> : <LoginPage onLogin={login} />}</PortalFeedbackProvider>
  </ErrorBoundary>
}

export default App
