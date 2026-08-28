import { useEffect, useMemo, useState } from 'react'
import {
  CalendarClock, Check, CheckCircle2, Circle, CircleAlert, Clock3, Eye,
  FileCheck2, Pencil, Plus, RefreshCw, RotateCcw, Search, ShieldCheck, Trash2, XCircle,
} from 'lucide-react'
import {
  approveProductionRequest, createProductionRequest, listProductionRequests,
  getSystem, rejectProductionRequest, type ProductionRequest, type ProductionRequestCreate,
} from './api/netciClient'
import { Modal, PageHeader, StatusPill } from './PortalShell'
import { usePortalFeedback } from './PortalFeedback'
import { modules as seedModules, type PortalModule as PortalModuleView } from './portalData'

const statusLabels: Record<string, string> = {
  waiting_approval: 'Pending checks',
  approved: 'Approved',
  rejected: 'Rejected',
  blocked: 'Blocked',
}

function statusLabel(status: string): string {
  return statusLabels[status] ?? status.replace(/_/g, ' ')
}

function displayRequestId(request: ProductionRequest): string {
  if (request.id.toUpperCase().startsWith('PR-')) return request.id.toUpperCase()
  return `PR-${new Date(request.scheduledFor).getFullYear()}-${request.id.replace(/-/g, '').slice(0, 8).toUpperCase()}`
}

function scheduledDate(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('vi-VN', { dateStyle: 'short', timeStyle: 'short', timeZone: 'Asia/Ho_Chi_Minh' }).format(date)
}

