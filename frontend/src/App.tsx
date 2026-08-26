import { useEffect, useMemo, useState } from 'react'
import { getStageCatalog, type StageCatalog } from './api/netciClient'
import './styles.css'

type Page = 'dashboard' | 'systems' | 'requests' | 'system' | 'module'
type ModuleTab = 'overview' | 'pipeline' | 'version' | 'dora'

type IconName = 'grid' | 'layers' | 'server' | 'bell' | 'chevron' | 'plus' | 'search' | 'arrow' | 'box' | 'globe' | 'settings' | 'play' | 'history' | 'check' | 'alert' | 'clock' | 'activity' | 'shield' | 'menu'

const iconGlyph: Record<IconName, string> = {
  grid: '▦', layers: '▤', server: '▥', bell: '♧', chevron: '⌄', plus: '+', search: '⌕', arrow: '→', box: '▱', globe: '◎', settings: '⚙', play: '▶', history: '↺', check: '✓', alert: '△', clock: '◷', activity: '∿', shield: '◈', menu: '☰',
}

function Icon({ name, size = 'md' }: { name: IconName; size?: 'sm' | 'md' | 'lg' }) {
  return <span className={`icon icon-${size}`} aria-hidden="true">{iconGlyph[name]}</span>
}

const systems = [
  { id: 'netChat', unit: 'Trung tâm nền tảng Công nghệ và Chuyển đổi số', description: 'Real-time messaging platform for internal team communication.', modules: 2, status: 'healthy' as const, accent: 'green', runs: 15, success: 73, failure: 27 },
  { id: 'PCTT', unit: 'Trung tâm Chăm sóc khách hàng', description: 'Ticketing & customer-support case tracking module.', modules: 2, status: 'degraded' as const, accent: 'amber', runs: 0, success: 0, failure: 0 },
  { id: 'NocPro5', unit: 'Trung tâm Vận hành khai thác mạng', description: 'Network operations alarm monitoring & correlation.', modules: 1, status: 'critical' as const, accent: 'red', runs: 0, success: 0, failure: 0 },
]

const modules = [
  { id: 'Backend API', type: 'Backend', typeClass: 'purple', description: 'Node.js REST & WebSocket API for auth, messaging, presence.', versions: ['v2.4.1', 'v2.4.0', 'v2.3.8'], health: 'healthy' },
  { id: 'Web Client', type: 'Frontend', typeClass: 'blue', description: 'React SPA for desktop & mobile web messaging.', versions: ['v1.9.2', 'v1.9.1'], health: 'healthy' },
]

const pipelineRows = [
  { name: 'CI Pipeline', number: '#241', sha: 'a1c4e2f', actor: 'TrungTT', time: '28/04/2025 09:14', duration: '3m 42s', status: 'success' },
  { name: 'CD - Dev', number: '#118', sha: 'a1c4e2f', actor: 'TrungTT', time: '28/04/2025 09:20', duration: '52s', status: 'success' },
  { name: 'CD - Staging', number: '#64', sha: 'a1c4e2f', actor: 'TrungTT', time: '28/04/2025 09:25', duration: '1m 40s', status: 'success' },
  { name: 'CD - Production', number: '#37', sha: '5c88a10', actor: 'HaiNM', time: '27/04/2025 18:02', duration: '3m 05s', status: 'success' },
  { name: 'Automation Test', number: '#12', sha: 'a1c4e2f', actor: 'netAT', time: '28/04/2025 09:40', duration: '4m 10s', status: 'success' },
]

