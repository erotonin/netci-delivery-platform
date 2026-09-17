import { useEffect, useMemo, useState } from 'react'
import {
  Activity, ArrowRight, Box, CheckCircle2, ChevronLeft, ChevronRight, CircleAlert,
  CloudDownload, Cpu, HardDrive, Layers3, MoreHorizontal, Plus, Search, Server, ShieldAlert, ShieldCheck, Trash2, Zap,
} from 'lucide-react'
import {
  createModule,
  createSecurityWaiver,
  createSystem,
  deleteSystem,
  getDora,
  getPortalDashboard,
  getServerTelemetry,
  getSystem,
  getAgentStatus,
  executeAgentCommand,
  listSecurityWaivers,
  listServerInventory,
  listServersMaintenance,
  listSystems,
  revokeSecurityWaiver,
  searchDcimServices,
  toggleServerMaintenance,
  type AgentExecuteResponse,
  type AgentStatusItem,
  type DcimService,
  type ModulePipelineConfig,
  type PortalDashboard,
  type Runtime,
  type SecurityWaiver,
  type ServerInventoryItem,
  type ServerMaintenanceState,
  type ServerTelemetry,
} from './api/netciClient'
import { usePortalFeedback } from './PortalFeedback'
import { AsyncPanel, LoadFailure, Skeleton, useAsyncData } from './AsyncState'
import { DoraCards, Modal, PageHeader, StatusPill, type Navigate } from './PortalShell'
import type { DoraCardMetric, PortalServer, PortalSystemView } from './portalTypes'

function serverFromApi(item: ServerInventoryItem): PortalServer {
  const environment: PortalServer['environment'] = item.environment === 'prod' ? 'Production' : item.environment === 'staging' ? 'Staging' : 'Dev'
  const status: PortalServer['status'] = item.status === 'maintenance' ? 'Maintenance' : item.status === 'offline' ? 'Offline' : item.status === 'online' ? 'Online' : 'Unknown'
  const lastChecked = item.telemetry?.observedAt ?? item.agent?.lastSeenAt ?? 'Not reported'
  return { id: item.id || item.hostname, systemId: item.systemId, ip: item.ipAddress || '', environment, status, lastChecked, usedBy: item.usedBy ?? [], dcim: item.dcim ?? null, agent: item.agent ?? null, telemetry: item.telemetry ?? null }
}

export function DashboardPage({ navigate }: { navigate: Navigate }) {
  // No seeded baseline while the request is in flight. Showing invented KPIs that are
  // then replaced by real ones makes a slow API look like a healthy platform with
  // different numbers, which is the false green this project exists to prevent.
  const state = useAsyncData<PortalDashboard>(getPortalDashboard)
  return <>
    <PageHeader title="Dashboard" description="Monitor system activity and release health across the platform." />
    <AsyncPanel state={state} skeletonRows={5}>{(dashboard) => <DashboardContent dashboard={dashboard} navigate={navigate} />}</AsyncPanel>
  </>
}

function DashboardContent({ dashboard, navigate }: { dashboard: PortalDashboard; navigate: Navigate }) {
  const successfulRuns = Math.max(0, Math.round(dashboard.kpis.pipelineRuns * dashboard.kpis.successRate / 100))
  const kpis = [
    { label: 'Tổng số hệ thống', value: String(dashboard.kpis.systems), sub: `${dashboard.kpis.modules} module`, icon: Layers3, tone: 'pink' },
    { label: 'Mức độ hoạt động', value: String(dashboard.kpis.pipelineRuns), sub: 'Lượt chạy pipeline', icon: Activity, tone: 'blue' },
    { label: 'Tỉ lệ thành công', value: `${dashboard.kpis.successRate}%`, sub: `${successfulRuns} lượt thành công`, icon: CheckCircle2, tone: 'green' },
    { label: 'Tỉ lệ thất bại', value: `${dashboard.kpis.failureRate}%`, sub: `${Math.max(dashboard.kpis.pipelineRuns - successfulRuns, 0)} lượt thất bại`, icon: CircleAlert, tone: 'red' },
  ]
  const maximumActivity = Math.max(1, ...dashboard.pipelineActivity.map((item) => item.succeeded + item.failed))
  return <>
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
        return <button className="table-row table-button" key={system.id} onClick={() => navigate('system', { systemId: system.id })}><span className="strong-cell"><i className={`system-health health-${system.status === 'healthy' ? 'green' : system.status === 'degraded' ? 'amber' : system.status === 'critical' ? 'red' : 'gray'}`} />{system.id}</span><span><Activity size={15} />{system.pipelineRuns ? `${system.pipelineRuns} lượt chạy` : 'Chưa có dữ liệu'}</span><span className="progress-value"><i className="progress"><b className="success-fill" style={{ width: `${successRate}%` }} /></i>{system.pipelineRuns ? `${successRate}%` : '—'}</span><span className="progress-value"><i className="progress"><b className="failed-fill" style={{ width: `${failureRate}%` }} /></i>{system.pipelineRuns ? `${failureRate}%` : '—'}</span><span><Box size={15} />{system.moduleCount} modules</span><ChevronRight size={16} /></button>
      })}</div>
    </section>
  </>
}


