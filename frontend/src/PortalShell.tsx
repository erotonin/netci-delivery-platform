import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import {
  CalendarDays,
  Activity, Bell, BookOpen, Box, CheckCircle2, ChevronDown, ChevronRight, ClipboardCheck, Clock3,
  Compass, Gauge, Grid2X2, Layers3, LogOut, Menu, Search, Server, Settings,
  ShieldAlert, X,
} from 'lucide-react'
import { listSystems } from './api/netciClient'
import type { AuthSession } from './LoginPage'
import type { DoraCardMetric, PageId } from './portalTypes'

export type Navigate = (page: PageId, options?: { systemId?: string; moduleId?: string }) => void

const systemTone: Record<string, string> = { unknown: 'gray', healthy: 'green', degraded: 'amber', critical: 'red' }
const iconMap = { frequency: Activity, lead: Clock3, failure: ShieldAlert, recovery: Gauge }

export function StatusPill({ status }: { status: string }) {
  const key = status.toLowerCase().replace(/ /g, '-').replace('bảo-trì', 'maintenance').replace('pending-checks', 'pending')
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
      <header><div><h2 id={titleId}>{title}</h2>{description && <p id={descriptionId}>{description}</p>}</div><button type="button" className="icon-button" aria-label="Đóng" onClick={onClose}><X size={18} /></button></header>
      <div className="modal-body">{children}</div>
      <footer>{footer}</footer>
    </section>
  </div>
}

type NavigationModule = { id: string; name: string }
type NavigationSystem = { id: string; status: string; modules: NavigationModule[] }

const initialNavigationSystems: NavigationSystem[] = []