const doraCards = [
  { label: 'Deployment Frequency', value: '8.2', unit: '/wk', hint: 'Releases per week', icon: 'activity' as IconName, tone: 'purple' },
  { label: 'Lead Time for Changes', value: '4.5', unit: 'h', hint: 'Commit to production', icon: 'clock' as IconName, tone: 'blue' },
  { label: 'Change Fail Rate', value: '3.1', unit: '%', hint: 'Deploys causing incidents', icon: 'alert' as IconName, tone: 'red' },
  { label: 'Failed Deployment Recovery', value: '1.2', unit: 'h', hint: 'Mean recovery time', icon: 'shield' as IconName, tone: 'green' },
  { label: 'Deployment Rework Rate', value: '6.4', unit: '%', hint: 'Unplanned rework', icon: 'history' as IconName, tone: 'amber' },
]

function StatusPill({ status }: { status: string }) {
  return <span className={`status-pill status-${status}`}><span className="status-dot" />{status}</span>
}

function TopBar({ breadcrumb, title, onSearch }: { breadcrumb: string; title: string; onSearch: (value: string) => void }) {
  return (
    <div className="topbar">
      <div className="breadcrumb"><span>{breadcrumb}</span><strong>{title}</strong></div>
      <label className="search-box"><Icon name="search" size="sm" /><input aria-label="Search" placeholder="Search…" onChange={(event) => onSearch(event.target.value)} /></label>
    </div>
  )
}

function Sidebar({ page, onNavigate, activeModule }: { page: Page; onNavigate: (page: Page, module?: string) => void; activeModule: string }) {
  return (
    <aside className="sidebar">
      <div className="brand" onClick={() => onNavigate('dashboard')} role="button" tabIndex={0}>
        <div className="brand-mark">R</div><div><strong>Release Portal</strong><small>netCI Platform</small></div><Icon name="chevron" size="sm" />
      </div>
      <nav className="nav">
        <div className="nav-label">General</div>
        <button className={page === 'dashboard' ? 'nav-item active' : 'nav-item'} onClick={() => onNavigate('dashboard')}><Icon name="grid" />Dashboard</button>
        <button className={page === 'systems' || page === 'system' || page === 'module' ? 'nav-item active' : 'nav-item'} onClick={() => onNavigate('systems')}><Icon name="layers" />Systems</button>
        <button className={page === 'requests' ? 'nav-item active' : 'nav-item'} onClick={() => onNavigate('requests')}><Icon name="server" />Servers</button>
        <div className="nav-label nav-label-spaced">Systems</div>
        <button className={page === 'system' || page === 'module' ? 'nav-item system-item active-sub' : 'nav-item system-item'} onClick={() => onNavigate('system', 'netChat')}><span className="system-dot dot-green" />netChat</button>
        <button className="nav-item system-item" onClick={() => onNavigate('system', 'PCTT')}><span className="system-dot dot-amber" />PCTT</button>
        <button className="nav-item system-item" onClick={() => onNavigate('system', 'NocPro5')}><span className="system-dot dot-red" />NocPro5</button>
      </nav>
      <div className="sidebar-footer"><div className="avatar">AD</div><div><strong>Admin</strong><small>admin@netchat.io</small></div><Icon name="chevron" size="sm" /></div>
    </aside>
  )
}

function KpiCard({ label, value, sub, icon, tone }: { label: string; value: string; sub: string; icon: IconName; tone: string }) {
  return <div className="kpi-card"><div className={`kpi-icon tone-${tone}`}><Icon name={icon} /></div><div><span>{label}</span><strong>{value}</strong><small>{sub}</small></div></div>
}

