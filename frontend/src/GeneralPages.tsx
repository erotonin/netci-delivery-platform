import { useEffect, useMemo, useState } from 'react'
import {
  Activity, ArrowRight, Box, CheckCircle2, ChevronLeft, ChevronRight, CircleAlert,
  CloudDownload, Layers3, MoreHorizontal, Pencil, Plus, Search, Server, Trash2,
} from 'lucide-react'
import { createSystem, getSystem, listSystems, searchDcimServices, type DcimService } from './api/netciClient'
import { usePortalFeedback } from './PortalFeedback'
import { DoraCards, Modal, PageHeader, StatusPill, type Navigate } from './PortalShell'
import { activity, servers as seedServers, systems, type PortalServer, type PortalSystem as PortalSystemView } from './portalData'

const SERVER_SESSION_KEY = 'netci.servers'

function loadServerSession(): PortalServer[] {
  try {
    const saved = window.sessionStorage.getItem(SERVER_SESSION_KEY)
    const parsed = saved ? JSON.parse(saved) : null
    return Array.isArray(parsed) ? parsed : seedServers
  } catch {
    return seedServers
  }
}

function isIpv4(value: string): boolean {
  const parts = value.trim().split('.')
  return parts.length === 4 && parts.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255)
}

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
  const { notify } = usePortalFeedback()
  const [items, setItems] = useState(systems)
  const [query, setQuery] = useState('')
  const [modal, setModal] = useState(false)
  const [dcimQuery, setDcimQuery] = useState('')
  const [results, setResults] = useState<DcimService[]>([])
  const [selected, setSelected] = useState<DcimService | null>(null)
  const [searching, setSearching] = useState(false)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')
  useEffect(() => {
    listSystems().then((response) => setItems(response.map((item) => ({
      id: item.id,
      code: systems.find((seed) => seed.id === item.id)?.code ?? '',
      unit: item.unit,
      description: item.description,
      owner: item.owner,
      status: (['healthy', 'degraded', 'critical'].includes(item.status) ? item.status : 'healthy') as PortalSystemView['status'],
      modules: item.modules.map((module) => ({ id: module.id, name: module.name, type: module.type, description: module.description, versions: module.versions, runtime: module.runtime })),
      runs: item.pipelineRuns,
      succeeded: Math.max(item.pipelineRuns - item.failedRuns, 0),
      failed: item.failedRuns,
    })))).catch(() => undefined)
  }, [])
  const filtered = items.filter((system) => `${system.id} ${system.code} ${system.unit} ${system.description}`.toLowerCase().includes(query.toLowerCase()))
  const searchDcim = async () => {
    setSearching(true)
    setFormError('')
    setSelected(null)
    try {
      const response = await searchDcimServices(dcimQuery)
      setResults(response.items)
      if (!response.items.length) setFormError('Không tìm thấy service phù hợp trong DCIM.')
    } catch {
      setResults([])
      setFormError('Không thể kết nối DCIM adapter. Kiểm tra backend rồi thử lại.')
    } finally {
      setSearching(false)
    }
  }
  const saveSystem = async () => {
    if (!selected) return
    if (items.some((item) => item.id === selected.name)) { setFormError(`${selected.name} đã tồn tại trong Release Portal.`); return }
    setSaving(true)
    setFormError('')
    try {
      await createSystem({ id: selected.name, unit: selected.tenant, description: selected.description })
      setItems((current) => [...current, { id: selected.name, code: selected.code, unit: selected.tenant, description: selected.description, owner: 'Admin', status: 'healthy', modules: [], runs: 0, succeeded: 0, failed: 0 }])
      setModal(false)
      notify(`${selected.name} đã được thêm từ DCIM.`)
    } catch (error) {
      setFormError(error instanceof Error ? error.message : 'Không thể tạo system.')
    } finally {
      setSaving(false)
    }
  }
  return <>
    <PageHeader title="Systems" description="Quản lý danh sách hệ thống trong Release Portal." action={<button className="primary-button" onClick={() => setModal(true)}><Plus size={16} />New System</button>} />
    <section className="panel table-panel">
      <div className="table-toolbar"><label className="input-with-icon"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Tìm kiếm hệ thống…" /></label></div>
      <div className="data-table systems-table"><div className="table-row table-head"><span>Hệ thống</span><span>Đơn vị</span><span>Mô tả</span><span>Modules</span><span>Trạng thái</span><span /></div>{filtered.map((system) => <button className="table-row table-button" key={system.id} onClick={() => navigate('system', { systemId: system.id })}><span className="strong-cell"><i className={`system-health health-${system.status === 'healthy' ? 'green' : system.status === 'degraded' ? 'amber' : 'red'}`} />{system.id}</span><span className="truncate">{system.unit}</span><span className="truncate">{system.description}</span><span><Box size={15} />{system.modules.length} modules</span><StatusPill status={system.status[0].toUpperCase() + system.status.slice(1)} /><ChevronRight size={16} /></button>)}</div>{!filtered.length && <div className="empty-table"><Search size={22} /><strong>Không tìm thấy hệ thống</strong><span>Thử tên, mã service hoặc đơn vị khác.</span></div>}
    </section>
    {modal && <Modal title="Create new system" description="Search a DCIM service and add it to Release Portal." onClose={() => setModal(false)} footer={<><button className="secondary-button" onClick={() => setModal(false)}>Cancel</button><button className="primary-button" disabled={!selected || saving} onClick={saveSystem}>{saving ? 'Creating…' : 'Create System'}</button></>}>
      <label className="field"><span>DCIM service</span><div className="search-action"><input value={dcimQuery} onChange={(event) => { setDcimQuery(event.target.value); setResults([]); setSelected(null); setFormError('') }} onKeyDown={(event) => { if (event.key === 'Enter' && dcimQuery.trim().length >= 2) searchDcim() }} placeholder="Search by service name or code" /><button className="secondary-button" disabled={dcimQuery.trim().length < 2 || searching} onClick={searchDcim}><Search size={15} />{searching ? 'Searching…' : 'Search'}</button></div></label>
      {results.map((service) => <button className={`dcim-result ${selected?.id === service.id ? 'selected' : ''}`} onClick={() => { setSelected(service); setFormError('') }} key={service.id}><span className="result-icon"><Layers3 size={18} /></span><span><strong>{service.name}</strong><small>{service.code} · {service.tenant}</small></span><em>{service.tier}</em></button>)}
      {formError && <div className="inline-error" role="alert"><CircleAlert size={15} />{formError}</div>}
      {selected && <div className="form-grid"><label className="field full"><span>Display name</span><input value={selected.name} readOnly /></label><label className="field"><span>Service code</span><input value={selected.code} readOnly /></label><label className="field"><span>Tenant / Unit</span><input value={selected.tenant} readOnly /></label><label className="field full"><span>Description</span><textarea value={selected.description} readOnly /></label></div>}
    </Modal>}
  </>
}

