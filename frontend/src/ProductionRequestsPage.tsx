import { useMemo, useState } from 'react'
import {
  CalendarClock, Check, CheckCircle2, ChevronDown, Circle, CircleAlert, Clock3,
  Eye, FileCheck2, Pencil, Plus, RotateCcw, Search, Trash2, XCircle,
} from 'lucide-react'
import { Modal, PageHeader, StatusPill } from './PortalShell'
import { modules, productionRequests } from './portalData'

type RequestItem = (typeof productionRequests)[number]

function RequestDetails({ request, onClose }: { request: RequestItem; onClose: () => void }) {
  const failed = request.status === 'Rolled back'
  const pending = request.status === 'Pending checks'
  const steps = [
    { label: 'Create SR / CR', detail: `${request.sr} · ${request.cr}`, state: 'done' },
    { label: 'Check approval status', detail: pending ? 'Waiting for GNOC approval' : 'Approved by GNOC', state: pending ? 'running' : 'done' },
    { label: 'Check active alarms', detail: pending ? 'NOCPro5 check queued' : 'No active critical alarms', state: pending ? 'waiting' : 'done' },
    { label: 'Execute CD Production', detail: failed ? 'Health check failed after 3 attempts' : pending ? 'Waiting for checks' : 'Production pipeline completed', state: failed ? 'failed' : pending ? 'waiting' : 'done' },
    { label: failed ? 'Rollback modules' : 'Deploy modules', detail: failed ? 'Backend API restored to v2.3.7' : pending ? 'Not started' : 'All modules are healthy', state: failed || !pending ? 'done' : 'waiting' },
    { label: 'Close SR / CR', detail: failed ? 'Closed as failed deployment' : pending ? 'Not started' : 'SR and CR closed successfully', state: pending ? 'waiting' : 'done' },
  ]
  return <Modal wide title={request.id} description={`${request.modules.join(' · ')} · Scheduled ${request.scheduled}`} onClose={onClose} footer={<><span className="detail-last-sync">Last sync: 28/04/2025 09:14</span><button className="primary-button" onClick={onClose}>Close</button></>}>
    <div className="request-summary"><div><span>Requested by</span><strong>{request.requestedBy}</strong></div><div><span>SR / CR</span><strong>{request.sr} · {request.cr}</strong></div><div><span>Status</span><StatusPill status={request.status} /></div></div>
    <div className="request-module-pills">{request.modules.map((item) => <span key={item}>{item}</span>)}</div>
    {failed && <div className="failure-alert"><CircleAlert size={18} /><div><strong>Deployment failed and rolled back</strong><p>Health check returned HTTP 503. Automatic rollback restored the last healthy version.</p></div></div>}
    <div className="request-timeline">{steps.map((step, index) => <div className={`timeline-step step-${step.state}`} key={step.label}><div className="timeline-rail"><span>{step.state === 'done' ? <Check size={15} /> : step.state === 'failed' ? <XCircle size={16} /> : step.state === 'running' ? <Clock3 size={15} /> : <Circle size={12} />}</span>{index < steps.length - 1 && <i />}</div><div><strong>{step.label}</strong><p>{step.detail}</p></div><em>{step.state === 'done' ? 'Done' : step.state === 'failed' ? 'Failed' : step.state === 'running' ? 'In progress' : 'Pending'}</em></div>)}</div>
  </Modal>
}