function Dashboard({ onNavigate }: { onNavigate: (page: Page, module?: string) => void }) {
  return <>
    <TopBar breadcrumb="" title="Dashboard" onSearch={() => undefined} />
    <div className="page-heading"><div><h1>Dashboard</h1><p>Monitor system activity and release health across the platform.</p></div></div>
    <div className="kpi-grid dashboard-kpis">
      <KpiCard label="Tổng số hệ thống" value="3" sub="5 module" icon="layers" tone="pink" />
      <KpiCard label="Mức độ hoạt động" value="15" sub="Lượt chạy pipeline" icon="activity" tone="blue" />
      <KpiCard label="Tỉ lệ thành công" value="73%" sub="11 lượt thành công" icon="check" tone="green" />
      <KpiCard label="Tỉ lệ thất bại" value="27%" sub="4 lượt thất bại" icon="alert" tone="red" />
    </div>
    <section className="panel chart-panel"><div className="panel-title"><div><h2>Hoạt động pipeline · 7 ngày gần nhất</h2><p>Release activity across all systems</p></div><span className="legend"><i className="legend-dot success" />Thành công <i className="legend-dot failure" />Thất bại</span></div><div className="bar-chart"><div className="y-axis"><span>12</span><span>8</span><span>4</span><span>0</span></div><div className="bars">{[1, 2, 3, 4, 5, 6, 7].map((day, index) => <div className="bar-group" key={day}><div className="bar-stack"><span className={`bar-success h-${[22, 0, 33, 33, 48, 70, 42][index]}`} /><span className={`bar-failure f-${[0, 20, 0, 16, 0, 0, 0][index]}`} /></div><small>{['20/04', '22/04', '24/04', '25/04', '26/04', '27/04', '28/04'][index]}</small></div>)}</div></div></section>
    <section className="panel system-activity"><div className="panel-title"><div><h2>Mức độ hoạt động theo hệ thống</h2><p>Pipeline activity by system and current health</p></div><button className="text-button" onClick={() => onNavigate('systems')}>Xem tất cả <Icon name="arrow" size="sm" /></button></div><div className="activity-table"><div className="activity-head"><span>Hệ thống</span><span>Hoạt động</span><span>Thành công</span><span>Thất bại</span><span>Modules</span><span /></div>{systems.map((system) => <button className="activity-row" key={system.id} onClick={() => onNavigate('system', system.id)}><span className="system-name"><i className={`system-dot dot-${system.accent}`} />{system.id}</span><span className="activity-number"><Icon name="activity" size="sm" />{system.runs ? `${system.runs} lượt chạy` : '0 lượt chạy'}</span><span className="progress-cell"><i style={{ width: `${system.success}%` }} /><em>{system.success ? `${system.success}%` : '—'}</em></span><span className="progress-cell failure-progress"><i style={{ width: `${system.failure}%` }} /><em>{system.failure ? `${system.failure}%` : '—'}</em></span><span className="module-count"><Icon name="box" size="sm" />{system.modules} modules</span><Icon name="arrow" size="sm" /></button>)}</div></section>
  </>
}

function SystemsPage({ onNavigate, filter }: { onNavigate: (page: Page, module?: string) => void; filter: string }) {
  const visible = systems.filter((item) => `${item.id} ${item.unit} ${item.description}`.toLowerCase().includes(filter.toLowerCase()))
  return <><TopBar breadcrumb="" title="All Systems" onSearch={() => undefined} /><div className="page-heading page-heading-row"><div><h1>Systems</h1><p>Quản lý danh sách hệ thống trong Release Portal.</p></div><button className="primary-button" onClick={() => onNavigate('system', 'new')}><Icon name="plus" />New System</button></div><section className="panel systems-panel"><div className="systems-table"><div className="systems-head"><span>Hệ thống</span><span>Đơn vị</span><span>Mô tả</span><span>Modules</span><span>Trạng thái</span><span /></div>{visible.map((system) => <button className="systems-row" key={system.id} onClick={() => onNavigate('system', system.id)}><span className="system-name"><i className={`system-dot dot-${system.accent}`} />{system.id}</span><span className="truncate">{system.unit}</span><span className="truncate">{system.description}</span><span className="module-count"><Icon name="box" size="sm" />{system.modules} modules</span><StatusPill status={system.status} /><Icon name="arrow" size="sm" /></button>)}</div></section></>
}

function DoraCards() { return <div className="dora-grid">{doraCards.map((card) => <div className="dora-card" key={card.label}><div className={`metric-icon tone-${card.tone}`}><Icon name={card.icon} /></div><span>{card.label}</span><strong>{card.value}<small>{card.unit}</small></strong><em>{card.hint}</em></div>)}</div> }

