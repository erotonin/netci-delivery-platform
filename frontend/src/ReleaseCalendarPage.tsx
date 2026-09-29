import { useEffect, useMemo, useState, type FormEvent } from 'react'
import {
  AlertTriangle,
  CalendarDays,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  RefreshCw,
  X,
} from 'lucide-react'
import {
  cancelChangeFreeze,
  createChangeFreeze,
  getModule,
  listChangeFreezes,
  listProductionRequests,
  listServersMaintenance,
  type ChangeFreeze,
  type Environment,
  type ProductionRequest,
  type ServerMaintenanceState,
} from './api/netciClient'
import { PageHeader, StatusPill } from './PortalShell'
import './calendar.css'

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
      warnings.push({
        requestId: request.id,
        kind: 'same-day',
        message: `${moduleId} is in ${group.length} releases scheduled for the same day`,
      })
    }
  }

  for (const request of active) {
    for (const module of request.modules) {
      for (const host of prodTargets[module.moduleId] ?? []) {
        const state = inMaintenance.get(host)
        if (state) {
          warnings.push({
            requestId: request.id,
            kind: 'maintenance',
            message: `${module.moduleId}: production target ${host} is in maintenance now${state.reason ? ` (${state.reason})` : ''}`,
          })
        }
      }
    }
    if (request.status === 'waiting_approval' && new Date(request.scheduledFor) < now) {
      warnings.push({
        requestId: request.id,
        kind: 'overdue',
        message: 'the scheduled time has passed and it is still waiting for approval',
      })
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
type ViewMode = 'month' | 'week' | 'agenda'

function getStatusCategory(status: string): 'approved' | 'pending' | 'rejected' | 'executed' {
  const s = status.toLowerCase()
  if (s === 'approved') return 'approved'
  if (s === 'succeeded' || s === 'executed' || s === 'deployed') return 'executed'
  if (s === 'rejected' || s === 'cancelled' || s === 'failed' || s === 'blocked' || s === 'rolled_back') return 'rejected'
  return 'pending'
}

export function ReleaseCalendarPage({ now: fixedNow }: { now?: Date }) {
  // One clock per page load: a fresh Date on every render would recompute every memo below.
  const [now] = useState(() => fixedNow ?? new Date())
  const [viewDate, setViewDate] = useState(() => new Date(now))
  const [viewMode, setViewMode] = useState<ViewMode>('month')
  const [selectedDay, setSelectedDay] = useState<string | null>(null)
  const [selectedRequestId, setSelectedRequestId] = useState<string | null>(null)

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
        const ids = [...new Set(items.filter((r) => !SETTLED.has(r.status)).flatMap((r) => r.modules.map((m) => m.moduleId)))]
        const targets: Record<string, string[]> = {}
        await Promise.all(
          ids.map(async (id) => {
            try {
              const module = await getModule(id)
              targets[id] = module.deploymentEnvironments
                .filter((e) => e.environment === 'prod')
                .flatMap((e) => e.servers ?? [])
            } catch {
              targets[id] = []
            }
          })
        )
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
    return () => {
      active = false
    }
  }, [attempt])

  const handleCreateFreeze = async (e: FormEvent) => {
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

  const warnings = useMemo(
    () => calendarWarnings(requests, maintenance, prodTargets, now, freezes),
    [requests, maintenance, prodTargets, now, freezes]
  )

  const today = dayOf(now.toISOString())

  const byDay = useMemo(() => {
    const map = new Map<string, ProductionRequest[]>()
    for (const request of requests) {
      const day = dayOf(request.scheduledFor)
      map.set(day, [...(map.get(day) ?? []), request].sort((a, b) => a.scheduledFor.localeCompare(b.scheduledFor)))
    }
    return map
  }, [requests])

  // Compute 5-6 week rows for the active month (Monday-first)
  const monthWeeks = useMemo(() => {
    const year = viewDate.getFullYear()
    const month = viewDate.getMonth()
    const firstDay = new Date(year, month, 1, 12, 0, 0)
    const firstCol = (firstDay.getDay() + 6) % 7 // Monday = 0 ... Sunday = 6
    const startDate = new Date(year, month, 1 - firstCol, 12, 0, 0)

    const lastDay = new Date(year, month + 1, 0, 12, 0, 0)
    const lastCol = (lastDay.getDay() + 6) % 7
    const daysAfter = 6 - lastCol
    const endDate = new Date(year, month + 1, daysAfter, 12, 0, 0)

    const list: Array<{ date: Date; dateStr: string; isCurrentMonth: boolean; isWeekend: boolean }> = []
    const cur = new Date(startDate)
    while (cur <= endDate) {
      const dateStr = dayOf(cur.toISOString())
      const col = (cur.getDay() + 6) % 7
      list.push({
        date: new Date(cur),
        dateStr,
        isCurrentMonth: cur.getMonth() === month,
        isWeekend: col === 5 || col === 6,
      })
      cur.setDate(cur.getDate() + 1)
    }

    const rows: Array<typeof list> = []
    for (let i = 0; i < list.length; i += 7) {
      rows.push(list.slice(i, i + 7))
    }
    return rows
  }, [viewDate])

  // Compute 7-day week (Monday-first) for week view
  const weekDays = useMemo(() => {
    const dow = (viewDate.getDay() + 6) % 7
    const mon = new Date(viewDate)
    mon.setDate(mon.getDate() - dow)
    const list: Array<{ date: Date; dateStr: string; isWeekend: boolean }> = []
    for (let i = 0; i < 7; i++) {
      const d = new Date(mon)
      d.setDate(mon.getDate() + i)
      const col = (d.getDay() + 6) % 7
      list.push({
        date: d,
        dateStr: dayOf(d.toISOString()),
        isWeekend: col === 5 || col === 6,
      })
    }
    return list
  }, [viewDate])

  // Change freezes covering each day
  const freezesByDay = useMemo(() => {
    const map = new Map<string, ChangeFreeze[]>()

    const checkDay = (day: string) => {
      if (map.has(day)) return
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

    for (const f of freezes) {
      if (f.cancelledAt) continue
      const start = new Date(f.startsAt)
      const end = new Date(f.endsAt)
      const cur = new Date(start)
      while (cur <= end) {
        checkDay(dayOf(cur.toISOString()))
        cur.setDate(cur.getDate() + 1)
      }
      checkDay(dayOf(end.toISOString()))
    }

    for (const week of monthWeeks) {
      for (const d of week) {
        checkDay(d.dateStr)
      }
    }
    for (const d of weekDays) {
      checkDay(d.dateStr)
    }
    for (const r of requests) {
      checkDay(dayOf(r.scheduledFor))
    }
    return map
  }, [freezes, monthWeeks, weekDays, requests])

  // Summary row statistics for the viewed month
  const stats = useMemo(() => {
    const pad = (n: number) => String(n).padStart(2, '0')
    const prefix = `${viewDate.getFullYear()}-${pad(viewDate.getMonth() + 1)}`
    const monthRequests = requests.filter((r) => dayOf(r.scheduledFor).startsWith(prefix))
    const inFreezes = monthRequests.filter((r) => warnings.some((w) => w.requestId === r.id && w.kind === 'freeze'))
    const pendingApproval = monthRequests.filter((r) => r.status === 'waiting_approval')

    return {
      releasesThisMonth: monthRequests.length,
      inFreezes: inFreezes.length,
      pendingApproval: pendingApproval.length,
    }
  }, [requests, warnings, viewDate])

  // Upcoming items for agenda view
  const upcomingDays = useMemo(() => {
    const daySet = new Set<string>()
    for (const day of byDay.keys()) {
      if (day >= today) daySet.add(day)
    }
    for (const day of freezesByDay.keys()) {
      if (day >= today) daySet.add(day)
    }
    const sorted = Array.from(daySet).sort()
    if (sorted.length === 0) {
      const allDays = new Set([...byDay.keys(), ...freezesByDay.keys()])
      return Array.from(allDays).sort()
    }
    return sorted
  }, [byDay, freezesByDay, today])

  const handlePrev = () => {
    if (viewMode === 'week') {
      setViewDate((d) => {
        const next = new Date(d)
        next.setDate(next.getDate() - 7)
        return next
      })
    } else {
      setViewDate((d) => new Date(d.getFullYear(), d.getMonth() - 1, 1))
    }
  }

  const handleNext = () => {
    if (viewMode === 'week') {
      setViewDate((d) => {
        const next = new Date(d)
        next.setDate(next.getDate() + 7)
        return next
      })
    } else {
      setViewDate((d) => new Date(d.getFullYear(), d.getMonth() + 1, 1))
    }
  }

  const handleToday = () => {
    setViewDate(new Date(now))
  }

  const monthTitle = `Tháng ${viewDate.getMonth() + 1}, ${viewDate.getFullYear()}`

  // Render a freeze inside a calendar cell
  const renderCellFreeze = (freeze: ChangeFreeze, day: string) => {
    const scope = freeze.moduleId || freeze.systemId || 'all modules'
    const timeRange = dayOf(freeze.startsAt) === dayOf(freeze.endsAt)
      ? `${new Date(freeze.startsAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} – ${new Date(freeze.endsAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
      : `${dayOf(freeze.startsAt)} ${new Date(freeze.startsAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} – ${dayOf(freeze.endsAt)} ${new Date(freeze.endsAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`

    return (
      <div
        key={freeze.id}
        className="cal-freeze-band calendar-freeze"
        data-testid={`calendar-freeze-${freeze.id}`}
        onClick={(e) => {
          e.stopPropagation()
          setSelectedDay(day)
        }}
      >
        <strong className="cal-freeze-name">{freeze.name}</strong>
        {freeze.reason && <span className="cal-freeze-reason">: {freeze.reason}</span>}
        <div className="cal-sr-only">
          <small>{timeRange} · {freeze.environments.join(', ')} · scope: {scope}</small>
          <p>{freeze.reason}</p>
          <button
            type="button"
            className="secondary-button"
            onClick={(e) => {
              e.stopPropagation()
              handleCancelFreeze(freeze.id)
            }}
          >
            Cancel freeze
          </button>
        </div>
      </div>
    )
  }

  // Render a release request inside a calendar cell
  const renderCellRequest = (request: ProductionRequest, day: string) => {
    const mine = warnings.filter((w) => w.requestId === request.id)
    const statusCat = getStatusCategory(request.status)
    const time = new Date(request.scheduledFor).toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' })
    const moduleText = request.modules.map((m) => `${m.moduleName || m.moduleId} ${m.version}`).join(' · ')

    return (
      <button
        key={request.id}
        type="button"
        className={`cal-chip cal-status-${statusCat}${mine.length ? ' cal-chip-has-warning' : ''}`}
        onClick={(e) => {
          e.stopPropagation()
          setSelectedDay(day)
          setSelectedRequestId(request.id)
        }}
        title={`${time} ${moduleText} (${request.status})`}
      >
        <span className="cal-chip-time">{time}</span>
        <span className="cal-chip-title">{moduleText}</span>
        {mine.length > 0 && <AlertTriangle size={11} className="cal-chip-warning-icon" />}
        <div className="cal-sr-only">
          {mine.map((w, i) => (
            <p key={i} className="calendar-warning" role="note">
              {w.message}
            </p>
          ))}
        </div>
      </button>
    )
  }

  // Agenda list rendering helper
  const renderAgendaList = () => (
    upcomingDays.length === 0 ? (
      <div className="panel empty-table">
        <strong>Không có sự kiện sắp tới</strong>
        <span>Không có bản phát hành hay freeze nào được lên lịch.</span>
      </div>
    ) : (
      upcomingDays.map((day) => {
        const dayReqs = byDay.get(day) ?? []
        const dayFrzs = freezesByDay.get(day) ?? []
        return (
          <section
            key={day}
            className={`panel cal-agenda-day-card${day === today ? ' today' : ''}`}
            data-testid={`agenda-day-${day}`}
          >
            <div className="cal-agenda-day-header">
              <h4>
                <CalendarDays size={15} />
                {new Date(`${day}T00:00:00`).toLocaleDateString('vi-VN', {
                  weekday: 'short',
                  day: '2-digit',
                  month: '2-digit',
                  year: 'numeric',
                })}
                {day === today ? ' · Hôm nay' : ''}
              </h4>
            </div>

            {dayFrzs.map((freeze) => {
              const scope = freeze.moduleId || freeze.systemId || 'all modules'
              const timeRange = dayOf(freeze.startsAt) === dayOf(freeze.endsAt)
                ? `${new Date(freeze.startsAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} – ${new Date(freeze.endsAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
                : `${dayOf(freeze.startsAt)} ${new Date(freeze.startsAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} – ${dayOf(freeze.endsAt)} ${new Date(freeze.endsAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
              return (
                <div
                  key={freeze.id}
                  className="calendar-freeze cal-agenda-freeze"
                  data-testid={`agenda-freeze-${freeze.id}`}
                  style={{
                    padding: '8px 0',
                    borderTop: '1px solid var(--border)',
                    display: 'grid',
                    gridTemplateColumns: '1fr auto',
                    gap: '4px 12px',
                  }}
                >
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

            {dayReqs.map((request) => {
              const mine = warnings.filter((w) => w.requestId === request.id)
              return (
                <article
                  key={request.id}
                  className={`calendar-release cal-agenda-release${mine.length ? ' has-warning' : ''}`}
                  onClick={() => {
                    setSelectedDay(day)
                    setSelectedRequestId(request.id)
                  }}
                  style={{ cursor: 'pointer' }}
                >
                  <div>
                    <strong>{request.modules.map((m) => `${m.moduleName || m.moduleId} ${m.version}`).join(' · ')}</strong>
                    <small>
                      {new Date(request.scheduledFor).toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' })} · requested by {request.requestedBy}
                    </small>
                  </div>
                  <StatusPill status={request.status.replace('_', ' ')} />
                  {mine.map((w, i) => (
                    <p key={i} className="calendar-warning" role="note">
                      <AlertTriangle size={13} />
                      {w.message}
                    </p>
                  ))}
                </article>
              )
            })}
          </section>
        )
      })
    )
  )

  return (
    <section className="page cal-page">
      <PageHeader
        title="Release Calendar"
        description="Production releases by the day they are scheduled for, with what could collide with them."
        action={
          <div style={{ display: 'flex', gap: '8px' }}>
            <button type="button" className="secondary-button" onClick={() => setShowForm((v) => !v)}>
              New freeze
            </button>
            <button type="button" className="secondary-button" onClick={() => setAttempt((n) => n + 1)}>
              <RefreshCw size={15} />
              Refresh
            </button>
          </div>
        }
      />

      {showForm && (
        <form
          className="panel freeze-form"
          data-testid="freeze-form"
          onSubmit={handleCreateFreeze}
          style={{ marginBottom: '14px', display: 'flex', flexDirection: 'column', gap: '10px' }}
        >
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

      {state === 'ready' && (
        <div className="cal-container">
          {/* Calendar Toolbar */}
          <div className="cal-toolbar">
            <div className="cal-toolbar-nav">
              <button type="button" className="cal-today-btn" onClick={handleToday}>
                Hôm nay
              </button>
              <button
                type="button"
                className="cal-nav-btn"
                aria-label="Tháng trước"
                onClick={handlePrev}
                title="Tháng trước"
              >
                <ChevronLeft size={16} />
              </button>
              <button
                type="button"
                className="cal-nav-btn"
                aria-label="Tháng sau"
                onClick={handleNext}
                title="Tháng sau"
              >
                <ChevronRight size={16} />
              </button>
              <span className="cal-month-title">{monthTitle}</span>
            </div>

            <div className="segmented cal-view-switch" role="group" aria-label="Chế độ xem">
              <button
                type="button"
                className={viewMode === 'month' ? 'active' : ''}
                onClick={() => setViewMode('month')}
              >
                Tháng
              </button>
              <button
                type="button"
                className={viewMode === 'week' ? 'active' : ''}
                onClick={() => setViewMode('week')}
              >
                Tuần
              </button>
              <button
                type="button"
                className={viewMode === 'agenda' ? 'active' : ''}
                onClick={() => setViewMode('agenda')}
              >
                Danh sách
              </button>
            </div>
          </div>

          {/* Legend and Summary Stats Bar */}
          <div className="cal-legend-bar">
            <div className="cal-legend">
              <span className="cal-legend-title">Trạng thái:</span>
              <span className="cal-legend-item">
                <span className="cal-legend-dot cal-dot-approved" /> Đã duyệt
              </span>
              <span className="cal-legend-item">
                <span className="cal-legend-dot cal-dot-pending" /> Chờ duyệt
              </span>
              <span className="cal-legend-item">
                <span className="cal-legend-dot cal-dot-executed" /> Đã thực hiện
              </span>
              <span className="cal-legend-item">
                <span className="cal-legend-dot cal-dot-rejected" /> Từ chối / Hủy
              </span>
              <span className="cal-legend-item">
                <span className="cal-legend-dot cal-dot-freeze" /> Đóng băng thay đổi
              </span>
            </div>

            <div className="cal-summary-row" data-testid="calendar-month-stats">
              <div className="cal-stat-item">
                <strong className="cal-stat-value">{stats.releasesThisMonth}</strong>
                <span className="cal-stat-label">releases this month</span>
              </div>
              <div className="cal-stat-item">
                <strong className="cal-stat-value">{stats.inFreezes}</strong>
                <span className="cal-stat-label">in freezes</span>
              </div>
              <div className="cal-stat-item">
                <strong className="cal-stat-value">{stats.pendingApproval}</strong>
                <span className="cal-stat-label">pending approval</span>
              </div>
            </div>
          </div>

          {/* Summary Box with warnings and netCI scope reminder */}
          <div className="panel calendar-summary" data-testid="calendar-summary">
            {warnings.length ? (
              <span>
                <AlertTriangle size={16} /> {new Set(warnings.map((w) => w.requestId)).size} release(s) need attention
              </span>
            ) : (
              <span>
                <CheckCircle2 size={16} /> No conflicts among scheduled releases
              </span>
            )}
            <small>
              Maintenance is a server flag checked as it is right now (not future maintenance windows). Change freezes are windows netCI enforces: deployments inside them are refused.
            </small>
          </div>

          {/* Empty Month Note */}
          {stats.releasesThisMonth === 0 && (
            <div className="cal-empty-month-note" role="status">
              <span>Không có bản phát hành nào được lên lịch trong tháng này.</span>
            </div>
          )}

          {/* View Modes */}
          {viewMode === 'month' && (
            <div className="cal-month-view">
              <div className="cal-month-grid" role="grid" aria-label="Lịch phát hành theo tháng">
                <div className="cal-grid-header" role="row">
                  {['T2', 'T3', 'T4', 'T5', 'T6', 'T7', 'CN'].map((h) => (
                    <div key={h} className="cal-header-cell" role="columnheader">
                      {h}
                    </div>
                  ))}
                </div>

                <div className="cal-grid-body">
                  {monthWeeks.map((week, weekIdx) => (
                    <div key={weekIdx} className="cal-grid-row" role="row">
                      {week.map((item) => {
                        const dayFreezes = freezesByDay.get(item.dateStr) ?? []
                        const dayReqs = byDay.get(item.dateStr) ?? []
                        const total = dayFreezes.length + dayReqs.length
                        const maxVisible = 3
                        const visibleFreezes = dayFreezes.slice(0, maxVisible)
                        const remaining = Math.max(0, maxVisible - visibleFreezes.length)
                        const visibleReqs = dayReqs.slice(0, remaining)
                        const hiddenFreezes = dayFreezes.slice(visibleFreezes.length)
                        const hiddenReqs = dayReqs.slice(remaining)
                        const hiddenCount = hiddenFreezes.length + hiddenReqs.length

                        return (
                          <div
                            key={item.dateStr}
                            className={`cal-grid-cell${!item.isCurrentMonth ? ' cal-day-dimmed' : ''}${item.isWeekend ? ' cal-weekend' : ''}${item.dateStr === today ? ' cal-today today' : ''}`}
                            role="gridcell"
                            aria-label={item.dateStr}
                            data-testid={`calendar-day-${item.dateStr}`}
                            onClick={() => setSelectedDay(item.dateStr)}
                          >
                            <div className="cal-day-header">
                              <span className={`cal-day-number${item.dateStr === today ? ' cal-day-number-today' : ''}`}>
                                {item.date.getDate()}
                              </span>
                            </div>

                            {visibleFreezes.map((f) => renderCellFreeze(f, item.dateStr))}
                            {visibleReqs.map((r) => renderCellRequest(r, item.dateStr))}

                            {hiddenCount > 0 && (
                              <>
                                <button
                                  type="button"
                                  className="cal-more-btn"
                                  onClick={(e) => {
                                    e.stopPropagation()
                                    setSelectedDay(item.dateStr)
                                  }}
                                >
                                  +{hiddenCount} more
                                </button>
                                <div className="cal-sr-only">
                                  {hiddenFreezes.map((f) => renderCellFreeze(f, item.dateStr))}
                                  {hiddenReqs.map((r) => renderCellRequest(r, item.dateStr))}
                                </div>
                              </>
                            )}

                            {total === 0 && (
                              <span className="cal-sr-only">Nothing scheduled.</span>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {viewMode === 'week' && (
            <div className="cal-week-view">
              <div className="cal-week-grid" role="grid" aria-label="Lịch phát hành theo tuần">
                <div className="cal-grid-header" role="row">
                  {weekDays.map((item, idx) => {
                    const names = ['T2', 'T3', 'T4', 'T5', 'T6', 'T7', 'CN']
                    return (
                      <div key={item.dateStr} className="cal-header-cell" role="columnheader">
                        <span>{names[idx]}</span>
                        <small>{item.date.getDate()}/{item.date.getMonth() + 1}</small>
                      </div>
                    )
                  })}
                </div>

                <div className="cal-grid-body">
                  <div className="cal-grid-row" role="row">
                    {weekDays.map((item) => {
                      const dayFreezes = freezesByDay.get(item.dateStr) ?? []
                      const dayReqs = byDay.get(item.dateStr) ?? []
                      return (
                        <div
                          key={item.dateStr}
                          className={`cal-grid-cell cal-week-cell${item.isWeekend ? ' cal-weekend' : ''}${item.dateStr === today ? ' cal-today today' : ''}`}
                          role="gridcell"
                          aria-label={item.dateStr}
                          data-testid={`calendar-day-${item.dateStr}`}
                          onClick={() => setSelectedDay(item.dateStr)}
                        >
                          <div className="cal-day-header">
                            <span className={`cal-day-number${item.dateStr === today ? ' cal-day-number-today' : ''}`}>
                              {item.date.getDate()}
                            </span>
                          </div>
                          {dayFreezes.map((f) => renderCellFreeze(f, item.dateStr))}
                          {dayReqs.map((r) => renderCellRequest(r, item.dateStr))}
                        </div>
                      )
                    })}
                  </div>
                </div>
              </div>
            </div>
          )}

          {viewMode === 'agenda' && (
            <div className="cal-agenda-view" data-testid="calendar-agenda-view">
              {renderAgendaList()}
            </div>
          )}


          {/* Side Panel for Day Details */}
          {selectedDay && (
            <aside className="cal-side-panel" data-testid="calendar-side-panel" aria-label={`Chi tiết ngày ${selectedDay}`}>
              <div className="cal-side-panel-header">
                <div>
                  <h3>
                    {new Date(`${selectedDay}T00:00:00`).toLocaleDateString('vi-VN', {
                      weekday: 'long',
                      day: '2-digit',
                      month: '2-digit',
                      year: 'numeric',
                    })}
                  </h3>
                  <small className="muted">{selectedDay === today ? 'Hôm nay' : selectedDay}</small>
                </div>
                <button
                  type="button"
                  className="icon-button"
                  aria-label="Đóng"
                  onClick={() => {
                    setSelectedDay(null)
                    setSelectedRequestId(null)
                  }}
                >
                  <X size={18} />
                </button>
              </div>

              <div className="cal-side-panel-body">
                {/* Change Freezes */}
                {(freezesByDay.get(selectedDay) ?? []).length > 0 && (
                  <div className="cal-side-section">
                    <h4 className="cal-side-section-title">Change Freezes</h4>
                    {(freezesByDay.get(selectedDay) ?? []).map((freeze) => {
                      const scope = freeze.moduleId || freeze.systemId || 'all modules'
                      const timeRange = dayOf(freeze.startsAt) === dayOf(freeze.endsAt)
                        ? `${new Date(freeze.startsAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} – ${new Date(freeze.endsAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
                        : `${dayOf(freeze.startsAt)} ${new Date(freeze.startsAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} – ${dayOf(freeze.endsAt)} ${new Date(freeze.endsAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
                      return (
                        <div key={freeze.id} className="cal-side-freeze-card">
                          <div>
                            <strong>{freeze.name}</strong>
                            <small className="cal-side-freeze-meta">
                              {timeRange} · {freeze.environments.join(', ')} · scope: {scope}
                            </small>
                            <p className="cal-side-freeze-reason">{freeze.reason}</p>
                          </div>
                          <button
                            type="button"
                            className="secondary-button"
                            onClick={() => handleCancelFreeze(freeze.id)}
                          >
                            Cancel freeze
                          </button>
                        </div>
                      )
                    })}
                  </div>
                )}

                {/* Releases */}
                <div className="cal-side-section">
                  <h4 className="cal-side-section-title">Releases</h4>
                  {(byDay.get(selectedDay) ?? []).length === 0 && (
                    <p className="muted" style={{ fontSize: '12px' }}>
                      Không có bản phát hành nào được lên lịch cho ngày này.
                    </p>
                  )}
                  {(byDay.get(selectedDay) ?? []).map((req) => {
                    const reqWarnings = warnings.filter((w) => w.requestId === req.id)
                    const isSelected = selectedRequestId === req.id
                    return (
                      <div
                        key={req.id}
                        className={`cal-side-release-card${isSelected ? ' is-selected' : ''}`}
                        data-testid={`side-release-${req.id}`}
                      >
                        <div className="cal-side-release-header">
                          <div>
                            <strong className="cal-side-release-title">
                              {req.modules.map((m) => `${m.moduleName || m.moduleId} ${m.version}`).join(' · ')}
                            </strong>
                            <small className="muted" style={{ display: 'block', fontSize: '11px', marginTop: '2px' }}>
                              {new Date(req.scheduledFor).toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' })} · requested by {req.requestedBy}
                            </small>
                          </div>
                          <StatusPill status={req.status.replace('_', ' ')} />
                        </div>

                        <div className="cal-side-release-details">
                          {req.modules.map((m) => {
                            const targets = prodTargets[m.moduleId] ?? []
                            return (
                              <div key={m.moduleId} style={{ display: 'flex', flexDirection: 'column', gap: '3px', marginBottom: '4px' }}>
                                <div className="cal-side-detail-row">
                                  <span className="cal-side-detail-label">Module:</span>
                                  <span className="cal-side-detail-value">{m.moduleName || m.moduleId}</span>
                                </div>
                                <div className="cal-side-detail-row">
                                  <span className="cal-side-detail-label">Version:</span>
                                  <span className="cal-side-detail-value">{m.version}</span>
                                </div>
                                <div className="cal-side-detail-row">
                                  <span className="cal-side-detail-label">Environment:</span>
                                  <span className="cal-side-detail-value">
                                    prod {targets.length > 0 ? `(${targets.join(', ')})` : ''}
                                  </span>
                                </div>
                              </div>
                            )
                          })}
                          <div className="cal-side-detail-row">
                            <span className="cal-side-detail-label">Requester:</span>
                            <span className="cal-side-detail-value">{req.requestedBy}</span>
                          </div>
                          <div className="cal-side-detail-row">
                            <span className="cal-side-detail-label">Status:</span>
                            <span className="cal-side-detail-value">{req.status}</span>
                          </div>
                        </div>

                        {reqWarnings.length > 0 && (
                          <div className="cal-side-release-warnings">
                            {reqWarnings.map((w, idx) => (
                              <p key={idx} className="calendar-warning" role="note" style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                                <AlertTriangle size={14} />
                                <span>{w.message}</span>
                              </p>
                            ))}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              </div>
            </aside>
          )}
        </div>
      )}
    </section>
  )
}