function localScheduleDefault(): string {
  const date = new Date(Date.now() + 24 * 60 * 60 * 1000)
  date.setMinutes(0, 0, 0)
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}T${String(date.getHours()).padStart(2, '0')}:00`
}

function toOffsetIso(localValue: string): string {
  return `${localValue}:00+07:00`
}

function RequestDetails({ request, busy, onClose, onApprove, onReject }: { request: ProductionRequest; busy: boolean; onClose: () => void; onApprove: () => void; onReject: () => void }) {
  const pending = request.status === 'waiting_approval'
  const failed = request.status === 'rejected' || request.status === 'blocked'
  const steps = [
    { label: 'Create production request', detail: `${request.modules.length} module · ${request.rollbackStrategy} rollback`, state: 'done' },
    { label: 'Check approval status', detail: pending ? 'Waiting for mentor / GNOC approval' : request.status === 'approved' ? 'Approved for promotion' : `Request ${request.status}`, state: pending ? 'running' : failed ? 'failed' : 'done' },
    { label: 'Check active alarms', detail: pending ? 'Runs after approval' : failed ? 'Skipped' : 'Ready for NOCPro5 adapter', state: pending || failed ? 'waiting' : 'done' },
    { label: 'Execute CD Production', detail: 'Runs on Ubuntu after runtime adapters and security evidence are ready', state: 'waiting' },
    { label: 'Health check and rollback', detail: request.rollbackStrategy === 'automatic' ? 'Automatic rollback on failed health check' : 'Wait for operator decision', state: 'waiting' },
  ]
  return <Modal wide title={displayRequestId(request)} description={`${request.modules.map((item) => item.moduleName).join(' · ')} · Scheduled ${scheduledDate(request.scheduledFor)}`} onClose={onClose} footer={<>{pending && <><button className="danger-button" disabled={busy} onClick={onReject}><XCircle size={16} />Reject</button><button className="primary-button" disabled={busy} onClick={onApprove}><ShieldCheck size={16} />Approve</button></>}<button className="secondary-button" onClick={onClose}>Close</button></>}>
    <div className="request-summary"><div><span>Requested by</span><strong>{request.requestedBy}</strong></div><div><span>Automation</span><strong>{request.runAutomationTests ? 'Required' : 'Disabled'}</strong></div><div><span>Status</span><StatusPill status={statusLabel(request.status)} /></div></div>
    <div className="request-module-pills">{request.modules.map((item) => <span key={item.moduleId}>{item.moduleName} · {item.version} · order {item.deploymentOrder}</span>)}</div>
    {failed && <div className="failure-alert"><CircleAlert size={18} /><div><strong>Request cannot proceed</strong><p>{request.comment ?? 'The approval or policy gate blocked this production request.'}</p></div></div>}
    <div className="request-timeline">{steps.map((step, index) => <div className={`timeline-step step-${step.state}`} key={step.label}><div className="timeline-rail"><span>{step.state === 'done' ? <Check size={15} /> : step.state === 'failed' ? <XCircle size={16} /> : step.state === 'running' ? <Clock3 size={15} /> : <Circle size={12} />}</span>{index < steps.length - 1 && <i />}</div><div><strong>{step.label}</strong><p>{step.detail}</p></div><em>{step.state === 'done' ? 'Done' : step.state === 'failed' ? 'Failed' : step.state === 'running' ? 'In progress' : 'Pending'}</em></div>)}</div>
  </Modal>
}

type DraftModule = { moduleId: string; version: string; deploymentOrder: number }

function NewRequest({ availableModules, onClose, onCreate }: { availableModules: PortalModuleView[]; onClose: () => void; onCreate: (payload: ProductionRequestCreate) => Promise<void> }) {
  const firstVersionedModule = availableModules.find((module) => module.versions.length > 0)
  const [selected, setSelected] = useState<string[]>(firstVersionedModule ? [firstVersionedModule.id] : [])
  const [drafts, setDrafts] = useState<Record<string, DraftModule>>(() => Object.fromEntries(availableModules.map((module, index) => [module.id, { moduleId: module.id, version: module.versions[0] ?? '', deploymentOrder: index + 1 }])))
  const [scheduledFor, setScheduledFor] = useState(localScheduleDefault)
  const [review, setReview] = useState(false)
  const [rollback, setRollback] = useState<'automatic' | 'manual'>('automatic')
  const [automation, setAutomation] = useState(true)
  const [automationSuites, setAutomationSuites] = useState(['netAT Smoke Tests', 'API Regression Suite'])
  const [automationTimeout, setAutomationTimeout] = useState('30 minutes')
  const [minimumPassRate, setMinimumPassRate] = useState(95)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const toggle = (id: string) => setSelected((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id])
  const submit = async () => {
    setSaving(true)
    setError('')
    try {
      await onCreate({ modules: selected.map((id) => drafts[id]), requestedBy: 'Admin', scheduledFor: toOffsetIso(scheduledFor), rollbackStrategy: rollback, runAutomationTests: automation })
      onClose()
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : 'Không thể tạo production request.')
    } finally {
      setSaving(false)
    }
  }
  return <Modal wide title="New Production Request" description="Configure a controlled multi-module production deployment." onClose={onClose} footer={<><button className="secondary-button" disabled={saving} onClick={review ? () => setReview(false) : onClose}>{review ? 'Back' : 'Cancel'}</button>{review ? <button className="primary-button" disabled={saving} onClick={submit}><FileCheck2 size={16} />{saving ? 'Creating…' : 'Create Request'}</button> : <button className="primary-button" disabled={!selected.length || !scheduledFor || (automation && !automationSuites.length)} onClick={() => setReview(true)}>Review Request</button>}</>}>
    {!review ? <div className="request-form">
      <section className="form-section"><div className="form-section-title"><span>1</span><div><h3>Select modules</h3><p>Choose one or more modules to deploy together.</p></div></div><div className="selectable-modules">{availableModules.map((module) => <button className={selected.includes(module.id) ? 'selected' : ''} disabled={!module.versions.length} title={!module.versions.length ? 'Register a version before creating a production request' : undefined} onClick={() => toggle(module.id)} key={module.id}><span className="check-box">{selected.includes(module.id) && <Check size={13} />}</span><span><strong>{module.name}</strong><small>{module.versions.length} available versions</small></span></button>)}</div></section>
      {selected.length > 0 && <section className="form-section"><div className="form-section-title"><span>2</span><div><h3>Versions and deployment order</h3><p>Modules with the same order run in parallel.</p></div></div><div className="deployment-order">{selected.map((id) => { const module = availableModules.find((item) => item.id === id)!; const draft = drafts[id]; return <article key={id}><div><strong>{module.name}</strong><small>Registered immutable versions</small></div><select value={draft.version} onChange={(event) => setDrafts({ ...drafts, [id]: { ...draft, version: event.target.value } })}>{module.versions.map((version) => <option key={version}>{version}</option>)}</select><label><span>Order</span><input type="number" min="1" max="100" value={draft.deploymentOrder} onChange={(event) => setDrafts({ ...drafts, [id]: { ...draft, deploymentOrder: Math.max(1, Number(event.target.value)) } })} /></label></article> })}</div></section>}
      <section className="form-section form-grid"><div className="form-section-title full"><span>3</span><div><h3>Schedule</h3><p>Production window in Asia/Saigon timezone.</p></div></div><label className="field full"><span>Deployment date and time</span><input type="datetime-local" value={scheduledFor} onChange={(event) => setScheduledFor(event.target.value)} /></label></section>
      <section className="form-section"><div className="form-section-title"><span>4</span><div><h3>Rollback strategy</h3><p>Choose how the portal responds to failed health checks.</p></div></div><div className="option-cards"><button className={rollback === 'automatic' ? 'selected' : ''} onClick={() => setRollback('automatic')}><RotateCcw size={18} /><strong>Automatic</strong><small>Rollback when health check fails</small></button><button className={rollback === 'manual' ? 'selected' : ''} onClick={() => setRollback('manual')}><Pencil size={18} /><strong>Manual</strong><small>Wait for operator decision</small></button></div></section>
      <section className="form-section"><div className="toggle-row"><div><strong>Run automation tests</strong><p>Enforce the test gate before approval.</p></div><button type="button" role="switch" aria-checked={automation} className={`switch ${automation ? 'on' : ''}`} onClick={() => setAutomation(!automation)}><i /></button></div>{automation && <div className="automation-settings">{['netAT Smoke Tests', 'API Regression Suite'].map((suite) => <label key={suite}><input type="checkbox" checked={automationSuites.includes(suite)} onChange={() => setAutomationSuites((current) => current.includes(suite) ? current.filter((item) => item !== suite) : [...current, suite])} /> {suite}</label>)}<label className="field"><span>Timeout</span><select value={automationTimeout} onChange={(event) => setAutomationTimeout(event.target.value)}><option>30 minutes</option><option>60 minutes</option></select></label><label className="field"><span>Minimum pass rate</span><div className="suffix-input"><input type="number" value={minimumPassRate} min="1" max="100" onChange={(event) => setMinimumPassRate(Math.min(100, Math.max(1, Number(event.target.value))))} /><span>%</span></div></label></div>}</section>
    </div> : <div className="review-request"><div className="review-banner"><CheckCircle2 size={20} /><div><strong>Request is ready to create</strong><p>Approval and alarm checks run before the Ubuntu production adapter.</p></div></div><div className="review-grid"><div><span>Modules</span><strong>{selected.map((id) => availableModules.find((item) => item.id === id)?.name).join(', ')}</strong></div><div><span>Schedule</span><strong>{scheduledDate(toOffsetIso(scheduledFor))}</strong></div><div><span>Rollback</span><strong>{rollback === 'automatic' ? 'Automatic' : 'Manual'}</strong></div><div><span>Automation tests</span><strong>{automation ? `${automationSuites.length} suites · ${minimumPassRate}% · ${automationTimeout}` : 'Disabled'}</strong></div></div><div className="approval-flow"><span><FileCheck2 size={17} />Create request</span><i /><span><CheckCircle2 size={17} />Approval</span><i /><span><CircleAlert size={17} />Alarm check</span><i /><span><CalendarClock size={17} />Deploy</span></div>{error && <div className="inline-error" role="alert"><CircleAlert size={15} />{error}</div>}</div>}
  </Modal>
}

export function ProductionRequestsPage({ systemId }: { systemId: string }) {
  const { notify } = usePortalFeedback()
  const [items, setItems] = useState<ProductionRequest[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [query, setQuery] = useState('')
  const [moduleFilter, setModuleFilter] = useState('All modules')
  const [statusFilter, setStatusFilter] = useState('All statuses')
  const [fromDate, setFromDate] = useState('')
  const [toDate, setToDate] = useState('')
  const [details, setDetails] = useState<ProductionRequest | null>(null)
  const [creating, setCreating] = useState(false)
  const [commandBusy, setCommandBusy] = useState(false)
  const [availableModules, setAvailableModules] = useState<PortalModuleView[]>(seedModules)
  const load = async () => {
    setLoading(true)
    setLoadError('')
    try { setItems(await listProductionRequests()) } catch (error) { setLoadError(error instanceof Error ? error.message : 'Không thể tải production requests.') } finally { setLoading(false) }
  }
  useEffect(() => { void load() }, [])
  useEffect(() => {
    let active = true
    getSystem(systemId).then((system) => { if (active) setAvailableModules(system.modules.map((module) => ({ id: module.id, name: module.name, type: module.type, description: module.description, versions: module.versions, runtime: module.runtime }))) }).catch(() => undefined)
    return () => { active = false }
  }, [systemId])
  const filtered = useMemo(() => items.filter((request) => {
    const date = request.scheduledFor.slice(0, 10)
    return displayRequestId(request).toLowerCase().includes(query.toLowerCase())
      && (moduleFilter === 'All modules' || request.modules.some((item) => item.moduleName === moduleFilter))
      && (statusFilter === 'All statuses' || request.status === statusFilter)
      && (!fromDate || date >= fromDate)
      && (!toDate || date <= toDate)
  }), [items, query, moduleFilter, statusFilter, fromDate, toDate])
  const create = async (payload: ProductionRequestCreate) => {
    const request = await createProductionRequest(payload)
    setItems((current) => [request, ...current])
    notify(`${displayRequestId(request)} đã được tạo và đang chờ approval.`)
  }
  const updateStatus = async (action: 'approve' | 'reject') => {
    if (!details) return
    setCommandBusy(true)
    try {
      const updated = action === 'approve'
        ? await approveProductionRequest(details.id, { actor: 'Admin', comment: 'Approved from Release Portal' })
        : await rejectProductionRequest(details.id, { actor: 'Admin', comment: 'Rejected from Release Portal' })
      setItems((current) => current.map((item) => item.id === updated.id ? updated : item))
      setDetails(updated)
      notify(`${displayRequestId(updated)} đã được ${action === 'approve' ? 'approve' : 'reject'}.`)
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Không thể cập nhật request.', 'error')
    } finally {
      setCommandBusy(false)
    }
  }
  const immutableNotice = (request: ProductionRequest, action: string) => notify(`${displayRequestId(request)} đã submit và là immutable. ${action} cần command cancel/recreate riêng.`, 'info')
  return <>
    <PageHeader title="Production Requests" description="Create and track requests to deploy modules to production." action={<button className="primary-button" onClick={() => setCreating(true)}><Plus size={16} />New Request</button>} />
    <div className="request-kpis">{[['Total Requests', String(items.length), 'neutral'], ['Approved', String(items.filter((item) => item.status === 'approved').length), 'green'], ['Blocked / Rejected', String(items.filter((item) => ['blocked', 'rejected'].includes(item.status)).length), 'red'], ['Pending', String(items.filter((item) => item.status === 'waiting_approval').length), 'amber']].map(([label, value, tone]) => <article key={label}><i className={`request-kpi-dot ${tone}`} /><div><span>{label}</span><strong>{value}</strong></div></article>)}</div>
    <section className="panel table-panel"><div className="table-toolbar request-filters"><label className="input-with-icon"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search request ID…" /></label><select value={moduleFilter} onChange={(event) => setModuleFilter(event.target.value)}><option>All modules</option>{availableModules.map((item) => <option key={item.id}>{item.name}</option>)}</select><select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="All statuses">All statuses</option><option value="approved">Approved</option><option value="rejected">Rejected</option><option value="blocked">Blocked</option><option value="waiting_approval">Pending checks</option></select><label className="date-filter"><span>From</span><input type="date" value={fromDate} onChange={(event) => setFromDate(event.target.value)} /></label><label className="date-filter"><span>To</span><input type="date" value={toDate} min={fromDate || undefined} onChange={(event) => setToDate(event.target.value)} /></label><button className="text-button" onClick={() => { setQuery(''); setModuleFilter('All modules'); setStatusFilter('All statuses'); setFromDate(''); setToDate('') }}>Clear filters</button></div>
      {loadError && <div className="load-state error-state" role="alert"><CircleAlert size={20} /><strong>Không tải được approval queue</strong><span>{loadError}</span><button className="secondary-button" onClick={load}><RefreshCw size={15} />Retry</button></div>}
      {!loadError && <div className="data-table requests-table"><div className="table-row table-head"><span>Request</span><span>Modules</span><span>Requested by</span><span>Scheduled</span><span>Rollback</span><span>Status</span><span>Actions</span></div>{filtered.map((request) => <div className="table-row" key={request.id}><span className="request-id">{displayRequestId(request)}</span><span className="module-list-cell">{request.modules.map((item) => <small key={item.moduleId}>{item.moduleName}</small>)}</span><span>{request.requestedBy}</span><span>{scheduledDate(request.scheduledFor)}</span><span>{request.rollbackStrategy}</span><StatusPill status={statusLabel(request.status)} /><span className="row-actions"><button aria-label={`Xem ${displayRequestId(request)}`} onClick={() => setDetails(request)}><Eye size={16} /></button>{request.status === 'waiting_approval' && <><button aria-label={`Sửa ${displayRequestId(request)}`} onClick={() => immutableNotice(request, 'Editing')}><Pencil size={15} /></button><button aria-label={`Xóa ${displayRequestId(request)}`} onClick={() => immutableNotice(request, 'Deleting')}><Trash2 size={15} /></button></>}</span></div>)}</div>}
      {!loadError && !loading && !filtered.length && <div className="empty-table"><Search size={22} /><strong>Không có request phù hợp</strong><span>Thử đổi bộ lọc hoặc tạo production request mới.</span></div>}
      {loading && <div className="load-state"><RefreshCw className="spin" size={20} /><strong>Loading production requests…</strong></div>}
    </section>
    {details && <RequestDetails request={details} busy={commandBusy} onClose={() => setDetails(null)} onApprove={() => updateStatus('approve')} onReject={() => updateStatus('reject')} />}
    {creating && <NewRequest availableModules={availableModules} onClose={() => setCreating(false)} onCreate={create} />}
  </>
}
