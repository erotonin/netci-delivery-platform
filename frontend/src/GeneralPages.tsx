import { useEffect, useMemo, useState } from 'react'
import {
  Activity, ArrowRight, Box, CheckCircle2, ChevronLeft, ChevronRight, CircleAlert,
  CloudDownload, Layers3, MoreHorizontal, Pencil, Plus, Search, Server, Trash2,
} from 'lucide-react'
import { createSystem, deleteSystem, getPortalDashboard, getSystem, listServerInventory, listSystems, searchDcimServices, type DcimService, type PortalDashboard, type ServerInventoryItem } from './api/netciClient'
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

function serverFromApi(item: ServerInventoryItem): PortalServer {
  const environment: PortalServer['environment'] = item.environment === 'prod' ? 'Production' : item.environment === 'staging' ? 'Staging' : 'Dev'
  const status: PortalServer['status'] = item.status === 'maintenance' ? 'Bảo trì' : item.status === 'offline' ? 'Offline' : 'Online'
  return { id: item.hostname, systemId: item.systemId, ip: item.ipAddress, environment, status, lastChecked: 'API · vừa xong' }
}

export function DashboardPage({ navigate }: { navigate: Navigate }) {
  const [dashboard, setDashboard] = useState<PortalDashboard>({
    kpis: { systems: systems.length, modules: systems.reduce((total, system) => total + system.modules.length, 0), pipelineRuns: systems.reduce((total, system) => total + system.runs, 0), successRate: 73, failureRate: 27 },
    pipelineActivity: activity.map((item, index) => ({ date: `2025-04-${String(index + 22).padStart(2, '0')}`, succeeded: item.success, failed: item.failed })),
    systems: systems.map((system) => ({ id: system.id, unit: system.unit, description: system.description, owner: system.owner, status: system.status, moduleCount: system.modules.length, pipelineRuns: system.runs, failedRuns: system.failed, modules: [] })),
  })
  const [loadError, setLoadError] = useState(false)
  useEffect(() => {
    let active = true
    getPortalDashboard().then((response) => { if (active) { setDashboard(response); setLoadError(false) } }).catch(() => { if (active) setLoadError(true) })
    return () => { active = false }
  }, [])
  const successfulRuns = Math.max(0, Math.round(dashboard.kpis.pipelineRuns * dashboard.kpis.successRate / 100))
  const kpis = [
    { label: 'Tổng số hệ thống', value: String(dashboard.kpis.systems), sub: `${dashboard.kpis.modules} module`, icon: Layers3, tone: 'pink' },
    { label: 'Mức độ hoạt động', value: String(dashboard.kpis.pipelineRuns), sub: 'Lượt chạy pipeline', icon: Activity, tone: 'blue' },
    { label: 'Tỉ lệ thành công', value: `${dashboard.kpis.successRate}%`, sub: `${successfulRuns} lượt thành công`, icon: CheckCircle2, tone: 'green' },
    { label: 'Tỉ lệ thất bại', value: `${dashboard.kpis.failureRate}%`, sub: `${Math.max(dashboard.kpis.pipelineRuns - successfulRuns, 0)} lượt thất bại`, icon: CircleAlert, tone: 'red' },
  ]
  const maximumActivity = Math.max(1, ...dashboard.pipelineActivity.map((item) => item.succeeded + item.failed))
  return <>
    <PageHeader title="Dashboard" description="Monitor system activity and release health across the platform." />
    {loadError && <div className="sync-note is-warning" role="status"><CircleAlert size={15} />Không thể đồng bộ dashboard từ API; đang hiển thị dữ liệu mẫu gần nhất.</div>}
    <div className="kpi-grid">{kpis.map((item) => <article className="kpi-card" key={item.label}><span className={`kpi-icon tone-${item.tone}`}><item.icon size={19} /></span><div><p>{item.label}</p><strong>{item.value}</strong><small>{item.sub}</small></div></article>)}</div>
    <section className="panel chart-panel">
      <div className="panel-heading"><div><h2>Hoạt động pipeline · 7 ngày gần nhất</h2><p>Release activity across all systems</p></div><div className="chart-legend"><span><i className="legend-success" />Thành công</span><span><i className="legend-failed" />Thất bại</span></div></div>
      <div className="bar-chart"><div className="chart-y"><span>{maximumActivity}</span><span>{Math.round(maximumActivity * 2 / 3)}</span><span>{Math.round(maximumActivity / 3)}</span><span>0</span></div><div className="chart-grid-lines"><i /><i /><i /><i /></div><div className="bar-groups">{dashboard.pipelineActivity.map((item) => <div className="bar-group" key={item.date}><div className="bar-stack"><i className="bar-success" style={{ height: `${item.succeeded / maximumActivity * 116}px` }} /><i className="bar-failed" style={{ height: `${item.failed / maximumActivity * 116}px` }} /></div><span>{new Intl.DateTimeFormat('vi-VN', { day: '2-digit', month: '2-digit', timeZone: 'UTC' }).format(new Date(`${item.date}T00:00:00Z`))}</span></div>)}</div></div>
    </section>
    <section className="panel activity-panel">
      <div className="panel-heading"><div><h2>Mức độ hoạt động theo hệ thống</h2><p>Pipeline activity by system and current health</p></div><button className="text-button" onClick={() => navigate('systems')}>Xem tất cả <ArrowRight size={15} /></button></div>
      <div className="data-table dashboard-table"><div className="table-row table-head"><span>Hệ thống</span><span>Hoạt động</span><span>Thành công</span><span>Thất bại</span><span>Modules</span><span /></div>{dashboard.systems.map((system) => {
        const successRate = system.pipelineRuns ? Math.round((system.pipelineRuns - system.failedRuns) / system.pipelineRuns * 100) : 0
        const failureRate = system.pipelineRuns ? Math.round(system.failedRuns / system.pipelineRuns * 100) : 0
        return <button className="table-row table-button" key={system.id} onClick={() => navigate('system', { systemId: system.id })}><span className="strong-cell"><i className={`system-health health-${system.status === 'healthy' ? 'green' : system.status === 'degraded' ? 'amber' : 'red'}`} />{system.id}</span><span><Activity size={15} />{system.pipelineRuns ? `${system.pipelineRuns} lượt chạy` : 'Chưa có dữ liệu'}</span><span className="progress-value"><i className="progress"><b className="success-fill" style={{ width: `${successRate}%` }} /></i>{system.pipelineRuns ? `${successRate}%` : '—'}</span><span className="progress-value"><i className="progress"><b className="failed-fill" style={{ width: `${failureRate}%` }} /></i>{system.pipelineRuns ? `${failureRate}%` : '—'}</span><span><Box size={15} />{system.moduleCount} modules</span><ChevronRight size={16} /></button>
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
  const [editingId, setEditingId] = useState<string | null>(null)
  const [details, setDetails] = useState<PortalServer | null>(null)
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [syncing, setSyncing] = useState(false)
  const [lastSync, setLastSync] = useState('28/04/2025 09:14')
  const [pageSize, setPageSize] = useState(10)
  const [page, setPage] = useState(1)
  const [newServer, setNewServer] = useState({ id: '', systemId: 'hello-container', ip: '', environment: 'Dev' as PortalServer['environment'] })
  useEffect(() => {
    window.sessionStorage.setItem(SERVER_SESSION_KEY, JSON.stringify(items))
  }, [items])
  useEffect(() => {
    let active = true
    listServerInventory().then((result) => { if (active) { setItems(result.map(serverFromApi)); setLastSync('API · vừa xong') } }).catch((error) => { if (active) notify(error instanceof Error ? error.message : 'Không thể tải inventory từ netCI API.', 'error') })
    return () => { active = false }
  }, [])
  const duplicateServer = items.some((item) => item.id !== editingId && (item.id.toLowerCase() === newServer.id.trim().toLowerCase() || item.ip === newServer.ip.trim()))
  const filtered = useMemo(() => items.filter((item) => `${item.id} ${item.systemId} ${item.ip}`.toLowerCase().includes(query.toLowerCase()) && (environment === 'All environments' || item.environment === environment) && (status === 'All statuses' || item.status === status)), [items, query, environment, status])
  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize))
  const visible = filtered.slice((page - 1) * pageSize, page * pageSize)
  useEffect(() => setPage((current) => Math.min(current, totalPages)), [totalPages])
  const saveServer = () => {
    const next: PortalServer = { ...newServer, status: 'Online', lastChecked: 'Windows preview · vừa xong' }
    setItems((current) => editingId ? current.map((item) => item.id === editingId ? next : item) : [...current, next])
    setModal(false)
    notify(`${next.id} đã được ${editingId ? 'cập nhật' : 'thêm'} trong Windows preview inventory.`)
    setEditingId(null)
    setNewServer({ id: '', systemId: 'hello-container', ip: '', environment: 'Dev' })
  }
  const openEdit = (server: PortalServer) => { setEditingId(server.id); setNewServer({ id: server.id, systemId: server.systemId, ip: server.ip, environment: server.environment }); setModal(true) }
  const syncServers = async () => {
    setSyncing(true)
    try { const result = await listServerInventory(); setItems(result.map(serverFromApi)); setSelectedIds([]); setLastSync('API · vừa xong'); notify('Đã đồng bộ inventory từ netCI API / DCIM fixture.') } catch (error) { notify(error instanceof Error ? error.message : 'Không thể đồng bộ inventory.', 'error') } finally { setSyncing(false) }
  }
  const toggleAll = () => setSelectedIds(visible.length > 0 && visible.every((item) => selectedIds.includes(item.id)) ? selectedIds.filter((id) => !visible.some((item) => item.id === id)) : [...new Set([...selectedIds, ...visible.map((item) => item.id)])])
  return <>
    <PageHeader title="Servers" description="Danh sách server được đồng bộ từ DCIM, dùng để cấu hình deployment khi khởi tạo module." action={<div className="heading-actions"><button className="secondary-button" disabled={syncing} onClick={syncServers}><CloudDownload size={16} />{syncing ? 'Đang đồng bộ…' : 'Đồng bộ từ DCIM'}</button><button className="primary-button" onClick={() => { setEditingId(null); setNewServer({ id: '', systemId: 'hello-container', ip: '', environment: 'Dev' }); setModal(true) }}><Plus size={16} />Thêm server</button></div>} />
    <div className="sync-note"><CheckCircle2 size={15} />Last sync: {lastSync} · {items.length} servers from DCIM</div>
    <section className="panel table-panel"><div className="table-toolbar server-filters"><label className="input-with-icon"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Tìm hostname, IP, hệ thống…" /></label><select value={environment} onChange={(event) => setEnvironment(event.target.value)}><option>All environments</option><option>Dev</option><option>Staging</option><option>Production</option></select><select value={status} onChange={(event) => setStatus(event.target.value)}><option>All statuses</option><option>Online</option><option>Bảo trì</option><option>Offline</option></select></div>
      <div className="data-table servers-table"><div className="table-row table-head"><span><input type="checkbox" aria-label="Chọn tất cả trên trang" checked={visible.length > 0 && visible.every((item) => selectedIds.includes(item.id))} onChange={toggleAll} /></span><span>Server</span><span>Hệ thống</span><span>IP address</span><span>Environment</span><span>Status</span><span>Last checked</span><span /></div>{visible.map((server) => <div className="table-row" key={server.id}><span><input type="checkbox" aria-label={`Chọn ${server.id}`} checked={selectedIds.includes(server.id)} onChange={() => setSelectedIds((current) => current.includes(server.id) ? current.filter((id) => id !== server.id) : [...current, server.id])} /></span><span className="strong-cell"><Server size={16} />{server.id}</span><span>{server.systemId}</span><span className="mono">{server.ip}</span><span className={`env-badge env-${server.environment.toLowerCase()}`}>{server.environment}</span><StatusPill status={server.status} /><span>{server.lastChecked}</span><span className="row-actions"><button aria-label={`Sửa ${server.id}`} onClick={() => openEdit(server)}><Pencil size={15} /></button><button aria-label={`Xóa ${server.id}`} onClick={() => { setItems((current) => current.filter((item) => item.id !== server.id)); setSelectedIds((current) => current.filter((id) => id !== server.id)); notify(`${server.id} đã được xóa khỏi Windows preview inventory.`, 'info') }}><Trash2 size={15} /></button><button aria-label={`Chi tiết ${server.id}`} onClick={() => setDetails(server)}><MoreHorizontal size={16} /></button></span></div>)}</div>{!filtered.length && <div className="empty-table"><Server size={22} /><strong>Không có server phù hợp</strong><span>Thử đổi môi trường, trạng thái hoặc từ khóa.</span></div>}
      <div className="pagination"><span>Showing {filtered.length ? (page - 1) * pageSize + 1 : 0}–{Math.min(page * pageSize, filtered.length)} of {filtered.length}</span><div><select aria-label="Số dòng mỗi trang" value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(1) }}><option value={10}>10 / page</option><option value={20}>20 / page</option></select><button disabled={page <= 1} aria-label="Trang trước" onClick={() => setPage((current) => Math.max(1, current - 1))}><ChevronLeft size={16} /></button><button className="page-active" disabled aria-current="page">{page}</button><button disabled={page >= totalPages} aria-label="Trang sau" onClick={() => setPage((current) => Math.min(totalPages, current + 1))}><ChevronRight size={16} /></button></div></div>
    </section>
    {modal && <Modal title={editingId ? `Sửa ${editingId}` : 'Thêm server'} description="Windows preview overlay; nguồn chính vẫn là DCIM adapter qua netCI API." onClose={() => setModal(false)} footer={<><button className="secondary-button" onClick={() => setModal(false)}>Hủy</button><button className="primary-button" disabled={!newServer.id || !isIpv4(newServer.ip) || duplicateServer} onClick={saveServer}>{editingId ? 'Lưu thay đổi' : 'Thêm server'}</button></>}><div className="form-grid"><label className="field full"><span>Server name</span><input value={newServer.id} onChange={(event) => setNewServer({ ...newServer, id: event.target.value })} placeholder="srv-app-01" /></label><label className="field full"><span>System</span><select value={newServer.systemId} onChange={(event) => setNewServer({ ...newServer, systemId: event.target.value })}>{systems.map((system) => <option key={system.id}>{system.id}</option>)}</select></label><label className="field full"><span>IP address</span><input value={newServer.ip} onChange={(event) => setNewServer({ ...newServer, ip: event.target.value })} placeholder="10.60.12.21" /></label>{newServer.ip && !isIpv4(newServer.ip) && <div className="inline-error full" role="alert"><CircleAlert size={15} />IP phải gồm 4 octet hợp lệ từ 0 đến 255.</div>}{duplicateServer && <div className="inline-error full" role="alert"><CircleAlert size={15} />Hostname hoặc IP đã tồn tại.</div>}<div className="field full"><span>Environment</span><div className="segmented">{(['Dev', 'Staging', 'Production'] as const).map((env) => <button className={newServer.environment === env ? 'active' : ''} onClick={() => setNewServer({ ...newServer, environment: env })} key={env}>{env}</button>)}</div></div></div></Modal>}
    {details && <Modal title={details.id} description="Inventory details from the current Portal projection." onClose={() => setDetails(null)} footer={<button className="primary-button" onClick={() => setDetails(null)}>Close</button>}><div className="request-summary"><div><span>System</span><strong>{details.systemId}</strong></div><div><span>IP address</span><strong className="mono">{details.ip}</strong></div><div><span>Status</span><StatusPill status={details.status} /></div></div><div className="form-grid"><label className="field"><span>Environment</span><input readOnly value={details.environment} /></label><label className="field"><span>Last checked</span><input readOnly value={details.lastChecked} /></label></div></Modal>}
  </>
}

