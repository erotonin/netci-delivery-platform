import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle, CalendarDays, CheckCircle2, RefreshCw } from 'lucide-react'
import {
  getModule, listProductionRequests, listServersMaintenance,
  type ProductionRequest, type ServerMaintenanceState,
} from './api/netciClient'
import { PageHeader, StatusPill } from './PortalShell'

/** Requests that are still going to happen, or are happening. */
const SETTLED = new Set(['succeeded', 'rejected', 'cancelled', 'failed', 'blocked', 'rolled_back'])

export type CalendarWarning = { requestId: string; kind: 'same-day' | 'maintenance' | 'overdue'; message: string }

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
 */
export function calendarWarnings(
  requests: ProductionRequest[],
  maintenance: ServerMaintenanceState[],
  prodTargets: Record<string, string[]>,
  now: Date,
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
  const [state, setState] = useState<LoadState>('loading')
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let active = true
    setState('loading')
    Promise.all([listProductionRequests(), listServersMaintenance()])
      .then(async ([items, maint]) => {
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
        setRequests(items); setMaintenance(maint); setProdTargets(targets); setState('ready')
      })
      .catch((cause) => {
        if (!active) return
        setError(cause instanceof Error ? cause.message : String(cause)); setState('error')
      })
    return () => { active = false }
  }, [attempt])

  const warnings = useMemo(() => calendarWarnings(requests, maintenance, prodTargets, now), [requests, maintenance, prodTargets, now])
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
  const today = dayOf(now.toISOString())

  return <section className="page">
    <PageHeader title="Release Calendar" description="Production releases by the day they are scheduled for, with what could collide with them." action={
      <button className="secondary-button" onClick={() => setAttempt((n) => n + 1)}><RefreshCw size={15} />Refresh</button>
    } />
    {state === 'loading' && <div className="panel empty-table">Loading scheduled releases…</div>}
    {state === 'error' && <div className="inline-error" role="alert">Could not load the calendar: {error}</div>}
    {state === 'ready' && <>
      <div className="panel calendar-summary" data-testid="calendar-summary">
        {warnings.length
          ? <span><AlertTriangle size={16} /> {new Set(warnings.map((w) => w.requestId)).size} release(s) need attention</span>
          : <span><CheckCircle2 size={16} /> No conflicts among scheduled releases</span>}
        <small>Maintenance is checked as it is right now: netCI records a maintenance flag, not future maintenance windows.</small>
      </div>
      <div className="calendar-days">
        {days.filter((day) => byDay.has(day) || day === today).map((day) => (
          <section key={day} className={`panel calendar-day${day === today ? ' today' : ''}`} data-testid={`calendar-day-${day}`}>
            <h3><CalendarDays size={15} />{new Date(`${day}T00:00:00`).toLocaleDateString('vi-VN', { weekday: 'short', day: '2-digit', month: '2-digit', year: 'numeric' })}{day === today ? ' · hôm nay' : ''}</h3>
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
            {!byDay.has(day) && <small className="muted">Nothing scheduled.</small>}
          </section>
        ))}
      </div>
    </>}
  </section>
}