function Sidebar({ page, systemId, moduleId, moduleLinks, navigationSystems, session, navigate, onLogout, open, close }: { page: PageId; systemId: string; moduleId: string; moduleLinks: NavigationModule[]; navigationSystems: NavigationSystem[]; session: AuthSession; navigate: Navigate; onLogout: () => void; open: boolean; close: () => void }) {
  const inSystem = ['system', 'requests', 'module', 'new-module'].includes(page)
  const currentSystemTone = systemTone[navigationSystems.find((system) => system.id === systemId)?.status ?? 'unknown'] ?? 'gray'
  return <aside className={`sidebar ${open ? 'sidebar-open' : ''}`}>
    <button className="brand" onClick={() => navigate('dashboard')}>
      <span className="brand-mark">R</span><span><strong>Release Portal</strong><small>netCI Platform</small></span><ChevronDown size={15} />
    </button>
    <nav>
      {inSystem ? <>
        <button className="nav-back" onClick={() => navigate('systems')}><ChevronRight size={16} className="rotate-180" /> All Systems</button>
        <div className="nav-context"><span className={`system-health health-${currentSystemTone}`} /><strong>{systemId}</strong></div>
        <button aria-current={page === 'system' ? 'page' : undefined} className={page === 'system' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('system', { systemId })}><Grid2X2 size={17} />Overview</button>
        <button aria-current={page === 'requests' ? 'page' : undefined} className={page === 'requests' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('requests', { systemId })}><ShieldAlert size={17} />Production Requests</button>
        <span className="nav-label">Modules</span>
        {moduleLinks.map((module) => <button key={module.id} aria-current={page === 'module' && moduleId === module.id ? 'page' : undefined} className={page === 'module' && moduleId === module.id ? 'nav-item active' : 'nav-item'} onClick={() => navigate('module', { systemId, moduleId: module.id })}><Box size={17} />{module.name}</button>)}
      </> : <>
        <span className="nav-label">General</span>
        <button aria-current={page === 'dashboard' ? 'page' : undefined} className={page === 'dashboard' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('dashboard')}><Grid2X2 size={17} />Dashboard</button>
        <button aria-current={page === 'systems' ? 'page' : undefined} className={page === 'systems' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('systems')}><Layers3 size={17} />Systems</button>
        <button aria-current={page === 'catalog' ? 'page' : undefined} className={page === 'catalog' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('catalog')}><Compass size={17} />Service Catalog</button>
        <button aria-current={page === 'servers' ? 'page' : undefined} className={page === 'servers' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('servers')}><Server size={17} />Servers</button>
        <button aria-current={page === 'requests' ? 'page' : undefined} className={page === 'requests' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('requests')}><ShieldAlert size={17} />Production Requests</button>
        <button aria-current={page === 'calendar' ? 'page' : undefined} className={page === 'calendar' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('calendar')}><CalendarDays size={17} />Release Calendar</button>
        <button aria-current={page === 'vulnerabilities' ? 'page' : undefined} className={page === 'vulnerabilities' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('vulnerabilities')}><ShieldAlert size={17} />Vulnerabilities</button>
        <button aria-current={page === 'scorecards' ? 'page' : undefined} className={page === 'scorecards' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('scorecards')}><ClipboardCheck size={17} />Scorecards</button>
        <span className="nav-label nav-label-spaced">Systems</span>
        {navigationSystems.map((system) => <button key={system.id} className="nav-item system-link" onClick={() => navigate('system', { systemId: system.id })}><i className={`system-health health-${systemTone[system.status] ?? 'gray'}`} />{system.id}</button>)}
      </>}
    </nav>
    {session.token === null ? (
      <div className="sidebar-user">
        <span className="avatar">NA</span>
        <span>
          <strong>{session.identity.principal.displayName}</strong>
          <small>Chưa bật xác thực</small>
        </span>
      </div>
    ) : (
      <button
        className="sidebar-user"
        onClick={onLogout}
        title="Bấm để Đăng xuất / Chuyển tài khoản (admin ↔ dev)"
        style={{ cursor: 'pointer', textAlign: 'left', width: '100%', border: 'none', background: 'transparent' }}
      >
        <span className="avatar">{session.identity.principal.displayName.slice(0, 2).toUpperCase()}</span>
        <span>
          <strong>{session.identity.principal.displayName}</strong>
          <small>{roleLabel(session)}</small>
        </span>
        <LogOut size={17} style={{ marginLeft: 'auto', opacity: 0.8 }} />
      </button>
    )}
    <button className="sidebar-close" aria-label="Đóng menu" onClick={close}><X size={20} /></button>
  </aside>
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
  const moduleName = moduleLinks.find((item) => item.id === moduleId)?.name ?? navigationSystems.flatMap((system) => system.modules).find((item) => item.id === moduleId)?.name
  const labels: Partial<Record<PageId, string>> = { dashboard: 'Dashboard', systems: 'All Systems', servers: 'Servers', catalog: 'Service Catalog', calendar: 'Release Calendar', vulnerabilities: 'Vulnerabilities', scorecards: 'Scorecards', architecture: 'Architecture & IDP 2026 Roadmap', system: 'Overview', requests: 'Production Requests', module: moduleName, 'new-module': 'New Module' }
  const crumbs = ['system', 'requests', 'module', 'new-module'].includes(page) ? ['Systems', systemId, labels[page]] : [labels[page]]
  const searchItems = [
    { key: 'dashboard', label: 'Dashboard', detail: 'General', action: () => navigate('dashboard') },
    { key: 'systems', label: 'Systems', detail: 'General', action: () => navigate('systems') },
    { key: 'catalog', label: 'Service Catalog', detail: 'Catalog & Golden Paths', action: () => navigate('catalog') },
    { key: 'servers', label: 'Servers', detail: 'Infrastructure', action: () => navigate('servers') },
    { key: 'calendar', label: 'Release Calendar', detail: 'Planning', action: () => navigate('calendar') },
    { key: 'vulnerabilities', label: 'Vulnerabilities', detail: 'Security', action: () => navigate('vulnerabilities') },
    { key: 'scorecards', label: 'Scorecards', detail: 'Quality', action: () => navigate('scorecards') },
    ...navigationSystems.flatMap((system) => [
      { key: `system-${system.id}`, label: system.id, detail: 'System', action: () => navigate('system', { systemId: system.id }) },
      ...system.modules.map((module) => ({ key: `module-${system.id}-${module.id}`, label: module.name, detail: `${system.id} · Module`, action: () => navigate('module', { systemId: system.id, moduleId: module.id }) })),
    ]),
  ]
  const results = query.trim() ? searchItems.filter((item) => `${item.label} ${item.detail}`.toLowerCase().includes(query.trim().toLowerCase())).slice(0, 7) : searchItems.slice(0, 5)
  const selectResult = (action: () => void) => { action(); setQuery(''); setSearchOpen(false) }
  return <header className="topbar">
    <button className="mobile-menu" aria-label="Mở menu" onClick={onMenu}><Menu size={20} /></button>
    <div className="breadcrumbs">{crumbs.filter(Boolean).map((crumb, index) => <span key={`${crumb}-${index}`}>{index > 0 && <i>/</i>}{crumb}</span>)}</div>
    <div className="global-search-wrap"><label className="global-search"><Search size={16} /><input aria-label="Tìm kiếm toàn cục" placeholder="Search systems, modules…" value={query} onFocus={() => setSearchOpen(true)} onBlur={() => setTimeout(() => setSearchOpen(false), 200)} onChange={(event) => { setQuery(event.target.value); setSearchOpen(true) }} onKeyDown={(event) => { if (event.key === 'Escape') setSearchOpen(false); if (event.key === 'Enter' && results[0]) selectResult(results[0].action) }} /></label>{searchOpen && <section className="global-search-results" aria-label="Search results">{results.map((item) => <button key={item.key} onMouseDown={(event) => event.preventDefault()} onClick={() => selectResult(item.action)}><Search size={14} /><span><strong>{item.label}</strong><small>{item.detail}</small></span></button>)}{!results.length && <div><strong>No matching destination</strong><small>Try a system or module name.</small></div>}</section>}</div>
    <div className="topbar-actions">
      <button className="icon-button notification-button" aria-label="Thông báo" aria-expanded={notifications} onClick={() => setNotifications(!notifications)}><Bell size={18} /></button>
      <button className="icon-button" aria-label="Cài đặt" onClick={onSettings}><Settings size={18} /></button>
      {session.token === null ? (
        <span className="auth-pill auth-pill-none" style={{ fontSize: '0.8rem', padding: '4px 10px', background: 'rgba(255,255,255,0.06)', borderRadius: '12px', color: 'var(--text-muted)' }}>
          Chưa bật xác thực
        </span>
      ) : (
        <button
          type="button"
          onClick={onLogout}
          className="icon-button"
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            background: 'rgba(255,255,255,0.06)',
            border: '1px solid rgba(255,255,255,0.12)',
            borderRadius: '20px',
            padding: '4px 12px',
            cursor: 'pointer',
            color: 'var(--text-main)',
            fontSize: '0.85rem'
          }}
          title={`Đang đăng nhập: ${session.identity.principal.displayName} (${roleLabel(session)}). Bấm để Đăng xuất / Đổi tài khoản.`}
        >
          <span className="avatar" style={{ width: '22px', height: '22px', fontSize: '0.75rem' }}>
            {session.identity.principal.displayName.slice(0, 2).toUpperCase()}
          </span>
          <span style={{ fontWeight: 500 }}>{session.identity.principal.displayName.split(' ')[0]}</span>
          <LogOut size={14} style={{ color: 'var(--text-muted)' }} />
        </button>
      )}
    </div>
    {notifications && <section className="notification-panel">
      <div className="notification-title"><strong>Notifications</strong></div>
      <div className="empty-notifications" style={{ padding: '1.5rem', textAlign: 'center', color: 'var(--text-muted)' }}><small>No notifications. Real events appear when an outbox event or notification service is configured.</small></div>
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
    {menuOpen && <button className="mobile-overlay" aria-label="Đóng menu" onClick={() => setMenuOpen(false)} />}
    <div className="portal-main"><TopBar page={page} systemId={systemId} moduleId={moduleId} moduleLinks={moduleLinks} navigationSystems={navigationSystems} session={session} navigate={navigate} onSettings={onSettings} onLogout={onLogout} onMenu={() => setMenuOpen(true)} /><main className="page-content">{children}</main></div>
  </div>
}