function ModuleCard({ module, onClick }: { module: typeof modules[number]; onClick: () => void }) {
  return <article className="module-card"><div className="module-card-heading"><div className="module-symbol"><Icon name={module.type === 'Frontend' ? 'globe' : 'server'} /></div><div><h3>{module.id}</h3><p>{module.description}</p></div><span className={`type-badge ${module.typeClass}`}>{module.type}</span></div><div className="module-label">Versions</div><div className="version-chips">{module.versions.map((v) => <span key={v}>{v}</span>)}</div><div className="module-label">Environments</div><div className="env-grid">{['Dev', 'Staging', 'Production'].map((env) => <div className="env-box" key={env}><small>{env}</small><StatusPill status="deployed" /></div>)}</div><button className="module-action" onClick={onClick}>View Module <Icon name="arrow" size="sm" /></button></article>
}

function SystemDetail({ systemId, onNavigate }: { systemId: string; onNavigate: (page: Page, module?: string) => void }) {
  const system = systems.find((item) => item.id === systemId) ?? systems[0]
  return <><TopBar breadcrumb={`Systems / ${system.id}`} title="" onSearch={() => undefined} /><div className="detail-heading"><div><div className="title-line"><h1>{system.id}</h1><StatusPill status={system.status === 'healthy' ? 'healthy' : system.status} /></div><p>{system.description}</p><small>Đơn vị: {system.unit}</small></div><button className="primary-button" onClick={() => onNavigate('system', 'new')}><Icon name="plus" />New Module</button></div><DoraCards /><section className="modules-section"><div className="section-heading"><h2>{system.modules} modules</h2><span>Last updated just now</span></div><div className="modules-grid">{system.id === 'netChat' ? modules.map((module) => <ModuleCard key={module.id} module={module} onClick={() => onNavigate('module', module.id)} />) : <div className="empty-module"><Icon name="box" /><strong>No module activity yet</strong><span>Start by adding a module to this system.</span><button className="secondary-button">Add module</button></div>}</div></section></>
}

function ModuleOverview({ onNavigate }: { onNavigate: (page: Page, module?: string) => void }) {
  return <><DoraCards /><div className="module-overview-grid"><section className="panel release-panel"><div className="panel-title"><div><h2>CI — MERGE REQUESTS</h2><p>Latest pipeline checks</p></div><span className="tiny-status success-text">● All checks passing</span></div><div className="release-list"><div><span>↗ MR #239 · <b>main</b></span><StatusPill status="running" /></div><div><span>↗ MR #241 · <b>main</b></span><StatusPill status="success" /></div><div><span>↗ MR #240 · <b>develop</b></span><StatusPill status="failed" /></div></div><div className="subsection-title">CD — DEPLOYMENTS</div><div className="deploy-list"><div><span>Deploy Dev</span><StatusPill status="deployed" /></div><div><span>Deploy Staging</span><StatusPill status="deployed" /></div></div></section><section className="panel recent-panel"><div className="panel-title"><div><h2>Recent Releases</h2><p>3 total</p></div></div>{[['v2.4.1', '28/04/2025 · TrungTT', 'Deployed', 'Passed'], ['v2.4.0', '10/04/2025 · HaiNM', 'Pending', 'Passed'], ['v2.3.8', '01/03/2025 · DungLV', 'Failed', 'Failed']].map((release) => <div className="recent-release" key={release[0]}><span className="release-avatar">{release[0].slice(1, 3)}</span><div><strong>{release[0]}</strong><small>{release[1]}</small></div><div className="release-badges"><span className={release[2] === 'Deployed' ? 'badge-success' : release[2] === 'Pending' ? 'badge-warning' : 'badge-danger'}>{release[2]}</span><span className="badge-test">Test · {release[3]}</span></div></div>)}</section></div><section className="trend-grid"><div className="panel trend-card"><div className="trend-head"><span>Test coverage</span><strong>87%</strong><small>per released version</small><em>↗ +3% vs prev</em></div><div className="mini-chart blue-chart"><i /><i /><i /></div></div><div className="panel trend-card"><div className="trend-head"><span>Automation pass rate</span><strong>100%</strong><small>netAT test cases</small><em>↗ +14% vs prev</em></div><div className="mini-chart green-chart"><i /><i /><i /></div></div><div className="panel trend-card"><div className="trend-head"><span>Security findings</span><strong>10</strong><small>SAST + SCA by severity</small><em className="down">↘ -7 vs prev</em></div><div className="security-chart"><i /><i /><i /><i /></div></div></section><button className="hidden-route-button" onClick={() => onNavigate('module', 'Backend API')}>Module</button></>
}

