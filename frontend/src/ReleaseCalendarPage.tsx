import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle, CalendarDays, CheckCircle2, RefreshCw } from 'lucide-react'
import {
  cancelChangeFreeze, createChangeFreeze, getModule, listChangeFreezes,
  listProductionRequests, listServersMaintenance,
  type ChangeFreeze, type Environment,
  type ProductionRequest, type ServerMaintenanceState,
} from './api/netciClient'
import { PageHeader, StatusPill } from './PortalShell'

/** Requests that are still going to happen, or are happening. */
const SETTLED = new Set(['succeeded', 'rejected', 'cancelled', 'failed', 'blocked', 'rolled_back'])

export type CalendarWarning = {
  requestId: string
  kind: 'same-day' | 'maintenance' | 'overdue' | 'freeze'
  message: string
}

/** Local calendar day of an ISO instant, as YYYY-MM-DD. */
export function dayOf(iso: string): string {
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/**
 * What a release manager should look at before a scheduled release -- derived only from
 * records netCI holds. Three things it can know: two releases of one module on the same
 * day; a production target that is in maintenance *now*; a release whose time has passed
 * while it still waits for approval. It does not warn about future maintenance windows,
 * because netCI does not record any: maintenance is a flag an operator sets, not a window
 * with a start and an end, and a calendar that implied otherwise would be guessing.
 * Change freezes are windows netCI enforces: deployments inside them are refused.
 */
export function calendarWarnings(
  requests: ProductionRequest[],
  maintenance: ServerMaintenanceState[],
  prodTargets: Record<string, string[]>,
  now: Date,
  freezes: ChangeFreeze[] = [],
): CalendarWarning[] {
  const warnings: CalendarWarning[] = []
  const active = requests.filter((r) => !SETTLED.has(r.status))
  const inMaintenance = new Map(maintenance.filter((m) => m.inMaintenance).map((m) => [m.serverName, m]))

  const byModuleDay = new Map<string, ProductionRequest[]>()
  for (const request of active) {
    for (const module of request.modules) {
      const key = `${module.moduleId}|${dayOf(request.scheduledFor)}`
      byModuleDay.set(key, [...(byModuleDay.get(key) ?? []), request])
    }
  }
  for (const [key, group] of byModuleDay) {
    if (group.length < 2) continue
    const moduleId = key.split('|')[0]
    for (const request of group) {
      warnings.push({ requestId: request.id, kind: 'same-day',
        message: `${moduleId} is in ${group.length} releases scheduled for the same day` })
    }
  }

  for (const request of active) {
    for (const module of request.modules) {
      for (const host of prodTargets[module.moduleId] ?? []) {
        const state = inMaintenance.get(host)
        if (state) {
          warnings.push({ requestId: request.id, kind: 'maintenance',
            message: `${module.moduleId}: production target ${host} is in maintenance now${state.reason ? ` (${state.reason})` : ''}` })
        }
      }
    }
    if (request.status === 'waiting_approval' && new Date(request.scheduledFor) < now) {
      warnings.push({ requestId: request.id, kind: 'overdue',
        message: 'the scheduled time has passed and it is still waiting for approval' })
    }

    const scheduledTime = new Date(request.scheduledFor).getTime()
    for (const freeze of freezes) {
      if (freeze.cancelledAt) continue
      if (!freeze.environments.includes('prod')) continue

      const freezeStart = new Date(freeze.startsAt).getTime()
      const freezeEnd = new Date(freeze.endsAt).getTime()
      if (scheduledTime < freezeStart || scheduledTime >= freezeEnd) continue

      if (freeze.moduleId !== null && !request.modules.some((m) => m.moduleId === freeze.moduleId)) {
        continue
      }

      // SystemId scope: ignore unless you have the module's system; treat a system-scoped freeze as matching
      // when module system information is not present on the request's modules.
      if (freeze.systemId !== null) {
        const knownSystems = request.modules.map((m) => m.systemId).filter(Boolean)
        if (knownSystems.length > 0 && !knownSystems.includes(freeze.systemId)) {
          continue
        }
      }

      warnings.push({
        requestId: request.id,
        kind: 'freeze',
        message: `scheduled inside the freeze "${freeze.name}" (until ${new Date(freeze.endsAt).toLocaleTimeString()})`,
      })
    }
  }
  return warnings
}

type LoadState = 'loading' | 'ready' | 'error'

export function ReleaseCalendarPage({ now: fixedNow }: { now?: Date }) {
  // One clock per page load: a fresh Date on every render would recompute every memo below.
  const [now] = useState(() => fixedNow ?? new Date())
  const [requests, setRequests] = useState<ProductionRequest[]>([])
  const [maintenance, setMaintenance] = useState<ServerMaintenanceState[]>([])
  const [prodTargets, setProdTargets] = useState<Record<string, string[]>>({})
  const [freezes, setFreezes] = useState<ChangeFreeze[]>([])
  const [state, setState] = useState<LoadState>('loading')
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)

  // Freeze creation and cancellation UI state
  const [showForm, setShowForm] = useState(false)
  const [name, setName] = useState('')
  const [startsAt, setStartsAt] = useState('')
  const [endsAt, setEndsAt] = useState('')
  const [environments, setEnvironments] = useState<Environment[]>(['prod'])
  const [reason, setReason] = useState('')
  const [formError, setFormError] = useState('')
  const [actionError, setActionError] = useState('')

  useEffect(() => {
    let active = true
    setState('loading')
    Promise.all([listProductionRequests(), listServersMaintenance(), listChangeFreezes()])
      .then(async ([items, maint, freezeItems]) => {
        // Only the modules in releases still to happen: their production targets are
        // what a maintenance flag could collide with.
        const ids = [...new Set(items.filter((r) => !SETTLED.has(r.status)).flatMap((r) => r.modules.map((m) => m.moduleId)))]
        const targets: Record<string, string[]> = {}
        await Promise.all(ids.map(async (id) => {
          try {
            const module = await getModule(id)
            targets[id] = module.deploymentEnvironments
              .filter((e) => e.environment === 'prod').flatMap((e) => e.servers ?? [])
          } catch {
            targets[id] = []
          }
        }))
        if (!active) return
        setRequests(items)
        setMaintenance(maint)
        setProdTargets(targets)
        setFreezes(freezeItems)
        setState('ready')
      })
      .catch((cause) => {
        if (!active) return
        setError(cause instanceof Error ? cause.message : String(cause))
        setState('error')
      })
    return () => { active = false }
  }, [attempt])

  const handleCreateFreeze = async (e: React.FormEvent) => {
    e.preventDefault()
    setFormError('')
    if (environments.length === 0) {
      setFormError('At least one environment must be selected')
      return
    }
    try {
      const startsAtIso = new Date(startsAt).toISOString()
      const endsAtIso = new Date(endsAt).toISOString()
      await createChangeFreeze({
        name,
        startsAt: startsAtIso,
        endsAt: endsAtIso,
        environments,
        reason,
      })
      setShowForm(false)
      setName('')
      setStartsAt('')
      setEndsAt('')
      setEnvironments(['prod'])
      setReason('')
      setAttempt((n) => n + 1)
    } catch (err) {
      setFormError(err instanceof Error ? err.message : String(err))
    }
  }

  const handleCancelFreeze = async (id: string) => {
    try {
      setActionError('')
      await cancelChangeFreeze(id)
      setAttempt((n) => n + 1)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    }
  }

  const warnings = useMemo(() => calendarWarnings(requests, maintenance, prodTargets, now, freezes), [requests, maintenance, prodTargets, now, freezes])
  const days = useMemo(() => {
    const start = new Date(now); start.setDate(start.getDate() - 7)
    const list: string[] = []
    for (let i = 0; i < 36; i += 1) { const d = new Date(start); d.setDate(start.getDate() + i); list.push(dayOf(d.toISOString())) }
    return list
  }, [now])
  const byDay = useMemo(() => {
    const map = new Map<string, ProductionRequest[]>()
    for (const request of requests) {
      const day = dayOf(request.scheduledFor)
      map.set(day, [...(map.get(day) ?? []), request].sort((a, b) => a.scheduledFor.localeCompare(b.scheduledFor)))
    }
    return map
  }, [requests])
  const freezesByDay = useMemo(() => {
    const map = new Map<string, ChangeFreeze[]>()
    for (const day of days) {
      const dayStart = new Date(`${day}T00:00:00`).getTime()
      const nextDay = new Date(`${day}T00:00:00`)
      nextDay.setDate(nextDay.getDate() + 1)
      const dayEnd = nextDay.getTime()

      const covering = freezes.filter((f) => {
        if (f.cancelledAt) return false
        const fStart = new Date(f.startsAt).getTime()
        const fEnd = new Date(f.endsAt).getTime()
        return fStart < dayEnd && fEnd > dayStart
      })
      if (covering.length > 0) {
        map.set(day, covering)
      }
    }
    return map
  }, [days, freezes])

  const today = dayOf(now.toISOString())

  return <section className="page">
    <PageHeader
      title="Release Calendar"
      description="Production releases by the day they are scheduled for, with what could collide with them."
      action={
        <div style={{ display: 'flex', gap: '8px' }}>
          <button type="button" className="secondary-button" onClick={() => setShowForm((v) => !v)}>New freeze</button>
          <button type="button" className="secondary-button" onClick={() => setAttempt((n) => n + 1)}><RefreshCw size={15} />Refresh</button>
        </div>
      }
    />
    {showForm && (
      <form className="panel freeze-form" data-testid="freeze-form" onSubmit={handleCreateFreeze} style={{ marginBottom: '14px', display: 'flex', flexDirection: 'column', gap: '10px' }}>
        <h3 style={{ margin: '0 0 6px' }}>New change freeze</h3>
        {formError && <div className="inline-error" role="alert">{formError}</div>}
        <div>
          <label htmlFor="freeze-name" style={{ display: 'block', fontSize: '12px', fontWeight: 600, marginBottom: '4px' }}>Name</label>
          <input
            id="freeze-name"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            placeholder="e.g. Year-End Freeze"
          />
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
          <div>
            <label htmlFor="freeze-start" style={{ display: 'block', fontSize: '12px', fontWeight: 600, marginBottom: '4px' }}>Start</label>
            <input
              id="freeze-start"
              type="datetime-local"
              value={startsAt}
              onChange={(e) => setStartsAt(e.target.value)}
              required
            />
          </div>
          <div>
            <label htmlFor="freeze-end" style={{ display: 'block', fontSize: '12px', fontWeight: 600, marginBottom: '4px' }}>End</label>
            <input
              id="freeze-end"
              type="datetime-local"
              value={endsAt}
              onChange={(e) => setEndsAt(e.target.value)}
              required
            />
          </div>
        </div>
        <div>
          <span style={{ display: 'block', fontSize: '12px', fontWeight: 600, marginBottom: '4px' }}>Environments</span>
          <div style={{ display: 'flex', gap: '14px' }}>
            {(['dev', 'staging', 'prod'] as const).map((env) => (
              <label key={env} className="checkbox-field" style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                <input
                  type="checkbox"
                  name="environments"
                  value={env}
                  checked={environments.includes(env)}
                  onChange={(e) => {
                    if (e.target.checked) {
                      setEnvironments([...environments, env])
                    } else {
                      setEnvironments(environments.filter((x) => x !== env))
                    }
                  }}
                />
                <span>{env}</span>
              </label>
            ))}
          </div>
        </div>
        <div>
          <label htmlFor="freeze-reason" style={{ display: 'block', fontSize: '12px', fontWeight: 600, marginBottom: '4px' }}>Reason</label>
          <textarea
            id="freeze-reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            required
            placeholder="Reason for change freeze"
          />
        </div>
        <div style={{ display: 'flex', gap: '8px', marginTop: '6px' }}>
          <button type="submit" className="primary-button">Create freeze</button>
          <button type="button" className="secondary-button" onClick={() => { setShowForm(false); setFormError('') }}>Cancel</button>
        </div>
      </form>
    )}
    {actionError && <div className="inline-error" role="alert" style={{ marginBottom: '14px' }}>{actionError}</div>}
    {state === 'loading' && <div className="panel empty-table">Loading scheduled releases…</div>}
    {state === 'error' && <div className="inline-error" role="alert">Could not load the calendar: {error}</div>}
    {state === 'ready' && <>
      <div className="panel calendar-summary" data-testid="calendar-summary">
        {warnings.length
          ? <span><AlertTriangle size={16} /> {new Set(warnings.map((w) => w.requestId)).size} release(s) need attention</span>
          : <span><CheckCircle2 size={16} /> No conflicts among scheduled releases</span>}
        <small>Maintenance is a server flag checked as it is right now (not future maintenance windows). Change freezes are windows netCI enforces: deployments inside them are refused.</small>
      </div>
      <div className="calendar-days">
        {days.filter((day) => byDay.has(day) || day === today || freezesByDay.has(day)).map((day) => (
          <section key={day} className={`panel calendar-day${day === today ? ' today' : ''}`} data-testid={`calendar-day-${day}`}>
            <h3><CalendarDays size={15} />{new Date(`${day}T00:00:00`).toLocaleDateString('vi-VN', { weekday: 'short', day: '2-digit', month: '2-digit', year: 'numeric' })}{day === today ? ' · hôm nay' : ''}</h3>
            {(freezesByDay.get(day) ?? []).map((freeze) => {
              const scope = freeze.moduleId || freeze.systemId || 'all modules'
              const timeRange = dayOf(freeze.startsAt) === dayOf(freeze.endsAt)
                ? `${new Date(freeze.startsAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} – ${new Date(freeze.endsAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
                : `${dayOf(freeze.startsAt)} ${new Date(freeze.startsAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} – ${dayOf(freeze.endsAt)} ${new Date(freeze.endsAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
              return (
                <div key={freeze.id} className="calendar-freeze" data-testid={`calendar-freeze-${freeze.id}`} style={{ padding: '8px 0', borderTop: '1px solid var(--border)', display: 'grid', gridTemplateColumns: '1fr auto', gap: '4px 12px' }}>
                  <div>
                    <strong>{freeze.name}</strong>
                    <small style={{ display: 'block', color: 'var(--muted)', fontSize: '11px' }}>
                      {timeRange} · {freeze.environments.join(', ')} · scope: {scope}
                    </small>
                    <p style={{ margin: '4px 0 0', fontSize: '12px' }}>{freeze.reason}</p>
                  </div>
                  <div>
                    <button
                      type="button"
                      className="secondary-button"
                      onClick={() => handleCancelFreeze(freeze.id)}
                    >
                      Cancel freeze
                    </button>
                  </div>
                </div>
              )
            })}
            {(byDay.get(day) ?? []).map((request) => {
              const mine = warnings.filter((w) => w.requestId === request.id)
              return <article key={request.id} className={`calendar-release${mine.length ? ' has-warning' : ''}`}>
                <div>
                  <strong>{request.modules.map((m) => `${m.moduleName} ${m.version}`).join(' · ')}</strong>
                  <small>{new Date(request.scheduledFor).toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' })} · requested by {request.requestedBy}</small>
                </div>
                <StatusPill status={request.status.replace('_', ' ')} />
                {mine.map((w, i) => <p key={i} className="calendar-warning" role="note"><AlertTriangle size={13} />{w.message}</p>)}
              </article>
            })}
            {!byDay.has(day) && !(freezesByDay.get(day)?.length) && <small className="muted">Nothing scheduled.</small>}
          </section>
        ))}
      </div>
    </>}
  </section>
}
