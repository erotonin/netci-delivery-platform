import { useState, type ReactNode } from 'react'
import {
  Activity, Bell, Box, CheckCircle2, ChevronDown, ChevronRight, Clock3,
  Gauge, Grid2X2, Layers3, LogOut, Menu, Search, Server, Settings,
  ShieldAlert, X,
} from 'lucide-react'
import { dora, systems, type PageId } from './portalData'

export type Navigate = (page: PageId, options?: { systemId?: string; moduleId?: string }) => void

const systemTone: Record<string, string> = { healthy: 'green', degraded: 'amber', critical: 'red' }
const iconMap = { frequency: Activity, lead: Clock3, failure: ShieldAlert, recovery: Gauge }

export function StatusPill({ status }: { status: string }) {
  const key = status.toLowerCase().replace(/ /g, '-').replace('bảo-trì', 'maintenance').replace('pending-checks', 'pending')
  return <span className={`status status-${key}`}><i />{status}</span>
}

export function PageHeader({ title, description, action }: { title: string; description: string; action?: ReactNode }) {
  return <div className="page-heading"><div><h1>{title}</h1><p>{description}</p></div>{action}</div>
}

export function DoraCards() {
  return <div className="dora-grid">{dora.map((metric) => {
    const MetricIcon = iconMap[metric.key as keyof typeof iconMap]
    return <article className="dora-card" key={metric.key}>
      <div className={`metric-icon tone-${metric.tone}`}><MetricIcon size={18} /></div>
      <div className="metric-copy"><span>{metric.label}</span><strong>{metric.value}<small>{metric.unit}</small></strong><p>{metric.hint}</p></div>
      <em className={metric.trend.startsWith('+') && metric.key === 'failure' ? 'bad-trend' : ''}>{metric.trend}</em>
    </article>
  })}</div>
}

export function Modal({ title, description, children, footer, onClose, wide = false }: { title: string; description?: string; children: ReactNode; footer: ReactNode; onClose: () => void; wide?: boolean }) {
  return <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose() }}>
    <section className={`modal ${wide ? 'modal-wide' : ''}`} role="dialog" aria-modal="true" aria-label={title}>
      <header><div><h2>{title}</h2>{description && <p>{description}</p>}</div><button className="icon-button" aria-label="Đóng" onClick={onClose}><X size={18} /></button></header>
      <div className="modal-body">{children}</div>
      <footer>{footer}</footer>
    </section>
  </div>
}

function Sidebar({ page, systemId, moduleId, navigate, open, close }: { page: PageId; systemId: string; moduleId: string; navigate: Navigate; open: boolean; close: () => void }) {
  const inSystem = ['system', 'requests', 'module', 'new-module'].includes(page)
  return <aside className={`sidebar ${open ? 'sidebar-open' : ''}`}>
    <button className="brand" onClick={() => navigate('dashboard')}>
      <span className="brand-mark">R</span><span><strong>Release Portal</strong><small>netCI Platform</small></span><ChevronDown size={15} />
    </button>
    <nav>
      {inSystem ? <>
        <button className="nav-back" onClick={() => navigate('systems')}><ChevronRight size={16} className="rotate-180" /> All Systems</button>
        <div className="nav-context"><span className="system-health health-green" /><strong>{systemId}</strong></div>
        <button className={page === 'system' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('system', { systemId })}><Grid2X2 size={17} />Overview</button>
        <button className={page === 'requests' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('requests', { systemId })}><ShieldAlert size={17} />Production Requests</button>
        <span className="nav-label">Modules</span>
        {systems.find((item) => item.id === systemId)?.modules.map((module) => <button key={module.id} className={page === 'module' && moduleId === module.id ? 'nav-item active' : 'nav-item'} onClick={() => navigate('module', { systemId, moduleId: module.id })}><Box size={17} />{module.name}</button>)}
      </> : <>
        <span className="nav-label">General</span>
        <button className={page === 'dashboard' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('dashboard')}><Grid2X2 size={17} />Dashboard</button>
        <button className={page === 'systems' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('systems')}><Layers3 size={17} />Systems</button>
        <button className={page === 'servers' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('servers')}><Server size={17} />Servers</button>
        <span className="nav-label nav-label-spaced">Systems</span>
        {systems.map((system) => <button key={system.id} className="nav-item system-link" onClick={() => navigate('system', { systemId: system.id })}><i className={`system-health health-${systemTone[system.status]}`} />{system.id}</button>)}
      </>}
    </nav>
    <div className="sidebar-user"><span className="avatar">AD</span><span><strong>Admin</strong><small>admin@netchat.io</small></span><LogOut size={17} /></div>
    <button className="sidebar-close" aria-label="Đóng menu" onClick={close}><X size={20} /></button>
  </aside>
}

