import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import {
  CalendarDays,
  Activity, Bell, Box, CheckCircle2, ChevronDown, ChevronRight, Clock3,
  Compass, Gauge, GitBranch, Grid2X2, Layers3, LogOut, Menu, Search, Server, Settings,
  Moon, ShieldAlert, Sun, Wrench, X,
} from 'lucide-react'
import { listSystems } from './api/netciClient'
import type { AuthSession } from './LoginPage'
import type { DoraCardMetric, PageId } from './portalTypes'
import { useTheme } from './theme'

export type Navigate = (page: PageId, options?: { systemId?: string; moduleId?: string }) => void

const systemTone: Record<string, string> = { unknown: 'gray', healthy: 'green', degraded: 'amber', critical: 'red' }
const iconMap = { frequency: Activity, lead: Clock3, failure: ShieldAlert, recovery: Gauge }

export function StatusPill({ status }: { status: string }) {
  const key = status.toLowerCase().replace(/ /g, '-').replace('pending-checks', 'pending')
  return <span className={`status status-${key}`}><i />{status}</span>
}

export function PageHeader({ title, description, action }: { title: string; description: string; action?: ReactNode }) {
  return <div className="page-heading"><div><h1>{title}</h1><p>{description}</p></div>{action}</div>
}

export function DoraCards({ metrics = [] }: { metrics?: DoraCardMetric[] }) {
  return <div className="dora-grid">{metrics.map((metric) => {
    const MetricIcon = iconMap[metric.key as keyof typeof iconMap] ?? Gauge
    return <article className="dora-card" key={metric.key}>
      <div className={`metric-icon tone-${metric.tone}`}><MetricIcon size={18} /></div>
      <div className="metric-copy"><span>{metric.label}</span><strong>{metric.value}<small>{metric.unit}</small></strong><p>{metric.hint}</p></div>
      {metric.trend && <em className={metric.trend.startsWith('+') && metric.key === 'failure' ? 'bad-trend' : ''}>{metric.trend}</em>}
    </article>
  })}</div>
}

export function Modal({ title, description, children, footer, onClose, wide = false }: { title: string; description?: string; children: ReactNode; footer: ReactNode; onClose: () => void; wide?: boolean }) {
  const modalRef = useRef<HTMLElement>(null)
  const onCloseRef = useRef(onClose)
  const titleId = useId()
  const descriptionId = useId()
  onCloseRef.current = onClose

  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const modal = modalRef.current
    const focusable = () => Array.from(modal?.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])') ?? [])
    focusable()[0]?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onCloseRef.current()
        return
      }
      if (event.key !== 'Tab') return
      const items = focusable()
      if (!items.length) return
      const first = items[0]
      const last = items[items.length - 1]
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.body.style.overflow = previousOverflow
      previousFocus?.focus()
    }
  }, [])

  return <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose() }}>
    <section ref={modalRef} className={`modal ${wide ? 'modal-wide' : ''}`} role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={description ? descriptionId : undefined}>
      <header><div><h2 id={titleId}>{title}</h2>{description && <p id={descriptionId}>{description}</p>}</div><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X size={18} /></button></header>
      <div className="modal-body">{children}</div>
      <footer>{footer}</footer>
    </section>
  </div>
}

type NavigationModule = { id: string; name: string }
type NavigationSystem = { id: string; status: string; modules: NavigationModule[] }

const initialNavigationSystems: NavigationSystem[] = []

type NavEntry = { page: PageId; label: string; icon: typeof Grid2X2 }

/** The Portal's map: what a team does every day first, planning and platform after. */
const NAV_GROUPS: { label: string; items: NavEntry[] }[] = [
  { label: 'Delivery', items: [
    { page: 'dashboard', label: 'Dashboard', icon: Grid2X2 },
    { page: 'systems', label: 'Systems', icon: Layers3 },
    { page: 'pipelines', label: 'Pipelines', icon: GitBranch },
    { page: 'requests', label: 'Production Requests', icon: ShieldAlert },
  ] },
  // Release Plan, Scorecards, Stage Catalog and CI Cost are reached from search: the
  // navigation was streamlined on purpose (fca849d) and stays that way.
  { label: 'Plan & Govern', items: [
    { page: 'calendar', label: 'Release Calendar', icon: CalendarDays },
    { page: 'catalog', label: 'Service Catalog', icon: Compass },
  ] },
  { label: 'Platform', items: [
    { page: 'servers', label: 'Servers', icon: Server },
    { page: 'toolchain', label: 'Toolchain', icon: Wrench },
  ] },
]