export function SystemsPage({ navigate }: { navigate: Navigate }) {
  const { notify } = usePortalFeedback()
  const [items, setItems] = useState<PortalSystemView[]>([])
  const [query, setQuery] = useState('')
  const [modal, setModal] = useState(false)
  const [dcimQuery, setDcimQuery] = useState('')
  const [results, setResults] = useState<DcimService[]>([])
  const [selected, setSelected] = useState<DcimService | null>(null)
  const [searching, setSearching] = useState(false)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')
  // Tracked, not swallowed. `.catch(() => undefined)` made a failed load look exactly
  // like an empty estate -- the one reading a delivery platform must never invite.
  const [systemSource, setSystemSource] = useState<'local' | 'dcim'>('local')
  const [localId, setLocalId] = useState('')
  const [localUnit, setLocalUnit] = useState('Core Engineering')
  const [localDescription, setLocalDescription] = useState('')
  const [loadState, setLoadState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [loadError, setLoadError] = useState<Error | null>(null)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let active = true
    setLoadState('loading')
    listSystems().then((response) => { if (!active) return; setLoadState('ready'); setItems(response.map((item) => ({
      id: item.id,
      code: '',
      unit: item.unit,
      description: item.description,
      owner: item.owner,
      status: (['unknown', 'healthy', 'degraded', 'critical'].includes(item.status) ? item.status : 'unknown') as PortalSystemView['status'],
      modules: item.modules.map((module) => ({ id: module.id, name: module.name, type: module.type, description: module.description, versions: module.versions, runtime: module.runtime, environments: module.environments })),
      runs: item.pipelineRuns,
      succeeded: Math.max(item.pipelineRuns - item.failedRuns, 0),
      failed: item.failedRuns,
    }))) }).catch((cause) => {
      if (!active) return
      setLoadState('error')
      setLoadError(cause instanceof Error ? cause : new Error(String(cause)))
    })
    return () => { active = false }
  }, [attempt])
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
    let systemId = ''
    let unit = ''
    let description = ''
    let code = ''
    if (systemSource === 'local') {
      systemId = localId.trim().toLowerCase().replace(/[^a-z0-9-_]/g, '-')
      if (!systemId) { setFormError('Vui lòng nhập System ID (chữ thường, số, dấu gạch nối).'); return }
      if (items.some((item) => item.id.toLowerCase() === systemId.toLowerCase())) {
        setFormError(`Hệ thống "${systemId}" đã tồn tại trong Release Portal.`)
        return
      }
      unit = localUnit.trim() || 'Core Engineering'
      description = localDescription.trim() || 'Hệ thống dịch vụ local'
      code = systemId
    } else {
      if (!selected) return
      if (items.some((item) => item.id === selected.id)) { setFormError(`${selected.name} đã tồn tại trong Release Portal.`); return }
      systemId = selected.id
      unit = selected.tenant
      description = selected.description
      code = selected.code
    }
    setSaving(true)
    setFormError('')
    try {
      const created = await createSystem({ id: systemId, unit, description })
      setItems((current) => [...current, {
        id: created.id,
        code,
        unit: created.unit,
        description: created.description,
        owner: created.owner,
        status: (['unknown', 'healthy', 'degraded', 'critical'].includes(created.status) ? created.status : 'unknown') as PortalSystemView['status'],
        modules: [],
        runs: created.pipelineRuns,
        succeeded: Math.max(created.pipelineRuns - created.failedRuns, 0),
        failed: created.failedRuns,
      }])
      setModal(false)
      notify(`Hệ thống ${created.id} đã được khởi tạo thành công.`)
      navigate('system', { systemId: created.id })
    } catch (error) {
      setFormError(error instanceof Error ? error.message : 'Không thể tạo system.')
    } finally {
      setSaving(false)
    }
  }
  return <>
    <PageHeader title="Systems" description="Quản lý danh sách hệ thống trong Release Portal." action={<button className="primary-button" onClick={() => { setFormError(''); setModal(true) }}><Plus size={16} />New System</button>} />
    <section className="panel table-panel">
      <div className="table-toolbar"><label className="input-with-icon"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Tìm kiếm hệ thống…" /></label></div>
      <div className="data-table systems-table"><div className="table-row table-head"><span>Hệ thống</span><span>Đơn vị</span><span>Mô tả</span><span>Modules</span><span>Trạng thái</span><span /></div>{filtered.map((system) => <button className="table-row table-button" key={system.id} onClick={() => navigate('system', { systemId: system.id })}><span className="strong-cell"><i className={`system-health health-${system.status === 'healthy' ? 'green' : system.status === 'degraded' ? 'amber' : system.status === 'critical' ? 'red' : 'gray'}`} />{system.id}</span><span className="truncate">{system.unit}</span><span className="truncate">{system.description}</span><span><Box size={15} />{system.modules.length} modules</span><StatusPill status={system.status[0].toUpperCase() + system.status.slice(1)} /><ChevronRight size={16} /></button>)}</div>{!filtered.length && loadState === 'loading' && <Skeleton rows={4} />}
      {!filtered.length && loadState === 'error' && <LoadFailure error={loadError} onRetry={() => setAttempt((value) => value + 1)} />}
      {!filtered.length && loadState === 'ready' && <div className="empty-table"><Search size={22} /><strong>{query ? 'Không tìm thấy hệ thống' : 'Chưa có hệ thống nào'}</strong><span>{query ? 'Thử tên, mã service hoặc đơn vị khác.' : 'Thêm hệ thống đầu tiên để bắt đầu.'}</span></div>}
    </section>
    {modal && <Modal title="Create new system" description="Khởi tạo hệ thống mới để quản lý phân hệ, CI/CD pipeline và hạ tầng triển khai." onClose={() => setModal(false)} footer={<><button className="secondary-button" onClick={() => setModal(false)}>Cancel</button><button className="primary-button" disabled={(systemSource === 'dcim' && !selected) || (systemSource === 'local' && !localId.trim()) || saving} onClick={saveSystem}>{saving ? 'Creating…' : 'Create System'}</button></>}>
      <div className="segmented compact" style={{ marginBottom: '16px' }}>
        <button className={systemSource === 'local' ? 'active' : ''} onClick={() => { setSystemSource('local'); setFormError('') }}>Tạo hệ thống Local</button>
        <button className={systemSource === 'dcim' ? 'active' : ''} onClick={() => { setSystemSource('dcim'); setFormError('') }}>Tìm từ DCIM</button>
      </div>
      {systemSource === 'local' ? (
        <div className="form-grid">
          <label className="field full">
            <span>System ID (Mã định danh duy nhất) *</span>
            <input value={localId} onChange={(e) => {
              const val = e.target.value.toLowerCase().replace(/[^a-z0-9-_]/g, '-')
              setLocalId(val)
            }} placeholder="e.g. fintech-platform, payment-core, crm-hub" />
            <small>Chữ thường, số, dấu gạch nối (e.g. fintech-core)</small>
          </label>
          <label className="field full">
            <span>Đơn vị / Khối quản lý</span>
            <input value={localUnit} onChange={(e) => setLocalUnit(e.target.value)} placeholder="Khối Công nghệ số" />
          </label>
          <label className="field full">
            <span>Mô tả hệ thống</span>
            <textarea value={localDescription} onChange={(e) => setLocalDescription(e.target.value)} placeholder="Mô tả chức năng và mục đích của hệ thống..." />
          </label>
        </div>
      ) : (
        <>
          <label className="field"><span>DCIM service</span><div className="search-action"><input value={dcimQuery} onChange={(event) => { setDcimQuery(event.target.value); setResults([]); setSelected(null); setFormError('') }} onKeyDown={(event) => { if (event.key === 'Enter' && dcimQuery.trim().length >= 2) searchDcim() }} placeholder="Search by service name or code" /><button className="secondary-button" disabled={dcimQuery.trim().length < 2 || searching} onClick={searchDcim}><Search size={15} />{searching ? 'Searching…' : 'Search'}</button></div></label>
          {results.map((service) => <button className={`dcim-result ${selected?.id === service.id ? 'selected' : ''}`} onClick={() => { setSelected(service); setFormError('') }} key={service.id}><span className="result-icon"><Layers3 size={18} /></span><span><strong>{service.name}</strong><small>{service.code} · {service.tenant}</small></span><em>{service.tier}</em></button>)}
          {selected && <div className="form-grid"><label className="field full"><span>Display name</span><input value={selected.name} readOnly /></label><label className="field"><span>Service code</span><input value={selected.code} readOnly /></label><label className="field"><span>Tenant / Unit</span><input value={selected.tenant} readOnly /></label><label className="field full"><span>Description</span><textarea value={selected.description} readOnly /></label></div>}
        </>
      )}
      {formError && <div className="inline-error" role="alert"><CircleAlert size={15} />{formError}</div>}
    </Modal>}
  </>
}

