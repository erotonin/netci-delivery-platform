import { useMemo, useState } from 'react'
import {
  Activity, ArrowRight, Box, CheckCircle2, ChevronLeft, ChevronRight, CircleAlert,
  CloudDownload, Layers3, MoreHorizontal, Pencil, Plus, Search, Server, Trash2,
} from 'lucide-react'
import { DoraCards, Modal, PageHeader, StatusPill, type Navigate } from './PortalShell'
import { activity, servers as seedServers, systems, type PortalServer } from './portalData'

export function DashboardPage({ navigate }: { navigate: Navigate }) {
  const kpis = [
    { label: 'Tổng số hệ thống', value: '3', sub: '5 module', icon: Layers3, tone: 'pink' },
    { label: 'Mức độ hoạt động', value: '15', sub: 'Lượt chạy pipeline', icon: Activity, tone: 'blue' },
    { label: 'Tỉ lệ thành công', value: '73%', sub: '11 lượt thành công', icon: CheckCircle2, tone: 'green' },
    { label: 'Tỉ lệ thất bại', value: '27%', sub: '4 lượt thất bại', icon: CircleAlert, tone: 'red' },
  ]
  return <>
    <PageHeader title="Dashboard" description="Monitor system activity and release health across the platform." />
    <div className="kpi-grid">{kpis.map((item) => <article className="kpi-card" key={item.label}><span className={`kpi-icon tone-${item.tone}`}><item.icon size={19} /></span><div><p>{item.label}</p><strong>{item.value}</strong><small>{item.sub}</small></div></article>)}</div>
    <section className="panel chart-panel">
      <div className="panel-heading"><div><h2>Hoạt động pipeline · 7 ngày gần nhất</h2><p>Release activity across all systems</p></div><div className="chart-legend"><span><i className="legend-success" />Thành công</span><span><i className="legend-failed" />Thất bại</span></div></div>
      <div className="bar-chart"><div className="chart-y"><span>12</span><span>8</span><span>4</span><span>0</span></div><div className="chart-grid-lines"><i /><i /><i /><i /></div><div className="bar-groups">{activity.map((item) => <div className="bar-group" key={item.day}><div className="bar-stack"><i className="bar-success" style={{ height: `${item.success * 11}px` }} /><i className="bar-failed" style={{ height: `${item.failed * 16}px` }} /></div><span>{item.day}</span></div>)}</div></div>
    </section>
    <section className="panel activity-panel">
      <div className="panel-heading"><div><h2>Mức độ hoạt động theo hệ thống</h2><p>Pipeline activity by system and current health</p></div><button className="text-button" onClick={() => navigate('systems')}>Xem tất cả <ArrowRight size={15} /></button></div>
      <div className="data-table dashboard-table"><div className="table-row table-head"><span>Hệ thống</span><span>Hoạt động</span><span>Thành công</span><span>Thất bại</span><span>Modules</span><span /></div>{systems.map((system) => {
        const successRate = system.runs ? Math.round(system.succeeded / system.runs * 100) : 0
        const failureRate = system.runs ? Math.round(system.failed / system.runs * 100) : 0
        return <button className="table-row table-button" key={system.id} onClick={() => navigate('system', { systemId: system.id })}><span className="strong-cell"><i className={`system-health health-${system.status === 'healthy' ? 'green' : system.status === 'degraded' ? 'amber' : 'red'}`} />{system.id}</span><span><Activity size={15} />{system.runs ? `${system.runs} lượt chạy` : 'Chưa có dữ liệu'}</span><span className="progress-value"><i className="progress"><b className="success-fill" style={{ width: `${successRate}%` }} /></i>{system.runs ? `${successRate}%` : '—'}</span><span className="progress-value"><i className="progress"><b className="failed-fill" style={{ width: `${failureRate}%` }} /></i>{system.runs ? `${failureRate}%` : '—'}</span><span><Box size={15} />{system.modules.length} modules</span><ChevronRight size={16} /></button>
      })}</div>
    </section>
  </>
}