export function BrandMark({ size = 32 }: { size?: number }) {
  // A pipeline: three stages joined, the last one lit -- netCI's mark.
  return <svg className="brand-mark" width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
    <rect width="32" height="32" rx="8" fill="var(--accent)" />
    <path d="M8 20.5V13a3 3 0 0 1 6 0v6a3 3 0 0 0 6 0v-7.5" fill="none" stroke="#fff" strokeWidth="2.4" strokeLinecap="round" />
    <circle cx="20" cy="9.5" r="2.6" fill="#fff" />
  </svg>
}

function Sidebar({ page, systemId, moduleId, moduleLinks, navigationSystems, session, navigate, onLogout, open, close }: { page: PageId; systemId: string; moduleId: string; moduleLinks: NavigationModule[]; navigationSystems: NavigationSystem[]; session: AuthSession; navigate: Navigate; onLogout: () => void; open: boolean; close: () => void }) {
  const inSystem = Boolean(systemId) && ['system', 'requests', 'module', 'new-module'].includes(page)
  const currentSystemTone = systemTone[navigationSystems.find((system) => system.id === systemId)?.status ?? 'unknown'] ?? 'gray'
  const item = (active: boolean, label: string, Icon: typeof Grid2X2, onClick: () => void, key?: string) =>
    <button key={key ?? label} aria-current={active ? 'page' : undefined} className={active ? 'nav-item active' : 'nav-item'} onClick={onClick}><Icon size={17} /><span>{label}</span></button>
  return <aside className={`sidebar ${open ? 'sidebar-open' : ''}`}>
    <button className="brand" onClick={() => navigate('dashboard')} aria-label="netCI home">
      <BrandMark /><span><strong>netCI</strong><small>Delivery Platform</small></span>
    </button>
    <nav aria-label="Main">
      {inSystem ? <>
        <button className="nav-back" onClick={() => navigate('systems')}><ChevronRight size={16} className="rotate-180" /> All Systems</button>
        <div className="nav-context"><span className={`system-health health-${currentSystemTone}`} /><strong>{systemId}</strong></div>
        {item(page === 'system', 'Overview', Grid2X2, () => navigate('system', { systemId }))}
        {item(page === 'requests', 'Production Requests', ShieldAlert, () => navigate('requests', { systemId }))}
        <span className="nav-label nav-label-spaced">Modules</span>
        {moduleLinks.map((module) => item(page === 'module' && moduleId === module.id, module.name, Box, () => navigate('module', { systemId, moduleId: module.id }), module.id))}
      </> : <>
        {NAV_GROUPS.map((group, index) => <div className="nav-group" key={group.label}>
          <span className={index ? 'nav-label nav-label-spaced' : 'nav-label'}>{group.label}</span>
          {group.items.map((entry) => item(page === entry.page, entry.label, entry.icon, () => navigate(entry.page)))}
        </div>)}
        {navigationSystems.length > 0 && <div className="nav-group">
          <span className="nav-label nav-label-spaced">Systems</span>
          {navigationSystems.map((system) => <button key={system.id} className="nav-item system-link" onClick={() => navigate('system', { systemId: system.id })}><i className={`system-health health-${systemTone[system.status] ?? 'gray'}`} /><span>{system.id}</span></button>)}
        </div>}
      </>}
    </nav>
    {session.token === null ? (
      <div className="sidebar-user">
        <span className="avatar">NA</span>
        <span>
          <strong>{session.identity.principal.displayName}</strong>
          <small>Authentication disabled</small>
        </span>
      </div>
    ) : (
      <button className="sidebar-user sidebar-user-button" onClick={onLogout} title="Sign out or switch account">
        <span className="avatar">{session.identity.principal.displayName.slice(0, 2).toUpperCase()}</span>
        <span>
          <strong>{session.identity.principal.displayName}<AgentBadge session={session} /></strong>
          <small>{roleLabel(session)}</small>
        </span>
        <LogOut size={16} />
      </button>
    )}
    <button className="sidebar-close" aria-label="Close menu" onClick={close}><X size={20} /></button>
  </aside>
}

/**
 * Marks a coding agent (ADR-052). The server decides the kind and refuses an agent what it
 * may not do; this only says who is signed in, so it shows for 'agent' and nothing else --
 * a missing kind is not guessed either way.
 */
export function AgentBadge({ session }: { session: AuthSession }) {
  if (session.identity.principal.kind !== 'agent') return null
  return <span className="agent-badge" data-testid="agent-badge" title="Signed in as a coding agent (ADR-052)">agent</span>
}