function NewRequest({ onClose }: { onClose: () => void }) {
  const [selected, setSelected] = useState<string[]>(['backend-api'])
  const [review, setReview] = useState(false)
  const [rollback, setRollback] = useState<'automatic' | 'manual'>('automatic')
  const [automation, setAutomation] = useState(true)
  const toggle = (id: string) => setSelected((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id])
  return <Modal wide title="New Production Request" description="Configure a controlled multi-module production deployment." onClose={onClose} footer={<><button className="secondary-button" onClick={onClose}>Cancel</button>{review ? <button className="primary-button" onClick={onClose}><FileCheck2 size={16} />Create Request</button> : <button className="primary-button" disabled={!selected.length} onClick={() => setReview(true)}>Review Request</button>}</>}>
    {!review ? <div className="request-form">
      <section className="form-section"><div className="form-section-title"><span>1</span><div><h3>Select modules</h3><p>Choose one or more modules to deploy together.</p></div></div><div className="selectable-modules">{modules.map((module) => <button className={selected.includes(module.id) ? 'selected' : ''} onClick={() => toggle(module.id)} key={module.id}><span className="check-box">{selected.includes(module.id) && <Check size={13} />}</span><span><strong>{module.name}</strong><small>{module.versions.length} available versions</small></span></button>)}</div></section>
      {selected.length > 0 && <section className="form-section"><div className="form-section-title"><span>2</span><div><h3>Versions and deployment order</h3><p>Modules with the same order run in parallel.</p></div></div><div className="deployment-order">{selected.map((id, index) => { const module = modules.find((item) => item.id === id)!; return <article key={id}><div><strong>{module.name}</strong><small>Security passed · Coverage {id === 'backend-api' ? '87%' : '91%'}</small></div><select defaultValue={module.versions[0]}>{module.versions.map((version) => <option key={version}>{version}</option>)}</select><label><span>Order</span><input type="number" min="1" defaultValue={index + 1} /></label></article> })}</div></section>}
      <section className="form-section form-grid"><div className="form-section-title full"><span>3</span><div><h3>Schedule</h3><p>Production window in Asia/Saigon timezone.</p></div></div><label className="field full"><span>Deployment date and time</span><input type="datetime-local" defaultValue="2025-04-30T03:00" /></label></section>
      <section className="form-section"><div className="form-section-title"><span>4</span><div><h3>Rollback strategy</h3><p>Choose how the portal responds to failed health checks.</p></div></div><div className="option-cards"><button className={rollback === 'automatic' ? 'selected' : ''} onClick={() => setRollback('automatic')}><RotateCcw size={18} /><strong>Automatic</strong><small>Rollback when health check fails</small></button><button className={rollback === 'manual' ? 'selected' : ''} onClick={() => setRollback('manual')}><Pencil size={18} /><strong>Manual</strong><small>Wait for operator decision</small></button></div></section>
      <section className="form-section"><div className="toggle-row"><div><strong>Run automation tests</strong><p>Select test pipelines and enforce a minimum pass rate.</p></div><button className={`switch ${automation ? 'on' : ''}`} onClick={() => setAutomation(!automation)}><i /></button></div>{automation && <div className="automation-settings"><label><input type="checkbox" defaultChecked /> netAT Smoke Tests</label><label><input type="checkbox" defaultChecked /> API Regression Suite</label><label className="field"><span>Timeout</span><select><option>30 minutes</option><option>60 minutes</option></select></label><label className="field"><span>Minimum pass rate</span><div className="suffix-input"><input type="number" defaultValue="95" /><span>%</span></div></label></div>}</section>
    </div> : <div className="review-request"><div className="review-banner"><CheckCircle2 size={20} /><div><strong>Request is ready to create</strong><p>GNOC approval and NOCPro5 alarm checks will run before production deployment.</p></div></div><div className="review-grid"><div><span>Modules</span><strong>{selected.map((id) => modules.find((item) => item.id === id)?.name).join(', ')}</strong></div><div><span>Schedule</span><strong>30/04/2025 03:00</strong></div><div><span>Rollback</span><strong>{rollback === 'automatic' ? 'Automatic' : 'Manual'}</strong></div><div><span>Automation tests</span><strong>{automation ? 'Required · 95% pass rate' : 'Disabled'}</strong></div></div><div className="approval-flow"><span><FileCheck2 size={17} />Create SR / CR</span><i /><span><CheckCircle2 size={17} />GNOC approval</span><i /><span><CircleAlert size={17} />Alarm check</span><i /><span><CalendarClock size={17} />Deploy</span></div></div>}
  </Modal>
}

export function ProductionRequestsPage() {
  const [query, setQuery] = useState('')
  const [moduleFilter, setModuleFilter] = useState('All modules')
  const [statusFilter, setStatusFilter] = useState('All statuses')
  const [details, setDetails] = useState<RequestItem | null>(null)
  const [creating, setCreating] = useState(false)
  const filtered = useMemo(() => productionRequests.filter((request) => request.id.toLowerCase().includes(query.toLowerCase()) && (moduleFilter === 'All modules' || request.modules.some((item) => item.startsWith(moduleFilter))) && (statusFilter === 'All statuses' || request.status === statusFilter)), [query, moduleFilter, statusFilter])
  return <>
    <PageHeader title="Production Requests" description="Create and track requests to deploy netChat modules to production." action={<button className="primary-button" onClick={() => setCreating(true)}><Plus size={16} />New Request</button>} />
    <div className="request-kpis">{[['Total Deploys', '3', 'neutral'], ['Success', '1', 'green'], ['Failed', '1', 'red'], ['Pending', '1', 'amber']].map(([label, value, tone]) => <article key={label}><i className={`request-kpi-dot ${tone}`} /><div><span>{label}</span><strong>{value}</strong></div></article>)}</div>
    <section className="panel table-panel"><div className="table-toolbar request-filters"><label className="input-with-icon"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search request ID…" /></label><select value={moduleFilter} onChange={(event) => setModuleFilter(event.target.value)}><option>All modules</option><option>Backend API</option><option>Web Client</option></select><select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option>All statuses</option><option>Success</option><option>Rolled back</option><option>Pending checks</option></select><label className="date-filter"><span>From</span><input type="date" /></label><label className="date-filter"><span>To</span><input type="date" /></label><button className="text-button">Clear filters</button></div>
      <div className="data-table requests-table"><div className="table-row table-head"><span>Request</span><span>Modules</span><span>Requested by</span><span>Scheduled</span><span>SR / CR</span><span>Status</span><span>Actions</span></div>{filtered.map((request) => <div className="table-row" key={request.id}><span className="request-id">{request.id}</span><span className="module-list-cell">{request.modules.map((module) => <small key={module}>{module.split(' · ')[0]}</small>)}</span><span>{request.requestedBy}</span><span>{request.scheduled}</span><span>{request.sr}<small> · {request.cr}</small></span><StatusPill status={request.status} /><span className="row-actions"><button aria-label={`Xem ${request.id}`} onClick={() => setDetails(request)}><Eye size={16} /></button>{request.status === 'Pending checks' && <><button aria-label={`Sửa ${request.id}`}><Pencil size={15} /></button><button aria-label={`Xóa ${request.id}`}><Trash2 size={15} /></button></>}</span></div>)}</div>
    </section>
    {details && <RequestDetails request={details} onClose={() => setDetails(null)} />}
    {creating && <NewRequest onClose={() => setCreating(false)} />}
  </>
}