export function ServersPage() {
  const { notify } = usePortalFeedback()
  const [items, setItems] = useState<PortalServer[]>([])
  const [query, setQuery] = useState('')
  const [environment, setEnvironment] = useState('All environments')
  const [status, setStatus] = useState('All statuses')
  const [details, setDetails] = useState<PortalServer | null>(null)
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [syncing, setSyncing] = useState(false)
  const [lastSync, setLastSync] = useState('Not synced')
  const [pageSize, setPageSize] = useState(10)
  const [page, setPage] = useState(1)
  const [maintenanceMap, setMaintenanceMap] = useState<Record<string, boolean>>({})
  const [waivers, setWaivers] = useState<SecurityWaiver[]>([])
  const [waiverModal, setWaiverModal] = useState(false)
  const [newCve, setNewCve] = useState('')
  const [newModule, setNewModule] = useState('')
  const [newReason, setNewReason] = useState('')
  const [newDays, setNewDays] = useState('14')
  const [savingWaiver, setSavingWaiver] = useState(false)
  const [selectedTelemetry, setSelectedTelemetry] = useState<ServerTelemetry | null>(null)
  const [loadingTelemetry, setLoadingTelemetry] = useState(false)
  const [agentMap, setAgentMap] = useState<Record<string, AgentStatusItem>>({})
  const [agentCmd, setAgentCmd] = useState('uname -a')
  const [agentRunning, setAgentRunning] = useState(false)
  const [agentOutput, setAgentOutput] = useState<AgentExecuteResponse | null>(null)

  const openServerDetails = (server: PortalServer) => {
    setDetails(server)
    setSelectedTelemetry(null)
    setAgentOutput(null)
    setLoadingTelemetry(true)
    getServerTelemetry(server.id)
      .then((telem) => setSelectedTelemetry(telem))
      .catch(() => undefined)
      .finally(() => setLoadingTelemetry(false))
  }

  useEffect(() => {
    let active = true
    Promise.all([
      listServerInventory(),
      listServersMaintenance().catch(() => []),
      listSecurityWaivers().catch(() => []),
      getAgentStatus().catch(() => ({ connectedAgents: 0, agents: [] })),
    ])
      .then(([result, maintList, wList, agentRes]) => {
        if (active) {
          setItems(result.map(serverFromApi))
          setLastSync('API · vừa xong')
          if (maintList && Array.isArray(maintList)) {
            const map: Record<string, boolean> = {}
            for (const s of maintList) {
              map[s.serverName] = s.inMaintenance
            }
            setMaintenanceMap(map)
          }
          if (wList && Array.isArray(wList)) {
            setWaivers(wList)
          }
          if (agentRes && Array.isArray(agentRes.agents)) {
            const amap: Record<string, AgentStatusItem> = {}
            for (const a of agentRes.agents) {
              amap[a.hostname] = a
            }
            setAgentMap(amap)
          }
        }
      })
      .catch((error) => {
        if (active) notify(error instanceof Error ? error.message : 'Không thể tải inventory từ netCI API.', 'error')
      })
    return () => { active = false }
  }, [])

  const filtered = useMemo(() => items.filter((item) => `${item.id} ${item.systemId} ${item.ip} ${item.usedBy.map((u) => `${u.moduleId} ${u.environment}`).join(' ')}`.toLowerCase().includes(query.toLowerCase()) && (environment === 'All environments' || item.usedBy.some((u) => (u.environment === 'prod' ? 'Production' : u.environment === 'staging' ? 'Staging' : 'Dev') === environment)) && (status === 'All statuses' || item.status === status)), [items, query, environment, status])
  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize))
  const visible = filtered.slice((page - 1) * pageSize, page * pageSize)
  useEffect(() => setPage((current) => Math.min(current, totalPages)), [totalPages])

  const syncServers = async () => {
    setSyncing(true)
    try {
      const [result, maintList, wList] = await Promise.all([
        listServerInventory(),
        listServersMaintenance().catch(() => []),
        listSecurityWaivers().catch(() => []),
      ])
      setItems(result.map(serverFromApi))
      setSelectedIds([])
      setLastSync('API · vừa xong')
      if (maintList && Array.isArray(maintList)) {
        const map: Record<string, boolean> = {}
        for (const s of maintList) {
          map[s.serverName] = s.inMaintenance
        }
        setMaintenanceMap(map)
      }
      setWaivers(wList)
      notify('Đã đồng bộ các runtime target, trạng thái bảo trì và danh sách miễn trừ bảo mật.')
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Không thể đồng bộ inventory.', 'error')
    } finally {
      setSyncing(false)
    }
  }

  const toggleAll = () => setSelectedIds(visible.length > 0 && visible.every((item) => selectedIds.includes(item.id)) ? selectedIds.filter((id) => !visible.some((item) => item.id === id)) : [...new Set([...selectedIds, ...visible.map((item) => item.id)])])

  const handleCreateWaiver = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!newCve.trim() || !newReason.trim()) {
      notify('Vui lòng điền mã CVE và lý do miễn trừ.', 'error')
      return
    }
    setSavingWaiver(true)
    try {
      const expiry = new Date()
      expiry.setDate(expiry.getDate() + (parseInt(newDays, 10) || 14))
      const created = await createSecurityWaiver({
        cveId: newCve.trim().toUpperCase(),
        moduleId: newModule.trim() || undefined,
        reason: newReason.trim(),
        expiresAt: expiry.toISOString(),
      })
      setWaivers((prev) => [created, ...prev])
      notify(`Đã tạo miễn trừ bảo mật cho ${created.cveId}`)
      setWaiverModal(false)
      setNewCve('')
      setNewModule('')
      setNewReason('')
    } catch (err) {
      notify(err instanceof Error ? err.message : 'Không thể tạo miễn trừ bảo mật.', 'error')
    } finally {
      setSavingWaiver(false)
    }
  }

  return <>
    <PageHeader title="Deployment targets & Inventory" description="Quản lý máy chủ triển khai, trạng thái bảo trì và đăng ký miễn trừ bảo mật VEX." action={<button className="secondary-button" disabled={syncing} onClick={syncServers}><CloudDownload size={16} />{syncing ? 'Refreshing…' : 'Refresh from API'}</button>} />
    <div className="sync-note"><CheckCircle2 size={15} />Last refresh: {lastSync} · {items.length} configured targets · {waivers.length} active VEX waivers</div>
    
    <section className="panel table-panel">
      <div className="table-toolbar server-filters">
        <label className="input-with-icon"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Tìm hostname, IP, hệ thống…" /></label>
        <select value={environment} onChange={(event) => setEnvironment(event.target.value)}><option>All environments</option><option>Dev</option><option>Staging</option><option>Production</option></select>
        <select value={status} onChange={(event) => setStatus(event.target.value)}><option>All statuses</option><option>Unknown</option><option>Online</option><option>Maintenance</option><option>Offline</option></select>
      </div>
      <div className="data-table servers-table">
        <div className="table-row table-head">
          <span><input type="checkbox" aria-label="Chọn tất cả trên trang" checked={visible.length > 0 && visible.every((item) => selectedIds.includes(item.id))} onChange={toggleAll} /></span>
          <span>Server</span>
          <span>Dùng bởi</span>
          <span>IP (NetBox)</span>
          <span>Agent · telemetry</span>
          <span>Status</span>
          <span>Chế độ bảo trì</span>
          <span />
        </div>
        {visible.map((server) => {
          const isMaint = maintenanceMap[server.id] ?? (server.status === 'Maintenance')
          return (
            <div className="table-row" key={server.id}>
              <span><input type="checkbox" aria-label={`Chọn ${server.id}`} checked={selectedIds.includes(server.id)} onChange={() => setSelectedIds((current) => current.includes(server.id) ? current.filter((id) => id !== server.id) : [...current, server.id])} /></span>
              <span className="strong-cell">
                <Server size={16} />{server.id}
                {agentMap[server.id] && (
                  <span className="mono" style={{ fontSize: '10px', color: '#10b981', background: 'rgba(16, 185, 129, 0.1)', padding: '1px 5px', borderRadius: 4, marginLeft: 6, display: 'inline-flex', alignItems: 'center', gap: 2 }}>
                    <Zap size={10} /> Edge Agent
                  </span>
                )}
              </span>
              <span className="used-by">{server.usedBy.map((u) => <em key={`${u.moduleId}-${u.environment}`} className={`env-badge env-${u.environment}`} title={`${u.systemId} / ${u.moduleId}`}>{u.moduleId} · {u.environment}</em>)}</span>
              <span className="mono" title={server.dcim?.message ?? ''}>{server.ip || <i className="muted">{server.dcim && server.dcim.status !== 'not_found' && server.dcim.status !== 'error' ? 'trong NetBox, chưa gán IP' : server.dcim?.status === 'error' ? 'NetBox không trả lời' : 'không có trong NetBox'}</i>}</span>
              <span className="mono" title={server.agent ? `agent qua ${server.agent.replicaId}, thấy lần cuối ${server.agent.lastSeenAt}` : 'chưa có edge agent trên host này'}>{server.agent ? (server.agent.stale ? 'agent mất kết nối' : 'agent online') : '—'}{server.telemetry ? ` · cpu ${server.telemetry.cpuPercent}% · mem ${server.telemetry.memPercent}% · disk ${server.telemetry.diskPercent}%` : ''}</span>
              <span className="status-stack"><StatusPill status={isMaint ? 'Maintenance' : server.status} />{server.dcim && !server.dcim.valid && !isMaint && <small className="gate-note" title={server.dcim.message}>gate: {server.dcim.status.replace(/_/g, ' ')}</small>}</span>
              <span>
                <button
                  type="button"
                  className={isMaint ? 'secondary-button' : 'primary-button'}
                  style={{ fontSize: '11px', padding: '3px 8px', height: '26px' }}
                  onClick={async () => {
                    try {
                      await toggleServerMaintenance(server.id, !isMaint, isMaint ? 'Exit maintenance' : 'Scheduled maintenance')
                      setMaintenanceMap((prev) => ({ ...prev, [server.id]: !isMaint }))
                      setItems((prev) => prev.map((s) => s.id === server.id ? { ...s, status: !isMaint ? 'Maintenance' : 'Online' } : s))
                      notify(`Đã cập nhật chế độ bảo trì cho ${server.id}`)
                    } catch {
                      notify('Không thể cập nhật trạng thái bảo trì.', 'error')
                    }
                  }}
                >
                  {isMaint ? 'Bỏ bảo trì' : 'Bảo trì'}
                </button>
              </span>
              <span className="row-actions"><button aria-label={`Chi tiết ${server.id}`} onClick={() => openServerDetails(server)}><MoreHorizontal size={16} /></button></span>
            </div>
          )
        })}
      </div>
      {!filtered.length && <div className="empty-table"><Server size={22} /><strong>No configured targets</strong><span>Targets appear after a module is connected to real deployment infrastructure.</span></div>}
      <div className="pagination">
        <span>Showing {filtered.length ? (page - 1) * pageSize + 1 : 0}–{Math.min(page * pageSize, filtered.length)} of {filtered.length}</span>
        <div>
          <select aria-label="Số dòng mỗi trang" value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(1) }}><option value={10}>10 / page</option><option value={20}>20 / page</option></select>
          <button disabled={page <= 1} aria-label="Trang trước" onClick={() => setPage((current) => Math.max(1, current - 1))}><ChevronLeft size={16} /></button>
          <button className="page-active" disabled aria-current="page">{page}</button>
          <button disabled={page >= totalPages} aria-label="Trang sau" onClick={() => setPage((current) => Math.min(totalPages, current + 1))}><ChevronRight size={16} /></button>
        </div>
      </div>
    </section>

    {/* VEX Security Waivers Section */}
    <section className="panel table-panel" style={{ marginTop: '24px' }}>
      <div className="panel-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '14px 20px', borderBottom: '1px solid var(--border-subtle, #e2e8f0)' }}>
        <div>
          <h3 style={{ margin: 0, fontSize: '15px', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <ShieldAlert size={18} color="#0284c7" />
            VEX Security Waivers (Miễn trừ lỗ hổng bảo mật)
          </h3>
          <p style={{ margin: '4px 0 0 0', fontSize: '12px', color: 'var(--text-muted, #64748b)' }}>
            Miễn trừ có thời hạn cho các CVE Critical đã được đánh giá an toàn hoặc có biện pháp kiểm soát bù đắp (Compensating Controls).
          </p>
        </div>
        <button className="primary-button" onClick={() => setWaiverModal(true)}>
          <Plus size={16} /> Tạo miễn trừ CVE
        </button>
      </div>
      <div className="data-table waivers-table">
        <div className="table-row table-head">
          <span>CVE Identifier</span>
          <span>Module</span>
          <span>Lý do miễn trừ / Compensating Control</span>
          <span>Người phê duyệt</span>
          <span>Hết hạn</span>
          <span>Thao tác</span>
        </div>
        {waivers.map((w) => (
          <div className="table-row" key={w.id}>
            <span className="strong-cell mono" style={{ color: '#e11d48' }}>{w.cveId}</span>
            <span>{w.moduleId || 'Tất cả module'}</span>
            <span>{w.reason}</span>
            <span>{w.approvedBy}</span>
            <span>{new Date(w.expiresAt).toLocaleDateString('vi-VN')}</span>
            <span>
              {w.status === 'active' ? (
                <button
                  type="button"
                  className="secondary-button"
                  style={{ fontSize: '11px', padding: '2px 8px', color: '#e11d48' }}
                  onClick={async () => {
                    try {
                      await revokeSecurityWaiver(w.id)
                      setWaivers((prev) => prev.filter((item) => item.id !== w.id))
                      notify(`Đã thu hồi miễn trừ cho ${w.cveId}`)
                    } catch {
                      notify('Không thể thu hồi miễn trừ', 'error')
                    }
                  }}
                >
                  Thu hồi
                </button>
              ) : (
                <span className="muted">Đã thu hồi</span>
              )}
            </span>
          </div>
        ))}
        {!waivers.length && (
          <div className="empty-table" style={{ padding: '20px' }}>
            <ShieldCheck size={24} color="#16a34a" />
            <strong>Không có miễn trừ nào đang kích hoạt</strong>
            <span>Mọi lỗ hổng Critical phát hiện qua Trivy scan sẽ kích hoạt hard gate bảo vệ môi trường Production.</span>
          </div>
        )}
      </div>
    </section>

    {waiverModal && (
      <Modal
        title="Tạo miễn trừ bảo mật VEX"
        description="Cho phép pipeline tiếp tục triển khai khi gặp CVE đã có kế hoạch xử lý hoặc nằm ngoài attack surface."
        onClose={() => setWaiverModal(false)}
        footer={
          <>
            <button className="secondary-button" onClick={() => setWaiverModal(false)}>Hủy</button>
            <button className="primary-button" disabled={savingWaiver} onClick={handleCreateWaiver}>
              {savingWaiver ? 'Đang lưu…' : 'Xác nhận miễn trừ'}
            </button>
          </>
        }
      >
        <form onSubmit={handleCreateWaiver} className="form-grid">
          <label className="field full">
            <span>Mã CVE Identifier *</span>
            <input value={newCve} onChange={(e) => setNewCve(e.target.value)} placeholder="e.g. CVE-2026-9999" required />
          </label>
          <label className="field full">
            <span>Module áp dụng (Bỏ trống để áp dụng toàn hệ thống)</span>
            <input value={newModule} onChange={(e) => setNewModule(e.target.value)} placeholder="e.g. billing-api" />
          </label>
          <label className="field full">
            <span>Thời hạn miễn trừ (Số ngày)</span>
            <select value={newDays} onChange={(e) => setNewDays(e.target.value)}>
              <option value="7">7 ngày</option>
              <option value="14">14 ngày (Mặc định)</option>
              <option value="30">30 ngày</option>
              <option value="60">60 ngày</option>
            </select>
          </label>
          <label className="field full">
            <span>Lý do / Compensating Control *</span>
            <textarea
              value={newReason}
              onChange={(e) => setNewReason(e.target.value)}
              placeholder="Mô tả phân tích VEX: Component không nhận untrusted input từ Internet, đã có WAF rule chặn payload, bản vá upstream sẽ được phát hành trong sprint tới..."
              rows={3}
              required
            />
          </label>
        </form>
      </Modal>
    )}

    {details && (
      <Modal
        title={`Server: ${details.id}`}
        description="Chi tiết máy chủ triển khai và quan sát tài nguyên thời gian thực qua Outbound Runner Agent."
        onClose={() => setDetails(null)}
        footer={<button className="primary-button" onClick={() => setDetails(null)}>Close</button>}
      >
        <div className="request-summary">
          <div><span>System</span><strong>{details.systemId}</strong></div>
          <div><span>IP address</span><strong className="mono">{details.ip || '—'}</strong></div>
          <div><span>Status</span><StatusPill status={details.status} /></div>
        </div>
        <div className="form-grid" style={{ marginBottom: 14 }}>
          <label className="field"><span>Environment</span><input readOnly value={details.environment} /></label>
          <label className="field"><span>Agent Connection</span><input readOnly value="WebSocket wss:// (Outbound Daemon)" /></label>
        </div>

        {/* Live Host Telemetry Panel */}
        <div style={{ padding: '14px 16px', background: 'rgba(255, 255, 255, 0.03)', borderRadius: 8, border: '1px solid var(--border-subtle, #e2e8f0)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
            <h4 style={{ margin: 0, fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}>
              <Activity size={16} color="#0284c7" />
              Pre-flight Telemetry (Host Metrics)
            </h4>
            {selectedTelemetry && (
              <span style={{
                fontSize: 11, fontWeight: 600, padding: '2px 8px', borderRadius: 10,
                background: selectedTelemetry.status === 'critical' ? '#ffe4e6' : '#dcfce7',
                color: selectedTelemetry.status === 'critical' ? '#e11d48' : '#16a34a',
              }}>
                {selectedTelemetry.status.toUpperCase()}
              </span>
            )}
          </div>

          {loadingTelemetry ? (
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>Đang tải telemetry từ agent…</div>
          ) : selectedTelemetry ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 4 }}>
                  <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}><Cpu size={14} /> CPU Usage</span>
                  <strong className="mono">{selectedTelemetry.cpuPercent.toFixed(1)}%</strong>
                </div>
                <div style={{ height: 6, background: '#334155', borderRadius: 3, overflow: 'hidden' }}>
                  <div style={{ width: `${Math.min(selectedTelemetry.cpuPercent, 100)}%`, height: '100%', background: selectedTelemetry.cpuPercent > 95 ? '#e11d48' : '#0284c7', transition: 'width 0.3s ease' }} />
                </div>
              </div>

              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 4 }}>
                  <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}><Activity size={14} /> Memory Usage</span>
                  <strong className="mono">{selectedTelemetry.memPercent.toFixed(1)}%</strong>
                </div>
                <div style={{ height: 6, background: '#334155', borderRadius: 3, overflow: 'hidden' }}>
                  <div style={{ width: `${Math.min(selectedTelemetry.memPercent, 100)}%`, height: '100%', background: '#3b82f6', transition: 'width 0.3s ease' }} />
                </div>
              </div>

              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 4 }}>
                  <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}><HardDrive size={14} /> Disk Usage</span>
                  <strong className="mono" style={{ color: selectedTelemetry.diskPercent > 90 ? '#e11d48' : 'inherit' }}>
                    {selectedTelemetry.diskPercent.toFixed(1)}%
                  </strong>
                </div>
                <div style={{ height: 6, background: '#334155', borderRadius: 3, overflow: 'hidden' }}>
                  <div style={{ width: `${Math.min(selectedTelemetry.diskPercent, 100)}%`, height: '100%', background: selectedTelemetry.diskPercent > 90 ? '#e11d48' : '#10b981', transition: 'width 0.3s ease' }} />
                </div>
                {selectedTelemetry.diskPercent > 90 && (
                  <small style={{ color: '#e11d48', marginTop: 4, display: 'block' }}>
                    Cảnh báo: Ổ cứng trên 90%, pre-flight gate sẽ chặn deploy tự động.
                  </small>
                )}
              </div>
            </div>
          ) : (
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>Chưa có telemetry được ghi nhận.</div>
          )}
        </div>

        {/* Outbound Runner Agent Panel */}
        <div style={{ marginTop: 14, padding: '14px 16px', background: 'rgba(16, 185, 129, 0.04)', borderRadius: 8, border: '1px solid rgba(16, 185, 129, 0.2)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
            <h4 style={{ margin: 0, fontSize: 13, display: 'flex', alignItems: 'center', gap: 6, color: '#10b981' }}>
              <Zap size={16} /> Outbound Edge Runner Agent
            </h4>
            <span style={{ fontSize: 11, fontWeight: 600, padding: '2px 8px', borderRadius: 10, background: agentMap[details.id] ? '#dcfce7' : 'rgba(255,255,255,0.08)', color: agentMap[details.id] ? '#16a34a' : 'var(--text-muted)' }}>
              {agentMap[details.id] ? 'AGENT CONNECTED (WS)' : 'AGENT STANDBY / LOCAL'}
            </span>
          </div>
          <p style={{ margin: '0 0 10px 0', fontSize: 12, color: 'var(--text-secondary)' }}>
            Kết nối Outbound WebSocket đến <code>/api/v1/agents/ws</code>. Không mở port firewall inbound (Zero Inbound Port).
          </p>

          <div style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
            <input
              value={agentCmd}
              id="agent-command-input"
              onChange={(e) => setAgentCmd(e.target.value)}
              placeholder="e.g. uname -a, df -h, docker ps"
              style={{ flex: 1, fontFamily: 'monospace', fontSize: 12 }}
            />
            <button
              type="button"
              id="btn-run-agent-cmd"
              className="primary-button"
              disabled={agentRunning}
              onClick={async () => {
                setAgentRunning(true)
                try {
                  const res = await executeAgentCommand({
                    hostname: details.id,
                    command: agentCmd.trim() || 'uname -a',
                  })
                  setAgentOutput(res)
                  notify(`Lệnh hoàn tất trên ${details.id} (exit code ${res.exitCode}) trong ${res.durationMs}ms`)
                } catch (err) {
                  notify(err instanceof Error ? err.message : 'Không thể gửi lệnh qua Edge Runner Agent', 'error')
                } finally {
                  setAgentRunning(false)
                }
              }}
              style={{ fontSize: 12, padding: '4px 12px' }}
            >
              {agentRunning ? 'Running…' : 'Dispatch via Agent'}
            </button>
          </div>

          {agentOutput && (
            <div id="agent-command-output" style={{ background: '#0f172a', padding: 10, borderRadius: 6, border: '1px solid #334155' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: '#94a3b8', marginBottom: 6 }}>
                <span>Command: <code style={{ color: '#38bdf8' }}>{agentOutput.command}</code></span>
                <span>Exit Code: <strong style={{ color: agentOutput.exitCode === 0 ? '#4ade80' : '#f87171' }}>{agentOutput.exitCode}</strong> ({agentOutput.durationMs}ms)</span>
              </div>
              <pre style={{ margin: 0, fontSize: 11, fontFamily: 'monospace', color: '#f1f5f9', whiteSpace: 'pre-wrap', maxHeight: 150, overflowY: 'auto' }}>
                {agentOutput.stdout || agentOutput.stderr || '(No output)'}
              </pre>
            </div>
          )}
        </div>
      </Modal>
    )}
  </>
}