function PipelineView() {
  return <section className="pipeline-list">{pipelineRows.map((row) => <div className="pipeline-row" key={row.name}><span className="pipeline-status"><Icon name={row.status === 'success' ? 'check' : 'alert'} /></span><div className="pipeline-name"><strong>{row.name}</strong><small>{row.number} · {row.sha} · {row.actor} · {row.time}</small></div><span className="pipeline-result"><b>{row.status === 'success' ? 'Success' : 'Failed'}</b><small>{row.duration}</small></span><button className="icon-button" aria-label={`${row.name} history`}><Icon name="history" /></button><button className="icon-button play-button" aria-label={`Trigger ${row.name}`}><Icon name="play" size="sm" /></button></div>)}</section>
}

function NewModuleModal({ catalog, onClose }: { catalog: StageCatalog | null; onClose: () => void }) {
  const [template, setTemplate] = useState(catalog?.templates[0]?.id ?? 'container-ci-cd-v1')
  const selected = catalog?.templates.find((item) => item.id === template)
  const stageNames = selected?.stageIds ?? ['checkout', 'unit-test', 'build', 'sbom', 'vulnerability-scan', 'sign', 'publish', 'deploy', 'health-check']
  return <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><div className="modal" role="dialog" aria-modal="true" aria-label="Create module"><div className="modal-head"><div><span className="eyebrow">New delivery workflow</span><h2>Create Module</h2><p>Configure a golden path without opening Jenkins.</p></div><button className="icon-button" onClick={onClose} aria-label="Close">×</button></div><label className="form-field"><span>Module name</span><input defaultValue="hello-container" /><small>Lowercase, 3–63 characters.</small></label><label className="form-field"><span>Pipeline template</span><select value={template} onChange={(event) => setTemplate(event.target.value)}>{catalog?.templates.map((item) => <option key={item.id} value={item.id}>{item.name}</option>) ?? <><option>container-ci-cd-v1</option><option>kubernetes-ci-cd-v1</option><option>systemd-ansible-ci-cd-v1</option></>}</select></label><div className="catalog-preview"><div className="catalog-preview-head"><span>Stage catalog</span><b>{stageNames.length} stages</b></div>{stageNames.map((stage, index) => <div className="catalog-stage" key={stage}><span>{index + 1}</span><strong>{stage.replace(/-/g, ' ')}</strong><i>ready</i></div>)}</div><div className="modal-actions"><button className="secondary-button" onClick={onClose}>Cancel</button><button className="primary-button" onClick={onClose}>Create module</button></div></div></div>
}