export function SystemPage({ systemId, navigate }: { systemId: string; navigate: Navigate }) {
  const { notify } = usePortalFeedback()
  const fallback: PortalSystemView = systems.find((item) => item.id === systemId) ?? { id: systemId, code: '', unit: 'Đang đồng bộ từ DCIM', description: 'System chưa có module delivery nào trong Release Portal.', owner: 'Admin', status: 'healthy', modules: [], runs: 0, succeeded: 0, failed: 0 }
  const [system, setSystem] = useState<PortalSystemView>(fallback)
  const [deleteModal, setDeleteModal] = useState(false)
  const [deleting, setDeleting] = useState(false)

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

  const handleDeleteSystem = async () => {
    setDeleting(true)
    try {
      await deleteSystem(systemId)
      notify(`Đã xóa system ${systemId}.`)
      setDeleteModal(false)
      navigate('systems')
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Không thể xóa system.', 'error')
    } finally {
      setDeleting(false)
    }
  }

  return <>
    <div className="system-heading"><div><div className="title-status"><h1>{system.id}</h1><StatusPill status={system.status[0].toUpperCase() + system.status.slice(1)} /></div><p>{system.description}</p><small>Đơn vị: {system.unit} · Owner: {system.owner}</small></div><div className="heading-actions"><button className="danger-button" onClick={() => setDeleteModal(true)}><Trash2 size={16} />Delete System</button><button className="primary-button" onClick={() => navigate('new-module', { systemId })}><Plus size={16} />New Module</button></div></div>
    <div className="dora-section-label">DORA METRICS · Q2 2025</div>
    <DoraCards />
    <section className="modules-section"><div className="section-heading"><h2>{system.modules.length} modules</h2><span>Last updated just now</span></div><div className="module-grid">{system.modules.map((module) => <article className="module-card" key={module.id}><div className="module-card-top"><span className={`module-icon ${module.type === 'Frontend' ? 'blue' : 'purple'}`}><Box size={19} /></span><div><h3>{module.name}</h3><p>{module.description}</p></div><em className={`type-badge ${module.type === 'Frontend' ? 'blue' : 'purple'}`}>{module.type}</em></div><label>Versions</label><div className="version-chips">{module.versions.map((version) => <span key={version}>{version}</span>)}</div><label>Environments</label><div className="environment-grid">{['Dev', 'Staging', 'Production'].map((environment) => <div key={environment}><span>{environment}</span><StatusPill status="Deployed" /></div>)}</div><button className="module-view-button" onClick={() => navigate('module', { systemId, moduleId: module.id })}>View Module <ArrowRight size={15} /></button></article>)}{!system.modules.length && <div className="empty-module-state"><Box size={27} /><strong>No modules yet</strong><span>Add a DCIM module to configure its delivery lifecycle.</span><button className="primary-button" onClick={() => navigate('new-module', { systemId })}><Plus size={15} />Add module</button></div>}</div></section>
    {deleteModal && <Modal title="Delete System" description={`Are you sure you want to delete system "${system.id}"?`} onClose={() => setDeleteModal(false)} footer={<><button className="secondary-button" onClick={() => setDeleteModal(false)}>Cancel</button><button className="danger-button" disabled={deleting} onClick={handleDeleteSystem}>{deleting ? 'Deleting…' : 'Confirm Delete'}</button></>}><div className="inline-error" role="status">Warning: This action will delete system {system.id} and detach all associated modules from the Release Portal.</div></Modal>}
  </>
}
