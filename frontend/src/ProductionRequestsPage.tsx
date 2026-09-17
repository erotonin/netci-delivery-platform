import { useEffect, useMemo, useState } from 'react'
import {
  CalendarClock, Check, CheckCircle2, Circle, CircleAlert, Clock3, Eye,
  FileCheck2, Pencil, Plus, RefreshCw, RotateCcw, Search, ShieldCheck, XCircle,
  GitFork, Split, TrendingUp, Layers, Activity,
} from 'lucide-react'
import {
  approveProductionRequest, createProductionRequest, listProductionRequests, listSystems,
  getSystem, rejectProductionRequest, getProductionRequestPlan, advanceCanary, abortCanary,
  getDeploymentTraffic, cancelDeployment, type ProductionRequest, type ProductionRequestCreate,
} from './api/netciClient'
import { Modal, PageHeader, StatusPill } from './PortalShell'
import { usePortalFeedback } from './PortalFeedback'
import type { PortalModuleView } from './portalTypes'

const statusLabels: Record<string, string> = {
  waiting_approval: 'Pending checks',
  approved: 'Approved',
  rejected: 'Rejected',
  blocked: 'Blocked',
  succeeded: 'Succeeded',
  cancelled: 'Cancelled',
}

function statusLabel(status: string): string {
  return statusLabels[status] ?? status.replace(/_/g, ' ')
}

function displayRequestId(request: any): string {
  if (!request) return 'PR-UNKNOWN'
  const id = request.id || request.requestId
  if (!id || typeof id !== 'string') return 'PR-UNKNOWN'
  if (id.toUpperCase().startsWith('PR-')) return id.toUpperCase()
  const year = request.scheduledFor ? new Date(request.scheduledFor).getFullYear() : new Date().getFullYear()
  return `PR-${year}-${id.replace(/-/g, '').slice(0, 8).toUpperCase()}`
}

function scheduledDate(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('vi-VN', { dateStyle: 'short', timeStyle: 'short', timeZone: 'Asia/Ho_Chi_Minh' }).format(date)
}