/** The caller's netCI roles, which is what actually decides what they can do. */
function roleLabel(session: AuthSession): string {
  const roles = session.identity.principal.roles
  return roles.length ? roles.join(' · ') : 'no roles'
}

function TopBar({ page, systemId, moduleId, moduleLinks, navigationSystems, session, navigate, onSettings, onLogout, onMenu }: { page: PageId; systemId: string; moduleId: string; moduleLinks: NavigationModule[]; navigationSystems: NavigationSystem[]; session: AuthSession; navigate: Navigate; onSettings: () => void; onLogout: () => void; onMenu: () => void }) {
  const [notifications, setNotifications] = useState(false)
  const [notificationsRead, setNotificationsRead] = useState(false)
  const [query, setQuery] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  const searchRef = useRef<HTMLInputElement>(null)
  const theme = useTheme()
  useEffect(() => {
    // "/" or Ctrl/Cmd+K jumps to search, as in most developer tools.
    const onKey = (event: KeyboardEvent) => {
      const typing = event.target instanceof HTMLElement && ['INPUT', 'TEXTAREA', 'SELECT'].includes(event.target.tagName)
      if ((event.key === 'k' && (event.metaKey || event.ctrlKey)) || (event.key === '/' && !typing)) {
        event.preventDefault()
        searchRef.current?.focus()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])
  const moduleName = moduleLinks.find((item) => item.id === moduleId)?.name ?? navigationSystems.flatMap((system) => system.modules).find((item) => item.id === moduleId)?.name
  const labels: Partial<Record<PageId, string>> = { dashboard: 'Dashboard', systems: 'All Systems', servers: 'Servers', catalog: 'Service Catalog', calendar: 'Release Calendar', scorecards: 'Scorecards', 'release-plan': 'Release Plan', 'stage-catalog': 'Stage Catalog', 'ci-cost': 'CI Cost', architecture: 'Architecture', system: 'Overview', requests: 'Production Requests', module: moduleName, 'new-module': 'New Module', toolchain: 'Toolchain', pipelines: 'Pipelines' }
  const crumbs = ['system', 'requests', 'module', 'new-module'].includes(page) ? ['Systems', systemId, labels[page]] : (page === 'pipelines' && moduleId ? ['Pipelines', moduleId] : [labels[page]])
  const searchItems = [
    { key: 'dashboard', label: 'Dashboard', detail: 'General', action: () => navigate('dashboard') },
    { key: 'systems', label: 'Systems', detail: 'General', action: () => navigate('systems') },
    { key: 'pipelines', label: 'Pipelines', detail: 'Pipelines & Designer', action: () => navigate('pipelines') },
    { key: 'catalog', label: 'Service Catalog', detail: 'Catalog & Golden Paths', action: () => navigate('catalog') },
    { key: 'servers', label: 'Servers', detail: 'Infrastructure', action: () => navigate('servers') },
    { key: 'toolchain', label: 'Toolchain', detail: 'Platform & Governance', action: () => navigate('toolchain') },
    { key: 'calendar', label: 'Release Calendar', detail: 'Planning', action: () => navigate('calendar') },
    { key: 'scorecards', label: 'Scorecards', detail: 'Quality', action: () => navigate('scorecards') },
    { key: 'release-plan', label: 'Release Plan', detail: 'Planning', action: () => navigate('release-plan') },
    { key: 'stage-catalog', label: 'Stage Catalog', detail: 'Pipeline stages', action: () => navigate('stage-catalog') },
    { key: 'ci-cost', label: 'CI Cost', detail: 'FinOps', action: () => navigate('ci-cost') },
    ...navigationSystems.flatMap((system) => [
      { key: `system-${system.id}`, label: system.id, detail: 'System', action: () => navigate('system', { systemId: system.id }) },
      ...system.modules.map((module) => ({ key: `module-${system.id}-${module.id}`, label: module.name, detail: `${system.id} · Module`, action: () => navigate('module', { systemId: system.id, moduleId: module.id }) })),
    ]),
  ]
  const results = query.trim() ? searchItems.filter((item) => `${item.label} ${item.detail}`.toLowerCase().includes(query.trim().toLowerCase())).slice(0, 7) : searchItems.slice(0, 5)
  const selectResult = (action: () => void) => { action(); setQuery(''); setSearchOpen(false) }
  return <header className="topbar">
    <button className="mobile-menu" aria-label="Open menu" onClick={onMenu}><Menu size={20} /></button>
    <div className="breadcrumbs">{crumbs.filter(Boolean).map((crumb, index) => <span key={`${crumb}-${index}`}>{index > 0 && <i>/</i>}{crumb}</span>)}</div>
    <div className="global-search-wrap"><label className="global-search"><Search size={16} /><input ref={searchRef} aria-label="Global search" placeholder="Search systems, modules, pages…" value={query} onFocus={() => setSearchOpen(true)} onBlur={() => setTimeout(() => setSearchOpen(false), 200)} onChange={(event) => { setQuery(event.target.value); setSearchOpen(true) }} onKeyDown={(event) => { if (event.key === 'Escape') setSearchOpen(false); if (event.key === 'Enter' && results[0]) selectResult(results[0].action) }} /></label>{searchOpen && <section className="global-search-results" aria-label="Search results">{results.map((item) => <button key={item.key} onMouseDown={(event) => event.preventDefault()} onClick={() => selectResult(item.action)}><Search size={14} /><span><strong>{item.label}</strong><small>{item.detail}</small></span></button>)}{!results.length && <div><strong>No matching destination</strong><small>Try a system or module name.</small></div>}</section>}</div>
    <div className="topbar-actions">
      <button className="icon-button" aria-label={theme.resolved === 'dark' ? 'Use the light theme' : 'Use the dark theme'} title={theme.resolved === 'dark' ? 'Light theme' : 'Dark theme'} onClick={() => theme.setChoice(theme.resolved === 'dark' ? 'light' : 'dark')}>{theme.resolved === 'dark' ? <Sun size={18} /> : <Moon size={18} />}</button>
      <button className="icon-button notification-button" aria-label="Notifications" aria-expanded={notifications} onClick={() => setNotifications(!notifications)}><Bell size={18} /></button>
      <button className="icon-button" aria-label="Settings" onClick={onSettings}><Settings size={18} /></button>
      {session.token === null ? (
        <span className="auth-pill auth-pill-none">Authentication disabled</span>
      ) : (
        <button type="button" onClick={onLogout} className="user-chip"
          title={`Signed in as: ${session.identity.principal.displayName} (${roleLabel(session)}). Click to sign out / switch account.`}>
          <span className="avatar avatar-sm">{session.identity.principal.displayName.slice(0, 2).toUpperCase()}</span>
          <span>{session.identity.principal.displayName.split(' ')[0]}</span>
          <AgentBadge session={session} />
          <LogOut size={14} />
        </button>
      )}
    </div>
    {notifications && <section className="notification-panel">
      <div className="notification-title"><strong>Notifications</strong></div>
      <div className="empty-notifications"><small>No notifications. Real events appear when an outbox event or notification service is configured.</small></div>
      <button className="notification-footer" onClick={() => { setNotifications(false); navigate('requests', { systemId }) }}>View approval requests</button>
    </section>}
  </header>
}

export function PortalShell({ children, page, systemId, moduleId, session, navigate, onSettings, onLogout }: { children: ReactNode; page: PageId; systemId: string; moduleId: string; session: AuthSession; navigate: Navigate; onSettings: () => void; onLogout: () => void }) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [moduleLinks, setModuleLinks] = useState<NavigationModule[]>([])
  const [navigationSystems, setNavigationSystems] = useState<NavigationSystem[]>(initialNavigationSystems)
  useEffect(() => {
    let active = true
    listSystems().then((items) => {
      if (!active) return
      const next = items.map((system) => ({ id: system.id, status: system.status, modules: system.modules.map((module) => ({ id: module.id, name: module.name })) }))
      setNavigationSystems(next)
      const current = next.find((system) => system.id === systemId)
      if (current) setModuleLinks(current.modules)
    }).catch(() => undefined)
    return () => { active = false }
  }, [page, systemId, moduleId])
  return <div className="portal-shell">
    <Sidebar page={page} systemId={systemId} moduleId={moduleId} moduleLinks={moduleLinks} navigationSystems={navigationSystems} session={session} navigate={(next, options) => { navigate(next, options); setMenuOpen(false) }} onLogout={onLogout} open={menuOpen} close={() => setMenuOpen(false)} />
    {menuOpen && <button className="mobile-overlay" aria-label="Close menu" onClick={() => setMenuOpen(false)} />}
    <div className="portal-main"><TopBar page={page} systemId={systemId} moduleId={moduleId} moduleLinks={moduleLinks} navigationSystems={navigationSystems} session={session} navigate={navigate} onSettings={onSettings} onLogout={onLogout} onMenu={() => setMenuOpen(true)} /><main className="page-content">{children}</main></div>
  </div>
}