export function ServersPage() {
  const { notify } = usePortalFeedback()
  const [items, setItems] = useState<PortalServer[]>(loadServerSession)
  const [query, setQuery] = useState('')
  const [environment, setEnvironment] = useState('All environments')
  const [status, setStatus] = useState('All statuses')
  const [modal, setModal] = useState(false)
  const [lastSync, setLastSync] = useState('28/04/2025 09:14')
  const [newServer, setNewServer] = useState({ id: '', ip: '', environment: 'Dev' as PortalServer['environment'] })
  useEffect(() => {
    window.sessionStorage.setItem(SERVER_SESSION_KEY, JSON.stringify(items))
  }, [items])
  const duplicateServer = items.some((item) => item.id.toLowerCase() === newServer.id.trim().toLowerCase() || item.ip === newServer.ip.trim())
  const filtered = useMemo(() => items.filter((item) => `${item.id} ${item.systemId} ${item.ip}`.toLowerCase().includes(query.toLowerCase()) && (environment === 'All environments' || item.environment === environment) && (status === 'All statuses' || item.status === status)), [items, query, environment, status])
  const addServer = () => { setItems((current) => [...current, { ...newServer, systemId: 'netChat', status: 'Online', lastChecked: 'Vừa xong' }]); setModal(false); notify(`${newServer.id} đã được thêm vào inventory.`); setNewServer({ id: '', ip: '', environment: 'Dev' }) }
  const syncServers = () => { setLastSync('Vừa xong'); notify('Đã đồng bộ inventory từ DCIM.') }
  return <>
    <PageHeader title="Servers" description="Quản lý máy chủ triển khai được đồng bộ từ DCIM." action={<div className="heading-actions"><button className="secondary-button" onClick={syncServers}><CloudDownload size={16} />Đồng bộ từ DCIM</button><button className="primary-button" onClick={() => setModal(true)}><Plus size={16} />Thêm server</button></div>} />
    <div className="sync-note"><CheckCircle2 size={15} />Last sync: {lastSync} · {items.length} servers from DCIM</div>
    <section className="panel table-panel"><div className="table-toolbar server-filters"><label className="input-with-icon"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Tìm hostname, IP, hệ thống…" /></label><select value={environment} onChange={(event) => setEnvironment(event.target.value)}><option>All environments</option><option>Dev</option><option>Staging</option><option>Production</option></select><select value={status} onChange={(event) => setStatus(event.target.value)}><option>All statuses</option><option>Online</option><option>Bảo trì</option><option>Offline</option></select></div>
      <div className="data-table servers-table"><div className="table-row table-head"><span><input type="checkbox" aria-label="Chọn tất cả" /></span><span>Server</span><span>Hệ thống</span><span>IP address</span><span>Environment</span><span>Status</span><span>Last checked</span><span /></div>{filtered.map((server) => <div className="table-row" key={server.id}><span><input type="checkbox" aria-label={`Chọn ${server.id}`} /></span><span className="strong-cell"><Server size={16} />{server.id}</span><span>{server.systemId}</span><span className="mono">{server.ip}</span><span className={`env-badge env-${server.environment.toLowerCase()}`}>{server.environment}</span><StatusPill status={server.status} /><span>{server.lastChecked}</span><span className="row-actions"><button aria-label={`Sửa ${server.id}`}><Pencil size={15} /></button><button aria-label={`Xóa ${server.id}`} onClick={() => { setItems((current) => current.filter((item) => item.id !== server.id)); notify(`${server.id} đã được xóa.`, 'info') }}><Trash2 size={15} /></button><button aria-label={`Thêm thao tác ${server.id}`}><MoreHorizontal size={16} /></button></span></div>)}</div>{!filtered.length && <div className="empty-table"><Server size={22} /><strong>Không có server phù hợp</strong><span>Thử đổi môi trường, trạng thái hoặc từ khóa.</span></div>}
      <div className="pagination"><span>Showing 1–{filtered.length} of {filtered.length}</span><div><select aria-label="Số dòng mỗi trang"><option>10 / page</option><option>20 / page</option></select><button disabled><ChevronLeft size={16} /></button><button className="page-active">1</button><button disabled><ChevronRight size={16} /></button></div></div>
    </section>
    {modal && <Modal title="Thêm server" description="Thêm một server thủ công vào inventory." onClose={() => setModal(false)} footer={<><button className="secondary-button" onClick={() => setModal(false)}>Hủy</button><button className="primary-button" disabled={!newServer.id || !isIpv4(newServer.ip) || duplicateServer} onClick={addServer}>Thêm server</button></>}><div className="form-grid"><label className="field full"><span>Server name</span><input value={newServer.id} onChange={(event) => setNewServer({ ...newServer, id: event.target.value })} placeholder="srv-app-01" /></label><label className="field full"><span>IP address</span><input value={newServer.ip} onChange={(event) => setNewServer({ ...newServer, ip: event.target.value })} placeholder="10.60.12.21" /></label>{newServer.ip && !isIpv4(newServer.ip) && <div className="inline-error full" role="alert"><CircleAlert size={15} />IP phải gồm 4 octet hợp lệ từ 0 đến 255.</div>}{duplicateServer && <div className="inline-error full" role="alert"><CircleAlert size={15} />Hostname hoặc IP đã tồn tại.</div>}<div className="field full"><span>Environment</span><div className="segmented">{(['Dev', 'Staging', 'Production'] as const).map((env) => <button className={newServer.environment === env ? 'active' : ''} onClick={() => setNewServer({ ...newServer, environment: env })} key={env}>{env}</button>)}</div></div></div></Modal>}
  </>
}