function ModuleDetail({ tab, setTab, onNavigate }: { tab: ModuleTab; setTab: (tab: ModuleTab) => void; onNavigate: (page: Page, module?: string) => void }) {
  return <><TopBar breadcrumb="Systems / netChat / Backend API" title="" onSearch={() => undefined} /><div className="module-heading"><div><h1>Backend API</h1><p>Node.js REST & WebSocket API for auth, messaging, presence.</p></div><button className="settings-button" aria-label="Module settings"><Icon name="settings" /></button></div><div className="tabs">{([['overview', 'Overview'], ['pipeline', 'Pipeline'], ['version', 'Version'], ['dora', 'DORA Metrics']] as const).map(([value, label]) => <button key={value} className={tab === value ? 'tab active' : 'tab'} onClick={() => setTab(value as ModuleTab)}>{label}</button>)}</div>{tab === 'overview' && <ModuleOverview onNavigate={onNavigate} />}{tab === 'pipeline' && <PipelineView />}{tab === 'version' && <section className="panel version-panel"><div className="panel-title"><div><h2>Version history</h2><p>Release artifacts and deployment status</p></div><button className="primary-button"><Icon name="plus" />New release</button></div>{['v2.4.1', 'v2.4.0', 'v2.3.8'].map((version) => <div className="version-row" key={version}><strong>{version}</strong><code>sha256: a1c4e2f…</code><span>Build artifact signed</span><StatusPill status={version === 'v2.3.8' ? 'failed' : 'deployed'} /></div>)}</section>}{tab === 'dora' && <section className="dora-detail"><DoraCards /><section className="panel dora-table"><div className="panel-title"><div><h2>DORA metric trend</h2><p>Production deployment performance over time</p></div></div><div className="fake-line-chart"><i /><i /><i /><i /><i /></div></section></section>}</>
}

function RequestsPage() { return <><TopBar breadcrumb="" title="Production Requests" onSearch={() => undefined} /><div className="page-heading"><div><h1>Production Requests</h1><p>Review and approve deployments waiting for production promotion.</p></div></div><section className="panel requests-panel">{[['Backend API', 'netChat', 'v2.4.1', 'TrungTT', 'Waiting approval'], ['Web Client', 'netChat', 'v1.9.2', 'HaiNM', 'Approved'], ['Alert Correlator', 'NocPro5', 'v0.8.4', 'MinhNV', 'Blocked by scan']].map((request) => <div className="request-row" key={`${request[0]}-${request[2]}`}><div className="request-icon"><Icon name="shield" /></div><div><strong>{request[0]}</strong><small>{request[1]} · {request[2]} · requested by {request[3]}</small></div><span className={request[4] === 'Approved' ? 'badge-success' : request[4].startsWith('Blocked') ? 'badge-danger' : 'badge-warning'}>{request[4]}</span><button className="secondary-button">Review <Icon name="arrow" size="sm" /></button></div>)}</section></> }

export default function App() {
  const [page, setPage] = useState<Page>('dashboard')
  const [activeId, setActiveId] = useState('netChat')
  const [moduleTab, setModuleTab] = useState<ModuleTab>('overview')
  const [search, setSearch] = useState('')
  const [catalog, setCatalog] = useState<StageCatalog | null>(null)
  const [showModal, setShowModal] = useState(false)

  useEffect(() => { getStageCatalog().then(setCatalog).catch(() => undefined) }, [])

  const navigate = (nextPage: Page, id?: string) => {
    if (id === 'new') { setShowModal(true); return }
    if (id) setActiveId(id)
    if (nextPage === 'module') setModuleTab('overview')
    setPage(nextPage)
  }
  const content = useMemo(() => {
    if (page === 'dashboard') return <Dashboard onNavigate={navigate} />
    if (page === 'systems') return <SystemsPage onNavigate={navigate} filter={search} />
    if (page === 'requests') return <RequestsPage />
    if (page === 'system') return <SystemDetail systemId={activeId} onNavigate={navigate} />
    return <ModuleDetail tab={moduleTab} setTab={setModuleTab} onNavigate={navigate} />
  }, [page, activeId, moduleTab, search])

  return <div className="app-shell"><Sidebar page={page} onNavigate={navigate} activeModule={activeId} /><main className="main"><div className="mobile-top"><Icon name="menu" /><span>Release Portal</span><Icon name="bell" /></div>{content}</main>{showModal && <NewModuleModal catalog={catalog} onClose={() => setShowModal(false)} />}</div>
}