export function SystemsPage({ navigate }: { navigate: Navigate }) {
  const [query, setQuery] = useState('')
  const [modal, setModal] = useState(false)
  const [dcimQuery, setDcimQuery] = useState('')
  const [searched, setSearched] = useState(false)
  const [selected, setSelected] = useState(false)
  const filtered = systems.filter((system) => `${system.id} ${system.unit} ${system.description}`.toLowerCase().includes(query.toLowerCase()))
  return <>
    <PageHeader title="Systems" description="Quản lý danh sách hệ thống trong Release Portal." action={<button className="primary-button" onClick={() => setModal(true)}><Plus size={16} />New System</button>} />
    <section className="panel table-panel">
      <div className="table-toolbar"><label className="input-with-icon"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Tìm kiếm hệ thống…" /></label></div>
      <div className="data-table systems-table"><div className="table-row table-head"><span>Hệ thống</span><span>Đơn vị</span><span>Mô tả</span><span>Modules</span><span>Trạng thái</span><span /></div>{filtered.map((system) => <button className="table-row table-button" key={system.id} onClick={() => navigate('system', { systemId: system.id })}><span className="strong-cell"><i className={`system-health health-${system.status === 'healthy' ? 'green' : system.status === 'degraded' ? 'amber' : 'red'}`} />{system.id}</span><span className="truncate">{system.unit}</span><span className="truncate">{system.description}</span><span><Box size={15} />{system.modules.length} modules</span><StatusPill status={system.status[0].toUpperCase() + system.status.slice(1)} /><ChevronRight size={16} /></button>)}</div>
    </section>
    {modal && <Modal title="Create new system" description="Search a DCIM service and add it to Release Portal." onClose={() => setModal(false)} footer={<><button className="secondary-button" onClick={() => setModal(false)}>Cancel</button><button className="primary-button" disabled={!selected} onClick={() => setModal(false)}>Create System</button></>}>
      <label className="field"><span>DCIM service</span><div className="search-action"><input value={dcimQuery} onChange={(event) => { setDcimQuery(event.target.value); setSearched(false) }} placeholder="Search by service name or code" /><button className="secondary-button" disabled={dcimQuery.trim().length < 2} onClick={() => setSearched(true)}><Search size={15} />Search</button></div></label>
      {searched && <button className={`dcim-result ${selected ? 'selected' : ''}`} onClick={() => setSelected(true)}><span className="result-icon"><Layers3 size={18} /></span><span><strong>netChat</strong><small>VTN_CNTT_MSS_686 · Trung tâm nền tảng Công nghệ và Chuyển đổi số</small></span><em>Tier 2</em></button>}
      {selected && <div className="form-grid"><label className="field full"><span>Display name</span><input defaultValue="netChat" /></label><label className="field"><span>Service code</span><input value="VTN_CNTT_MSS_686" readOnly /></label><label className="field"><span>Tenant / Unit</span><input value="Trung tâm nền tảng Công nghệ và Chuyển đổi số" readOnly /></label><label className="field full"><span>Description</span><textarea defaultValue="Real-time messaging platform for internal team communication." /></label></div>}
    </Modal>}
  </>
}