export function SystemPage({ systemId, navigate }: { systemId: string; navigate: Navigate }) {
  const { notify } = usePortalFeedback()
  const fallback: PortalSystemView = { id: systemId, code: '', unit: 'Loading', description: 'Loading system data from netCI.', owner: '', status: 'unknown', modules: [], runs: 0, succeeded: 0, failed: 0 }
  const [system, setSystem] = useState<PortalSystemView>(fallback)
  const [deleteModal, setDeleteModal] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [doraMetrics, setDoraMetrics] = useState<DoraCardMetric[]>([])

  useEffect(() => {
    setSystem(fallback)
    getSystem(systemId).then((item) => setSystem({
      id: item.id,
      code: '',
      unit: item.unit,
      description: item.description,
      owner: item.owner,
      status: (['unknown', 'healthy', 'degraded', 'critical'].includes(item.status) ? item.status : 'unknown') as PortalSystemView['status'],
      modules: item.modules.map((module) => ({ id: module.id, name: module.name, type: module.type, description: module.description, versions: module.versions, runtime: module.runtime, environments: module.environments })),
      runs: item.pipelineRuns,
      succeeded: Math.max(item.pipelineRuns - item.failedRuns, 0),
      failed: item.failedRuns,
    })).catch(() => undefined)
  }, [systemId])

  useEffect(() => {
    getDora('systems', systemId).then((result) => setDoraMetrics(result.metrics.map((metric) => ({
      key: metric.key,
      label: metric.label,
      value: String(metric.value),
      unit: metric.unit,
      hint: metric.hint,
    })))).catch(() => setDoraMetrics([]))
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
    <div className="system-heading"><div><div className="title-status"><h1>{system.id}</h1><StatusPill status={system.status[0].toUpperCase() + system.status.slice(1)} /></div><p>{system.description}</p><small>Đơn vị: {system.unit} · Tạo bởi: <span title={system.owner}>{/^[0-9a-f]{8}-[0-9a-f]{4}-/.test(system.owner) ? `${system.owner.slice(0, 8)}…` : system.owner}</span></small></div><div className="heading-actions"><button className="danger-button" onClick={() => setDeleteModal(true)}><Trash2 size={16} />Delete System</button><button className="primary-button" onClick={() => navigate('new-module', { systemId })}><Plus size={16} />New Module</button></div></div>
    <div className="dora-section-label">DORA METRICS · LAST 30 DAYS</div>
    <DoraCards metrics={doraMetrics} />
    <section className="modules-section"><div className="section-heading"><h2>{system.modules.length} modules</h2><span>Live API projection</span></div><div className="module-grid">{system.modules.map((module) => <article className="module-card" key={module.id}><div className="module-card-top"><span className={`module-icon ${module.type === 'Frontend' ? 'blue' : 'purple'}`}><Box size={19} /></span><div><h3>{module.name}</h3><p>{module.description}</p></div><em className={`type-badge ${module.type === 'Frontend' ? 'blue' : 'purple'}`}>{module.type}</em></div><label>Versions</label><div className="version-chips">{module.versions.length ? module.versions.map((version) => <span key={version}>{version}</span>) : <span>None</span>}</div><label>Environments</label><div className="environment-grid">{module.environments.map((environment) => <div key={environment.name}><span>{environment.name === 'prod' ? 'Production' : environment.name === 'staging' ? 'Staging' : 'Dev'}</span><StatusPill status={environment.status.replace('_', ' ')} /></div>)}</div><button className="module-view-button" onClick={() => navigate('module', { systemId, moduleId: module.id })}>View Module <ArrowRight size={15} /></button></article>)}{!system.modules.length && <div className="empty-module-state"><Box size={27} /><strong>No modules yet</strong><span>Add a DCIM module to configure its delivery lifecycle.</span><button className="primary-button" onClick={() => navigate('new-module', { systemId })}><Plus size={15} />Add module</button></div>}</div></section>
    {deleteModal && <Modal title="Delete System" description={`Are you sure you want to delete system "${system.id}"?`} onClose={() => setDeleteModal(false)} footer={<><button className="secondary-button" onClick={() => setDeleteModal(false)}>Cancel</button><button className="danger-button" disabled={deleting} onClick={handleDeleteSystem}>{deleting ? 'Deleting…' : 'Confirm Delete'}</button></>}><div className="inline-error" role="status">Warning: This action will delete system {system.id} and detach all associated modules from the Release Portal.</div></Modal>}
  </>
}