export function SystemPage({ systemId, navigate }: { systemId: string; navigate: Navigate }) {
  const fallback: PortalSystemView = systems.find((item) => item.id === systemId) ?? { id: systemId, code: '', unit: 'Đang đồng bộ từ DCIM', description: 'System chưa có module delivery nào trong Release Portal.', owner: 'Admin', status: 'healthy', modules: [], runs: 0, succeeded: 0, failed: 0 }
  const [system, setSystem] = useState<PortalSystemView>(fallback)
  useEffect(() => {
    setSystem(systems.find((item) => item.id === systemId) ?? fallback)
    getSystem(systemId).then((item) => setSystem({
      id: item.id,
      code: systems.find((seed) => seed.id === item.id)?.code ?? '',
      unit: item.unit,
      description: item.description,
      owner: item.owner,
      status: (['healthy', 'degraded', 'critical'].includes(item.status) ? item.status : 'healthy') as PortalSystemView['status'],
      modules: item.modules.map((module) => ({ id: module.id, name: module.name, type: module.type, description: module.description, versions: module.versions, runtime: module.runtime })),
      runs: item.pipelineRuns,
      succeeded: Math.max(item.pipelineRuns - item.failedRuns, 0),
      failed: item.failedRuns,
    })).catch(() => undefined)
  }, [systemId])
  return <>
    <div className="system-heading"><div><div className="title-status"><h1>{system.id}</h1><StatusPill status={system.status[0].toUpperCase() + system.status.slice(1)} /></div><p>{system.description}</p><small>Đơn vị: {system.unit} · Owner: {system.owner}</small></div><button className="primary-button" onClick={() => navigate('new-module', { systemId })}><Plus size={16} />New Module</button></div>
    <DoraCards />
    <section className="modules-section"><div className="section-heading"><h2>{system.modules.length} modules</h2><span>Last updated just now</span></div><div className="module-grid">{system.modules.map((module) => <article className="module-card" key={module.id}><div className="module-card-top"><span className={`module-icon ${module.type === 'Frontend' ? 'blue' : 'purple'}`}><Box size={19} /></span><div><h3>{module.name}</h3><p>{module.description}</p></div><em className={`type-badge ${module.type === 'Frontend' ? 'blue' : 'purple'}`}>{module.type}</em></div><label>Versions</label><div className="version-chips">{module.versions.map((version) => <span key={version}>{version}</span>)}</div><label>Environments</label><div className="environment-grid">{['Dev', 'Staging', 'Production'].map((environment) => <div key={environment}><span>{environment}</span><StatusPill status="Deployed" /></div>)}</div><button className="module-view-button" onClick={() => navigate('module', { systemId, moduleId: module.id })}>View Module <ArrowRight size={15} /></button></article>)}{!system.modules.length && <div className="empty-module-state"><Box size={27} /><strong>No modules yet</strong><span>Add a DCIM module to configure its delivery lifecycle.</span><button className="primary-button" onClick={() => navigate('new-module', { systemId })}><Plus size={15} />Add module</button></div>}</div></section>
  </>
}