// Default: now. A window a day away used to be the default, and an approved request
// then sat "deploying" for 24 hours while the workflow waited for it, with nothing on
// screen saying so. A window is something the requester chooses on purpose.
function localScheduleDefault(): string {
  const date = new Date(Date.now() + 60 * 1000)
  date.setSeconds(0, 0)
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}T${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

function toOffsetIso(localValue: string): string {
  return `${localValue}:00+07:00`
}

function RequestDetails({
  request,
  busy,
  onClose,
  onApprove,
  onReject,
  onRefresh,
}: {
  request: ProductionRequest
  busy: boolean
  onClose: () => void
  onApprove: () => void
  onReject: () => void
  onRefresh?: () => void
}) {
  const { notify } = usePortalFeedback()
  const [currentReq, setCurrentReq] = useState<ProductionRequest>(request)
  const [canaryBusy, setCanaryBusy] = useState(false)
  const [trafficInfo, setTrafficInfo] = useState<{ trafficWeight: number; canaryStep: number } | null>(null)

  useEffect(() => {
    let active = true
    getProductionRequestPlan(request.id)
      .then((plan) => {
        if (active) setCurrentReq((prev) => ({ ...prev, ...plan, id: (plan as any).id || (plan as any).requestId || prev.id }))
      })
      .catch(() => {})

    if (request.deploymentId && (request.strategy === 'canary' || request.strategy === 'blue_green')) {
      getDeploymentTraffic(request.deploymentId)
        .then((traffic) => {
          if (active) setTrafficInfo({ trafficWeight: traffic.trafficWeight, canaryStep: traffic.canaryStep })
        })
        .catch(() => {})
    }
    return () => {
      active = false
    }
  }, [request.id, request.deploymentId, request.strategy])

  const handleAdvanceCanary = async () => {
    setCanaryBusy(true)
    try {
      const res = await advanceCanary(request.id)
      notify(`Canary advanced to step ${res.step} (${res.trafficWeight}% traffic).`)
      setTrafficInfo({ trafficWeight: res.trafficWeight, canaryStep: res.step })
      const updated = await getProductionRequestPlan(request.id)
      setCurrentReq((prev) => ({ ...prev, ...updated, id: (updated as any).id || (updated as any).requestId || prev.id }))
      onRefresh?.()
    } catch (err) {
      notify(err instanceof Error ? err.message : 'Failed to advance canary', 'error')
    } finally {
      setCanaryBusy(false)
    }
  }

  const handleAbortCanary = async () => {
    setCanaryBusy(true)
    try {
      await abortCanary(request.id, 'Aborted from Release Portal')
      notify('Canary aborted. Traffic rolled back.')
      const updated = await getProductionRequestPlan(request.id)
      setCurrentReq((prev) => ({ ...prev, ...updated, id: (updated as any).id || (updated as any).requestId || prev.id }))
      onRefresh?.()
    } catch (err) {
      notify(err instanceof Error ? err.message : 'Failed to abort canary', 'error')
    } finally {
      setCanaryBusy(false)
    }
  }

  const pending = currentReq.status === 'waiting_approval'
  const failed = currentReq.status === 'rejected' || currentReq.status === 'blocked'
  const isApproved = currentReq.status === 'approved'
  const isCanary = currentReq.strategy === 'canary'

  const steps = [
    {
      label: 'Create production request',
      detail: `${currentReq.modules.length} module(s) · strategy: ${currentReq.strategy || 'rolling'} · ${currentReq.rollbackStrategy} rollback`,
      state: 'done',
    },
    {
      label: 'Check approval status',
      detail: pending
        ? 'Chờ một reviewer khác phê duyệt (separation of duties)'
        : currentReq.status === 'approved'
        ? 'Approved for promotion'
        : `Request ${currentReq.status}`,
      state: pending ? 'running' : failed ? 'failed' : 'done',
    },
    {
      label: 'Execute CD Production',
      detail: currentReq.deploymentId
        ? (isApproved && new Date(currentReq.scheduledFor).getTime() > Date.now()
          ? `Deployment ${currentReq.deploymentId.slice(0, 8)} đã được duyệt; worker chờ tới cửa sổ triển khai ${new Date(currentReq.scheduledFor).toLocaleString('vi-VN')} rồi mới chạy`
          : `Deployment ${currentReq.deploymentId.slice(0, 8)}`)
        : 'No deployment has been created',
      state: currentReq.deploymentId
        ? currentReq.status === 'blocked' || currentReq.status === 'cancelled'
          ? 'failed'
          : currentReq.status === 'succeeded'
          ? 'done'
          : 'running'
        : 'waiting',
    },
    {
      label: 'Health check and rollback',
      detail:
        currentReq.comment ??
        (currentReq.rollbackStrategy === 'automatic'
          ? 'Automatic rollback on failed health check'
          : 'Wait for operator decision'),
      state:
        currentReq.status === 'succeeded'
          ? 'done'
          : currentReq.status === 'blocked'
          ? 'failed'
          : 'waiting',
    },
  ]

  return (
    <Modal
      wide
      title={displayRequestId(currentReq)}
      description={`${currentReq.modules.map((item) => item.moduleName).join(' · ')} · Scheduled ${scheduledDate(currentReq.scheduledFor)}`}
      onClose={onClose}
      footer={
        <>
          {pending && (
            <>
              <button className="danger-button" disabled={busy} onClick={onReject}>
                <XCircle size={16} />Reject
              </button>
              <button className="primary-button" disabled={busy} onClick={onApprove}>
                <ShieldCheck size={16} />Approve
              </button>
            </>
          )}
          {isApproved && currentReq.deploymentId && !['succeeded', 'cancelled'].includes(currentReq.status) && (
            <button className="danger-button" disabled={busy || canaryBusy} onClick={async () => {
              const reason = window.prompt('Lý do huỷ deployment (ghi vào audit):', '') ?? ''
              setCanaryBusy(true)
              try {
                await cancelDeployment(currentReq.deploymentId!, reason)
                notify('Đã huỷ deployment; workflow đã được dừng.')
                onRefresh?.()
                onClose()
              } catch (error) {
                notify(error instanceof Error ? error.message : 'Không huỷ được deployment.', 'error')
              } finally { setCanaryBusy(false) }
            }}>
              <XCircle size={16} />Huỷ deployment
            </button>
          )}
          <button className="secondary-button" onClick={onClose}>
            Close
          </button>
        </>
      }
    >
      <div className="request-summary">
        <div>
          <span>Requested by</span>
          <strong>{currentReq.requestedBy}</strong>
        </div>
        <div>
          <span>Strategy</span>
          <span className={`strategy-tag ${currentReq.strategy || 'rolling'}`}>
            {currentReq.strategy || 'rolling'}
          </span>
        </div>
        <div>
          <span>Status</span>
          <StatusPill status={statusLabel(currentReq.status)} />
        </div>
      </div>

      <div className="request-module-pills">
        {currentReq.modules.map((item) => (
          <span key={item.moduleId}>
            {item.moduleName} · {item.version}
            {item.dependencies && item.dependencies.length > 0
              ? ` (deps: ${item.dependencies.length})`
              : ''}
            {item.status ? ` [${item.status}]` : ''}
          </span>
        ))}
      </div>

      {/* L7 Traffic Steering Rules */}
      {isCanary && (
        <div className="canary-rules-panel" style={{ margin: '10px 0', padding: '10px 14px', background: 'rgba(2, 132, 199, 0.08)', borderRadius: 8, border: '1px solid rgba(2, 132, 199, 0.2)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
            <Split size={15} color="#0284c7" />
            <strong style={{ fontSize: 12, color: '#0284c7' }}>L7 Traffic Steering & Routing Policy</strong>
          </div>
          <div style={{ fontSize: 12, display: 'flex', gap: 16, flexWrap: 'wrap' }}>
            {currentReq.canaryRules?.header_name || (currentReq.strategyConfig?.canary_rules as any)?.header_name ? (
              <span>HTTP Header: <code className="mono" style={{ color: '#38bdf8' }}>{currentReq.canaryRules?.header_name || (currentReq.strategyConfig?.canary_rules as any)?.header_name}: {currentReq.canaryRules?.header_value || (currentReq.strategyConfig?.canary_rules as any)?.header_value || 'true'}</code></span>
            ) : null}
            {currentReq.canaryRules?.cookie || (currentReq.strategyConfig?.canary_rules as any)?.cookie ? (
              <span>Session Cookie: <code className="mono" style={{ color: '#38bdf8' }}>{currentReq.canaryRules?.cookie || (currentReq.strategyConfig?.canary_rules as any)?.cookie}</code></span>
            ) : null}
            {!currentReq.canaryRules?.header_name && !(currentReq.strategyConfig?.canary_rules as any)?.header_name && !currentReq.canaryRules?.cookie && !(currentReq.strategyConfig?.canary_rules as any)?.cookie ? (
              <span className="muted">Standard progressive percentage split (Step 1: 10%)</span>
            ) : null}
          </div>
        </div>
      )}

      {/* Canary Traffic Controls */}
      {isCanary && isApproved && (
        <div className="canary-control-panel">
          <div>
            <Activity size={18} style={{ display: 'inline', verticalAlign: 'middle', marginRight: 6 }} />
            <strong>Canary Traffic Allocation</strong>
            <span className="traffic-pill">{trafficInfo ? `${trafficInfo.trafficWeight}%` : '10%'}</span>
            <small style={{ marginLeft: 8, color: 'var(--muted)' }}>
              (Step {trafficInfo ? trafficInfo.canaryStep : 1})
            </small>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              className="primary-button"
              style={{ padding: '4px 10px', fontSize: 11 }}
              disabled={canaryBusy || (trafficInfo ? trafficInfo.trafficWeight >= 100 : false)}
              onClick={handleAdvanceCanary}
            >
              <TrendingUp size={14} />Advance Step
            </button>
            <button
              className="danger-button"
              style={{ padding: '4px 10px', fontSize: 11 }}
              disabled={canaryBusy}
              onClick={handleAbortCanary}
            >
              <XCircle size={14} />Abort Canary
            </button>
          </div>
        </div>
      )}

      {/* DAG Release Plan Waves */}
      {currentReq.releasePlan && currentReq.releasePlan.waves && currentReq.releasePlan.waves.length > 0 && (
        <div className="release-plan-container">
          <h4>
            <Layers size={14} style={{ display: 'inline', verticalAlign: 'middle', marginRight: 5 }} />
            DAG Release Plan ({currentReq.releasePlan.totalWaves} Waves)
          </h4>
          <div className="wave-list">
            {currentReq.releasePlan.waves.map((wave) => (
              <div key={wave.wave} className="wave-card">
                <div className="wave-header">Wave {wave.wave}</div>
                <div className="wave-modules">
                  {wave.moduleIds.map((modId) => {
                    const mod = currentReq.modules.find((m) => m.moduleId === modId)
                    return (
                      <div key={modId} className="wave-module-item">
                        <strong>{mod?.moduleName ?? modId}</strong>
                        <span>{mod?.version}</span>
                        <StatusPill status={statusLabel(mod?.status ?? 'pending')} />
                      </div>
                    )
                  })}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {failed && (
        <div className="failure-alert">
          <CircleAlert size={18} />
          <div>
            <strong>Request cannot proceed</strong>
            <p>{currentReq.comment ?? 'The approval or policy gate blocked this production request.'}</p>
          </div>
        </div>
      )}

      {/* What netCI checks at approval, as facts, not as a score it did not compute */}
      <div className="governance-risk-card" style={{ marginTop: 12, marginBottom: 12, padding: '12px', background: 'rgba(255,255,255,0.03)', borderRadius: 8, border: '1px solid rgba(255,255,255,0.08)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
          <h4 style={{ margin: 0, display: 'flex', alignItems: 'center', gap: 6, fontSize: 13 }}>
            <ShieldCheck size={16} />
            Kiểm tra khi phê duyệt
          </h4>
          {currentReq.policyDecision ? <span className="risk-badge" style={{ padding: '2px 8px', borderRadius: 12, fontSize: 11, fontWeight: 600, background: currentReq.policyDecision.allowed ? '#14532d' : '#7f1d1d', color: currentReq.policyDecision.allowed ? '#86efac' : '#fca5a5' }}>
            policy: {currentReq.policyDecision.allowed ? 'allow' : 'deny'} · risk {currentReq.policyDecision.riskScore}/100
          </span> : <span className="risk-badge" style={{ padding: '2px 8px', borderRadius: 12, fontSize: 11, color: '#94a3b8', border: '1px solid rgba(148,163,184,.4)' }}>chính sách chạy lúc phê duyệt</span>}
        </div>
        <div style={{ fontSize: 12, color: '#94a3b8', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
          <div><strong>Phê duyệt:</strong> người khác người yêu cầu ({currentReq.requestedBy})</div>
          <div><strong>Bằng chứng tự động hoá:</strong> {currentReq.runAutomationTests ? 'bắt buộc autoTest: passed' : 'không bắt buộc'}</div>
          <div><strong>Artifact:</strong> theo digest sha256 của lượt chạy đã ký/quét</div>
          <div><strong>Rollback:</strong> {currentReq.rollbackStrategy}</div>
        </div>
        {currentReq.policyDecision?.reason && (
          <div style={{ marginTop: 8, fontSize: 11, color: '#cbd5e1', borderTop: '1px solid rgba(255,255,255,0.06)', paddingTop: 6 }}>
            <strong>Gate Verdict:</strong> {currentReq.policyDecision.reason}
          </div>
        )}
      </div>

      <div className="request-timeline">
        {steps.map((step, index) => (
          <div className={`timeline-step step-${step.state}`} key={step.label}>
            <div className="timeline-rail">
              <span>
                {step.state === 'done' ? (
                  <Check size={15} />
                ) : step.state === 'failed' ? (
                  <XCircle size={16} />
                ) : step.state === 'running' ? (
                  <Clock3 size={15} />
                ) : (
                  <Circle size={12} />
                )}
              </span>
              {index < steps.length - 1 && <i />}
            </div>
            <div>
              <strong>{step.label}</strong>
              <p>{step.detail}</p>
            </div>
            <em>
              {step.state === 'done'
                ? 'Done'
                : step.state === 'failed'
                ? 'Failed'
                : step.state === 'running'
                ? 'In progress'
                : 'Pending'}
            </em>
          </div>
        ))}
      </div>
    </Modal>
  )
}

type DraftModule = {
  moduleId: string
  version: string
  deploymentOrder: number
  dependencies: string[]
}

function NewRequest({
  availableModules,
  onClose,
  onCreate,
}: {
  availableModules: PortalModuleView[]
  onClose: () => void
  onCreate: (payload: ProductionRequestCreate) => Promise<void>
}) {
  const firstVersionedModule = availableModules.find((module) => module.versions.length > 0)
  const [selected, setSelected] = useState<string[]>(firstVersionedModule ? [firstVersionedModule.id] : [])
  const [drafts, setDrafts] = useState<Record<string, DraftModule>>(() =>
    Object.fromEntries(
      availableModules.map((module, index) => [
        module.id,
        {
          moduleId: module.id,
          version: module.versions[0] ?? '',
          deploymentOrder: index + 1,
          dependencies: [],
        },
      ])
    )
  )
  const [scheduledFor, setScheduledFor] = useState(localScheduleDefault)
  const [review, setReview] = useState(false)
  const [strategy, setStrategy] = useState<'rolling' | 'canary' | 'blue_green'>('rolling')
  const [canaryHeaderName, setCanaryHeaderName] = useState('X-Beta-Tester')
  const [canaryHeaderValue, setCanaryHeaderValue] = useState('true')
  const [canaryCookie, setCanaryCookie] = useState('')
  const [rollback, setRollback] = useState<'automatic' | 'manual'>('automatic')
  const [automation, setAutomation] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const toggle = (id: string) =>
    setSelected((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id]
    )

  const toggleDependency = (moduleId: string, depId: string) => {
    setDrafts((prev) => {
      const cur = prev[moduleId]
      const deps = cur.dependencies.includes(depId)
        ? cur.dependencies.filter((d) => d !== depId)
        : [...cur.dependencies, depId]
      return { ...prev, [moduleId]: { ...cur, dependencies: deps } }
    })
  }

  const submit = async () => {
    setSaving(true)
    setError('')
    try {
      await onCreate({
        modules: selected.map((id) => ({
          moduleId: drafts[id].moduleId,
          version: drafts[id].version,
          deploymentOrder: drafts[id].deploymentOrder,
          dependencies: drafts[id].dependencies,
        })),
        scheduledFor: toOffsetIso(scheduledFor),
        rollbackStrategy: rollback,
        runAutomationTests: automation,
        strategy,
        strategyConfig: strategy === 'canary' ? {
          steps: [10, 25, 50, 100],
          canary_rules: {
            header_name: canaryHeaderName || undefined,
            header_value: canaryHeaderValue || undefined,
            cookie: canaryCookie || undefined,
          },
        } : {},
        canaryRules: strategy === 'canary' ? {
          header_name: canaryHeaderName || undefined,
          header_value: canaryHeaderValue || undefined,
          cookie: canaryCookie || undefined,
        } : undefined,
      })
      onClose()
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : 'Không thể tạo production request.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      wide
      title="New Production Request"
      description="Promote verified artifacts through an approval-bound DAG production deployment."
      onClose={onClose}
      footer={
        <>
          <button
            className="secondary-button"
            disabled={saving}
            onClick={review ? () => setReview(false) : onClose}
          >
            {review ? 'Back' : 'Cancel'}
          </button>
          {review ? (
            <button className="primary-button" disabled={saving} onClick={submit}>
              <FileCheck2 size={16} />
              {saving ? 'Creating…' : 'Create Request'}
            </button>
          ) : (
            <button
              className="primary-button"
              disabled={!selected.length || !scheduledFor}
              onClick={() => setReview(true)}
            >
              Review Request
            </button>
          )}
        </>
      }
    >
      {!review ? (
        <div className="request-form">
          <section className="form-section">
            <div className="form-section-title">
              <span>1</span>
              <div>
                <h3>Select modules</h3>
                <p>Select one or more modules. DAG wave coordination schedules safe sequential rollouts.</p>
              </div>
            </div>
            <div className="selectable-modules">
              {availableModules.map((module) => (
                <button
                  className={selected.includes(module.id) ? 'selected' : ''}
                  disabled={!module.versions.length}
                  title={
                    !module.versions.length
                      ? 'Register a verified version before creating a production request'
                      : undefined
                  }
                  onClick={() => toggle(module.id)}
                  key={module.id}
                >
                  <span className="check-box">{selected.includes(module.id) && <Check size={13} />}</span>
                  <span>
                    <strong>{module.name}</strong>
                    <small>{module.versions.length} registered versions</small>
                  </span>
                </button>
              ))}
            </div>
          </section>

          {selected.length > 0 && (
            <section className="form-section">
              <div className="form-section-title">
                <span>2</span>
                <div>
                  <h3>Versions & Dependencies</h3>
                  <p>Configure verified immutable version and optional DAG upstream dependencies.</p>
                </div>
              </div>
              <div className="deployment-order">
                {selected.map((id) => {
                  const module = availableModules.find((item) => item.id === id)!
                  const draft = drafts[id]
                  const otherSelected = selected.filter((otherId) => otherId !== id)
                  return (
                    <article key={id}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <div>
                          <strong>{module.name}</strong>
                          <small style={{ display: 'block' }}>Registered immutable versions</small>
                        </div>
                        <select
                          value={draft.version}
                          onChange={(event) =>
                            setDrafts({
                              ...drafts,
                              [id]: { ...draft, version: event.target.value },
                            })
                          }
                        >
                          {module.versions.map((version) => (
                            <option key={version}>{version}</option>
                          ))}
                        </select>
                      </div>

                      {otherSelected.length > 0 && (
                        <div className="dependency-selector">
                          <span>
                            <GitFork size={12} style={{ display: 'inline', marginRight: 4 }} />
                            Depends on (deploy before this module):
                          </span>
                          <div className="dependency-checkboxes">
                            {otherSelected.map((otherId) => {
                              const otherMod = availableModules.find((item) => item.id === otherId)
                              const isChecked = draft.dependencies.includes(otherId)
                              return (
                                <label key={otherId}>
                                  <input
                                    type="checkbox"
                                    checked={isChecked}
                                    onChange={() => toggleDependency(id, otherId)}
                                  />
                                  <span>{otherMod?.name ?? otherId}</span>
                                </label>
                              )
                            })}
                          </div>
                        </div>
                      )}
                    </article>
                  )
                })}
              </div>
            </section>
          )}

          <section className="form-section">
            <div className="form-section-title">
              <span>3</span>
              <div>
                <h3>Deployment strategy</h3>
                <p>Choose progressive delivery mechanism for this promotion.</p>
              </div>
            </div>
            <div className="option-cards">
              <button
                className={strategy === 'rolling' ? 'selected' : ''}
                onClick={() => setStrategy('rolling')}
              >
                <Layers size={18} />
                <strong>Rolling DAG</strong>
                <small>Deploy in topological waves</small>
              </button>
              <button
                className={strategy === 'canary' ? 'selected' : ''}
                onClick={() => setStrategy('canary')}
              >
                <TrendingUp size={18} />
                <strong>Canary Rollout</strong>
                <small>10% → 25% → 50% → 100%</small>
              </button>
              <button
                className={strategy === 'blue_green' ? 'selected' : ''}
                onClick={() => setStrategy('blue_green')}
              >
                <Split size={18} />
                <strong>Blue / Green</strong>
                <small>Instant cutover with zero downtime</small>
              </button>
            </div>
            {strategy === 'canary' && (
              <div
                className="canary-l7-box full"
                style={{
                  marginTop: '12px',
                  padding: '14px',
                  background: 'rgba(56, 189, 248, 0.06)',
                  border: '1px solid rgba(56, 189, 248, 0.25)',
                  borderRadius: '8px',
                }}
              >
                <strong style={{ display: 'block', fontSize: '13px', color: '#0284c7', marginBottom: '8px' }}>
                  L7 Traffic Steering & Header Routing (Zero-Downtime Canary)
                </strong>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px' }}>
                  <label className="field">
                    <span style={{ fontSize: '12px' }}>HTTP Header Name</span>
                    <input
                      value={canaryHeaderName}
                      onChange={(e) => setCanaryHeaderName(e.target.value)}
                      placeholder="e.g. X-Beta-Tester"
                    />
                  </label>
                  <label className="field">
                    <span style={{ fontSize: '12px' }}>HTTP Header Value</span>
                    <input
                      value={canaryHeaderValue}
                      onChange={(e) => setCanaryHeaderValue(e.target.value)}
                      placeholder="e.g. true"
                    />
                  </label>
                </div>
                <label className="field" style={{ marginTop: '8px' }}>
                  <span style={{ fontSize: '12px' }}>Session Cookie Rule (Optional)</span>
                  <input
                    value={canaryCookie}
                    onChange={(e) => setCanaryCookie(e.target.value)}
                    placeholder="e.g. beta_user=1"
                  />
                </label>
              </div>
            )}
          </section>

          <section className="form-section form-grid">
            <div className="form-section-title full">
              <span>4</span>
              <div>
                <h3>Schedule</h3>
                <p>Production window in Asia/Saigon timezone.</p>
              </div>
            </div>
            <label className="field full">
              <span>Deployment date and time</span>
              <input
                type="datetime-local"
                value={scheduledFor}
                onChange={(event) => setScheduledFor(event.target.value)}
              />
              <small>{new Date(toOffsetIso(scheduledFor)).getTime() > Date.now() + 5 * 60 * 1000 ? `Sau khi được phê duyệt, worker sẽ chờ tới ${new Date(toOffsetIso(scheduledFor)).toLocaleString('vi-VN')} mới triển khai.` : 'Triển khai ngay sau khi được phê duyệt.'}</small>
            </label>
          </section>

          <section className="form-section">
            <div className="form-section-title">
              <span>5</span>
              <div>
                <h3>Rollback strategy</h3>
                <p>Choose how the portal responds to failed health checks.</p>
              </div>
            </div>
            <div className="option-cards">
              <button
                className={rollback === 'automatic' ? 'selected' : ''}
                onClick={() => setRollback('automatic')}
              >
                <RotateCcw size={18} />
                <strong>Automatic SAGA</strong>
                <small>Compensate and rollback previous waves</small>
              </button>
              <button
                className={rollback === 'manual' ? 'selected' : ''}
                onClick={() => setRollback('manual')}
              >
                <Pencil size={18} />
                <strong>Manual</strong>
                <small>Wait for operator decision</small>
              </button>
            </div>
          </section>

          <section className="form-section">
            <div className="toggle-row">
              <div>
                <strong>Require passing automation evidence</strong>
                <p>
                  When enabled, approval fails unless the selected version has a pipeline-reported{' '}
                  <code>autoTest: passed</code> result.
                </p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={automation}
                className={`switch ${automation ? 'on' : ''}`}
                onClick={() => setAutomation(!automation)}
              >
                <i />
              </button>
            </div>
          </section>
        </div>
      ) : (
        <div className="review-request">
          <div className="review-banner">
            <CheckCircle2 size={20} />
            <div>
              <strong>Request is ready to create</strong>
              <p>Approval re-checks immutable delivery evidence before the configured production adapter starts.</p>
            </div>
          </div>
          <div className="review-grid">
            <div>
              <span>Modules</span>
              <strong>{selected.map((id) => `${availableModules.find((item) => item.id === id)?.name} ${drafts[id]?.version || ''}`.trim()).join(', ')}</strong>
            </div>
            <div>
              <span>Strategy</span>
              <strong style={{ textTransform: 'capitalize' }}>{strategy.replace('_', ' ')}</strong>
            </div>
            <div>
              <span>Schedule</span>
              <strong>{scheduledDate(toOffsetIso(scheduledFor))}</strong>
            </div>
            <div>
              <span>Rollback</span>
              <strong>{rollback === 'automatic' ? 'Automatic SAGA compensation' : 'Manual intervention'}</strong>
            </div>
            <div>
              <span>Automation evidence</span>
              <strong>{automation ? 'Passing result required' : 'Not required'}</strong>
            </div>
          </div>
          <div className="approval-flow">
            <span>
              <FileCheck2 size={17} />Create request
            </span>
            <i />
            <span>
              <CheckCircle2 size={17} />Approval + evidence gates
            </span>
            <i />
            <span>
              <CalendarClock size={17} />DAG wave deploy
            </span>
          </div>
          {error && (
            <div className="inline-error" role="alert">
              <CircleAlert size={15} />
              {error}
            </div>
          )}
        </div>
      )}
    </Modal>
  )
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
  const [availableModules, setAvailableModules] = useState<PortalModuleView[]>([])

  const load = async () => {
    setLoading(true)
    setLoadError('')
    try {
      setItems(await listProductionRequests())
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : 'Không thể tải production requests.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  useEffect(() => {
    let active = true
    if (systemId) {
      getSystem(systemId)
        .then((system) => {
          if (active)
            setAvailableModules(
              system.modules.map((module) => ({
                id: module.id,
                name: module.name,
                type: module.type,
                description: module.description,
                versions: module.versions,
                runtime: module.runtime,
                environments: module.environments,
              }))
            )
        })
        .catch(() => undefined)
    } else {
      listSystems()
        .then((systems) => {
          if (!active) return
          const allMods = systems.flatMap((s) =>
            s.modules.map((m) => ({
              id: m.id,
              name: m.name,
              type: m.type,
              description: m.description,
              versions: m.versions,
              runtime: m.runtime,
              environments: m.environments,
            }))
          )
          if (allMods.length > 0) setAvailableModules(allMods)
        })
        .catch(() => undefined)
    }
    return () => {
      active = false
    }
  }, [systemId])

  const [scopeFilter, setScopeFilter] = useState<'scoped' | 'all'>(systemId ? 'scoped' : 'all')

  const scopedItems = useMemo(() => {
    if (!systemId || scopeFilter === 'all' || availableModules.length === 0) return items
    return items.filter((request) =>
      request.modules.some((item) =>
        availableModules.some((am) => am.id === item.moduleId || am.name === item.moduleName || am.id === item.moduleName)
      )
    )
  }, [items, systemId, scopeFilter, availableModules])

  const filtered = useMemo(
    () =>
      scopedItems.filter((request) => {
        const date = request.scheduledFor.slice(0, 10)
        return (
          displayRequestId(request).toLowerCase().includes(query.toLowerCase()) &&
          (moduleFilter === 'All modules' || request.modules.some((item) => item.moduleName === moduleFilter)) &&
          (statusFilter === 'All statuses' || request.status === statusFilter) &&
          (!fromDate || date >= fromDate) &&
          (!toDate || date <= toDate)
        )
      }).sort((a, b) => (b.createdAt ?? b.scheduledFor).localeCompare(a.createdAt ?? a.scheduledFor)),
    [scopedItems, query, moduleFilter, statusFilter, fromDate, toDate]
  )

  const create = async (payload: ProductionRequestCreate) => {
    const request = await createProductionRequest(payload)
    setItems((current) => [request, ...current])
    notify(`${displayRequestId(request)} đã được tạo và đang chờ approval.`)
  }

  const updateStatus = async (action: 'approve' | 'reject') => {
    if (!details) return
    setCommandBusy(true)
    try {
      const updated =
        action === 'approve'
          ? await approveProductionRequest(details.id, { comment: 'Approved from Release Portal' })
          : await rejectProductionRequest(details.id, { comment: 'Rejected from Release Portal' })
      setItems((current) => current.map((item) => (item.id === updated.id ? updated : item)))
      setDetails(updated)
      notify(`${displayRequestId(updated)} đã được ${action === 'approve' ? 'approve' : 'reject'}.`)
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Không thể cập nhật request.', 'error')
    } finally {
      setCommandBusy(false)
    }
  }

  return (
    <>
      <PageHeader
        title={systemId && scopeFilter === 'scoped' ? `Production Requests (${systemId})` : "Production Requests"}
        description={systemId && scopeFilter === 'scoped' ? `Quản lý và phê duyệt các đợt phát hành Production cho riêng hệ thống ${systemId}.` : "Tạo và theo dõi các yêu cầu phát hành lên môi trường Production trên toàn bộ hệ thống."}
        action={
          <div style={{ display: 'flex', gap: '10px', alignItems: 'center' }}>
            {systemId && (
              <div className="segmented compact" style={{ margin: 0 }}>
                <button
                  className={scopeFilter === 'scoped' ? 'active' : ''}
                  onClick={() => setScopeFilter('scoped')}
                >
                  Chỉ {systemId}
                </button>
                <button
                  className={scopeFilter === 'all' ? 'active' : ''}
                  onClick={() => setScopeFilter('all')}
                >
                  Tất cả hệ thống
                </button>
              </div>
            )}
            <button className="primary-button" onClick={() => setCreating(true)}>
              <Plus size={16} />New Request
            </button>
          </div>
        }
      />
      <div className="request-kpis">
        {[
          ['Total Requests', String(scopedItems.length), 'neutral'],
          ['Approved', String(scopedItems.filter((item) => item.status === 'approved').length), 'green'],
          ['Blocked / Rejected', String(scopedItems.filter((item) => ['blocked', 'rejected'].includes(item.status)).length), 'red'],
          ['Pending', String(scopedItems.filter((item) => item.status === 'waiting_approval').length), 'amber'],
        ].map(([label, value, tone]) => (
          <article key={label}>
            <i className={`request-kpi-dot ${tone}`} />
            <div>
              <span>{label}</span>
              <strong>{value}</strong>
            </div>
          </article>
        ))}
      </div>
      <section className="panel table-panel">
        <div className="table-toolbar request-filters">
          <label className="input-with-icon">
            <Search size={16} />
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search request ID…" />
          </label>
          <select value={moduleFilter} onChange={(event) => setModuleFilter(event.target.value)}>
            <option>All modules</option>
            {availableModules.map((item) => (
              <option key={item.id}>{item.name}</option>
            ))}
          </select>
          <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
            <option value="All statuses">All statuses</option>
            <option value="succeeded">Succeeded</option>
            <option value="approved">Approved / deploying</option>
            <option value="rejected">Rejected</option>
            <option value="blocked">Blocked</option>
            <option value="waiting_approval">Pending checks</option>
          </select>
          <label className="date-filter">
            <span>From</span>
            <input type="date" value={fromDate} onChange={(event) => setFromDate(event.target.value)} />
          </label>
          <label className="date-filter">
            <span>To</span>
            <input type="date" value={toDate} min={fromDate || undefined} onChange={(event) => setToDate(event.target.value)} />
          </label>
          <button
            className="text-button"
            onClick={() => {
              setQuery('')
              setModuleFilter('All modules')
              setStatusFilter('All statuses')
              setFromDate('')
              setToDate('')
            }}
          >
            Clear filters
          </button>
        </div>
        {loadError && (
          <div className="load-state error-state" role="alert">
            <CircleAlert size={20} />
            <strong>Không tải được approval queue</strong>
            <span>{loadError}</span>
            <button className="secondary-button" onClick={load}>
              <RefreshCw size={15} />Retry
            </button>
          </div>
        )}
        {!loadError && (
          <div className="data-table requests-table">
            <div className="table-row table-head">
              <span>Request</span>
              <span>Modules</span>
              <span>Strategy</span>
              <span>Requested by</span>
              <span>Scheduled</span>
              <span>Rollback</span>
              <span>Status</span>
              <span>Actions</span>
            </div>
            {filtered.map((request) => (
              <div className="table-row" key={request.id}>
                <span className="request-id">{displayRequestId(request)}</span>
                <span className="module-list-cell">
                  {request.modules.map((item) => (
                    <small key={item.moduleId}>{item.moduleName}</small>
                  ))}
                </span>
                <span>
                  <span className={`strategy-tag ${request.strategy || 'rolling'}`}>
                    {request.strategy || 'rolling'}
                  </span>
                </span>
                <span>{request.requestedBy}</span>
                <span>{scheduledDate(request.scheduledFor)}</span>
                <span>{request.rollbackStrategy}</span>
                <StatusPill status={statusLabel(request.status)} />
                <span className="row-actions">
                  <button aria-label={`Xem ${displayRequestId(request)}`} onClick={() => setDetails(request)}>
                    <Eye size={16} />
                  </button>
                </span>
              </div>
            ))}
          </div>
        )}
        {!loadError && !loading && !filtered.length && (
          <div className="empty-table">
            <Search size={22} />
            <strong>Không có request phù hợp</strong>
            <span>Thử đổi bộ lọc hoặc tạo production request mới.</span>
          </div>
        )}
        {loading && (
          <div className="load-state">
            <RefreshCw className="spin" size={20} />
            <strong>Loading production requests…</strong>
          </div>
        )}
      </section>
      {details && (
        <RequestDetails
          request={details}
          busy={commandBusy}
          onClose={() => { setDetails(null); void load() }}
          onApprove={() => updateStatus('approve')}
          onReject={() => updateStatus('reject')}
          onRefresh={load}
        />
      )}
      {creating && <NewRequest availableModules={availableModules} onClose={() => setCreating(false)} onCreate={create} />}
    </>
  )
}