export function ServersPage() {
  const [items, setItems] = useState(seedServers)
  const [query, setQuery] = useState('')
  const [environment, setEnvironment] = useState('All environments')
  const [status, setStatus] = useState('All statuses')
  const [modal, setModal] = useState(false)
  const [newServer, setNewServer] = useState({ id: '', ip: '', environment: 'Dev' as PortalServer['environment'] })
  const filtered = useMemo(() => items.filter((item) => `${item.id} ${item.systemId} ${item.ip}`.toLowerCase().includes(query.toLowerCase()) && (environment === 'All environments' || item.environment === environment) && (status === 'All statuses' || item.status === status)), [items, query, environment, status])
  const addServer = () => { setItems((current) => [...current, { ...newServer, systemId: 'netChat', status: 'Online', lastChecked: 'Vừa xong' }]); setModal(false) }
  return <>
    <PageHeader title="Servers" description="Quản lý máy chủ triển khai được đồng bộ từ DCIM." action={<div className="heading-actions"><button className="secondary-button"><CloudDownload size={16} />Đồng bộ từ DCIM</button><button className="primary-button" onClick={() => setModal(true)}><Plus size={16} />Thêm server</button></div>} />
    <div className="sync-note"><CheckCircle2 size={15} />Last sync: 28/04/2025 09:14 · 8 servers from DCIM</div>
    <section className="panel table-panel"><div className="table-toolbar server-filters"><label className="input-with-icon"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Tìm hostname, IP, hệ thống…" /></label><select value={environment} onChange={(event) => setEnvironment(event.target.value)}><option>All environments</option><option>Dev</option><option>Staging</option><option>Production</option></select><select value={status} onChange={(event) => setStatus(event.target.value)}><option>All statuses</option><option>Online</option><option>Bảo trì</option><option>Offline</option></select></div>
      <div className="data-table servers-table"><div className="table-row table-head"><span><input type="checkbox" aria-label="Chọn tất cả" /></span><span>Server</span><span>Hệ thống</span><span>IP address</span><span>Environment</span><span>Status</span><span>Last checked</span><span /></div>{filtered.map((server) => <div className="table-row" key={server.id}><span><input type="checkbox" aria-label={`Chọn ${server.id}`} /></span><span className="strong-cell"><Server size={16} />{server.id}</span><span>{server.systemId}</span><span className="mono">{server.ip}</span><span className={`env-badge env-${server.environment.toLowerCase()}`}>{server.environment}</span><StatusPill status={server.status} /><span>{server.lastChecked}</span><span className="row-actions"><button aria-label={`Sửa ${server.id}`}><Pencil size={15} /></button><button aria-label={`Xóa ${server.id}`}><Trash2 size={15} /></button><button aria-label={`Thêm thao tác ${server.id}`}><MoreHorizontal size={16} /></button></span></div>)}</div>
      <div className="pagination"><span>Showing 1–{filtered.length} of {filtered.length}</span><div><select aria-label="Số dòng mỗi trang"><option>10 / page</option><option>20 / page</option></select><button disabled><ChevronLeft size={16} /></button><button className="page-active">1</button><button disabled><ChevronRight size={16} /></button></div></div>
    </section>
    {modal && <Modal title="Thêm server" description="Thêm một server thủ công vào inventory." onClose={() => setModal(false)} footer={<><button className="secondary-button" onClick={() => setModal(false)}>Hủy</button><button className="primary-button" disabled={!newServer.id || !/^\d+\.\d+\.\d+\.\d+$/.test(newServer.ip)} onClick={addServer}>Thêm server</button></>}><div className="form-grid"><label className="field full"><span>Server name</span><input value={newServer.id} onChange={(event) => setNewServer({ ...newServer, id: event.target.value })} placeholder="srv-app-01" /></label><label className="field full"><span>IP address</span><input value={newServer.ip} onChange={(event) => setNewServer({ ...newServer, ip: event.target.value })} placeholder="10.60.12.21" /></label><div className="field full"><span>Environment</span><div className="segmented">{(['Dev', 'Staging', 'Production'] as const).map((env) => <button className={newServer.environment === env ? 'active' : ''} onClick={() => setNewServer({ ...newServer, environment: env })} key={env}>{env}</button>)}</div></div></div></Modal>}
  </>
}

export function SystemPage({ systemId, navigate }: { systemId: string; navigate: Navigate }) {
  const system = systems.find((item) => item.id === systemId) ?? systems[0]
  return <>
    <div className="system-heading"><div><div className="title-status"><h1>{system.id}</h1><StatusPill status={system.status[0].toUpperCase() + system.status.slice(1)} /></div><p>{system.description}</p><small>Đơn vị: {system.unit} · Owner: {system.owner}</small></div><button className="primary-button" onClick={() => navigate('new-module', { systemId })}><Plus size={16} />New Module</button></div>
    <DoraCards />
    <section className="modules-section"><div className="section-heading"><h2>{system.modules.length} modules</h2><span>Last updated just now</span></div><div className="module-grid">{system.modules.map((module) => <article className="module-card" key={module.id}><div className="module-card-top"><span className={`module-icon ${module.type === 'Frontend' ? 'blue' : 'purple'}`}><Box size={19} /></span><div><h3>{module.name}</h3><p>{module.description}</p></div><em className={`type-badge ${module.type === 'Frontend' ? 'blue' : 'purple'}`}>{module.type}</em></div><label>Versions</label><div className="version-chips">{module.versions.map((version) => <span key={version}>{version}</span>)}</div><label>Environments</label><div className="environment-grid">{['Dev', 'Staging', 'Production'].map((environment) => <div key={environment}><span>{environment}</span><StatusPill status="Deployed" /></div>)}</div><button className="module-view-button" onClick={() => navigate('module', { systemId, moduleId: module.id })}>View Module <ArrowRight size={15} /></button></article>)}</div></section>
  </>
}