function TopBar({ page, systemId, moduleId, onSettings, onMenu }: { page: PageId; systemId: string; moduleId: string; onSettings: () => void; onMenu: () => void }) {
  const [notifications, setNotifications] = useState(false)
  const moduleName = systems.flatMap((system) => system.modules).find((item) => item.id === moduleId)?.name
  const labels: Partial<Record<PageId, string>> = { dashboard: 'Dashboard', systems: 'All Systems', servers: 'Servers', system: 'Overview', requests: 'Production Requests', module: moduleName, 'new-module': 'New Module' }
  const crumbs = ['system', 'requests', 'module', 'new-module'].includes(page) ? ['Systems', systemId, labels[page]] : [labels[page]]
  return <header className="topbar">
    <button className="mobile-menu" aria-label="Mở menu" onClick={onMenu}><Menu size={20} /></button>
    <div className="breadcrumbs">{crumbs.filter(Boolean).map((crumb, index) => <span key={`${crumb}-${index}`}>{index > 0 && <i>/</i>}{crumb}</span>)}</div>
    <label className="global-search"><Search size={16} /><input aria-label="Tìm kiếm toàn cục" placeholder="Search…" /></label>
    <div className="topbar-actions">
      <button className="icon-button notification-button" aria-label="Thông báo" onClick={() => setNotifications(!notifications)}><Bell size={18} /><i /></button>
      <button className="icon-button" aria-label="Cài đặt" onClick={onSettings}><Settings size={18} /></button>
      <span className="avatar">AD</span>
      <button className="logout-button" aria-label="Đăng xuất"><LogOut size={17} /></button>
    </div>
    {notifications && <section className="notification-panel">
      <div className="notification-title"><strong>Notifications</strong><button onClick={() => setNotifications(false)}>Mark all as read</button></div>
      <div className="notification-row unread"><span className="notice-icon success"><CheckCircle2 size={16} /></span><div><strong>Deployment successful</strong><p>Backend API v2.4.1 deployed to Production.</p><small>12 minutes ago</small></div></div>
      <div className="notification-row"><span className="notice-icon warning"><ShieldAlert size={16} /></span><div><strong>Approval required</strong><p>PR-2025-0033 is waiting for GNOC checks.</p><small>42 minutes ago</small></div></div>
      <button className="notification-footer">View all notifications</button>
    </section>}
  </header>
}

export function PortalShell({ children, page, systemId, moduleId, navigate, onSettings }: { children: ReactNode; page: PageId; systemId: string; moduleId: string; navigate: Navigate; onSettings: () => void }) {
  const [menuOpen, setMenuOpen] = useState(false)
  return <div className="portal-shell">
    <Sidebar page={page} systemId={systemId} moduleId={moduleId} navigate={(next, options) => { navigate(next, options); setMenuOpen(false) }} open={menuOpen} close={() => setMenuOpen(false)} />
    {menuOpen && <button className="mobile-overlay" aria-label="Đóng menu" onClick={() => setMenuOpen(false)} />}
    <div className="portal-main"><TopBar page={page} systemId={systemId} moduleId={moduleId} onSettings={onSettings} onMenu={() => setMenuOpen(true)} /><main className="page-content">{children}</main></div>
  </div>
}
