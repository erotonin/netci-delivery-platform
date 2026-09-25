import { useEffect, useState } from 'react'
import { AlertTriangle } from 'lucide-react'
import { getCiCost, type CiCost, type CiCostCounters } from './api/netciClient'
import { PageHeader } from './PortalShell'

type LoadState = 'loading' | 'ready' | 'error'

const WINDOWS = [7, 30, 90] as const

export const NO_ESTIMATE = 'no estimate (no succeeded run to estimate from)'

/** Seconds as h/m/s. Under a minute keeps the server's tenth of a second, so 0.4 s never reads as 0 s. */
export function formatSeconds(seconds: number): string {
  if (seconds < 60) return `${Number(seconds.toFixed(1))}s`
  const whole = Math.round(seconds)
  const hours = Math.floor(whole / 3600)
  const minutes = Math.floor((whole % 3600) / 60)
  const rest = whole % 60
  if (hours > 0) return `${hours}h ${minutes}m ${rest}s`
  return `${minutes}m ${rest}s`
}

/** A queue percentile is null when no run in the window was admitted -- no sample, not zero wait. */
function queueText(value: number | null): string {
  return value === null ? 'no sample' : formatSeconds(value)
}

function EstimateTag({ method }: { method: string }) {
  return <span className="estimate-tag" title={method}>estimate</span>
}

/**
 * The estimated avoided runner time. It is an estimate, so it never shares the measured
 * figure's look: an "≈" prefix, an "estimate" tag and the server's method on hover. A null
 * estimate says why it is missing -- a 0 there would read as "supersession saved nothing".
 */
function EstimatedAvoided({ counters, method }: { counters: CiCostCounters; method: string }) {
  const seconds = counters.estimatedAvoidedRunnerSeconds
  if (seconds === null) return <span className="muted-text">{NO_ESTIMATE}</span>
  return <span className="estimated-value" title={method}>≈ {formatSeconds(seconds)} <EstimateTag method={method} /></span>
}

function CostCell({ counters, method }: { counters: CiCostCounters; method: string }) {
  const cost = counters.cost
  if (cost === null) return <span className="muted-text">not configured</span>
  return (
    <span className="status-stack">
      <span>{cost.measured} {cost.currency} <small className="measured-tag">measured</small></span>
      {cost.estimatedAvoided === null
        ? <small className="muted-text">avoided: {NO_ESTIMATE}</small>
        : <small className="estimated-value" title={method}>avoided ≈ {cost.estimatedAvoided} {cost.currency} <EstimateTag method={method} /></small>}
    </span>
  )
}

function CounterCells({ counters, method, showCost }: { counters: CiCostCounters; method: string; showCost: boolean }) {
  return <>
    <td>{counters.runs}</td>
    <td>{counters.succeeded} / {counters.failed} / {counters.cancelled}</td>
    <td>
      <span className="status-stack">
        <span>{counters.superseded}</span>
        <small>before admission -- never reached CI: {counters.supersededBeforeAdmission}</small>
        <small>while building: {counters.supersededWhileBuilding}</small>
      </span>
    </td>
    <td>
      <span className="status-stack">
        <span>{formatSeconds(counters.ciSeconds)} <small className="measured-tag">measured</small></span>
        <small title="Sum of recorded stage durations; misses agent start-up and untimed stages">stages: {formatSeconds(counters.stageSeconds)}</small>
      </span>
    </td>
    <td>{queueText(counters.p50QueueSeconds)} / {queueText(counters.p95QueueSeconds)}</td>
    <td><EstimatedAvoided counters={counters} method={method} /></td>
    {showCost && <td><CostCell counters={counters} method={method} /></td>}
  </>
}

export function CiCostPage() {
  const [days, setDays] = useState<number>(30)
  const [data, setData] = useState<CiCost | null>(null)
  const [state, setState] = useState<LoadState>('loading')
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    setState('loading')
    setError('')
    getCiCost(days)
      .then((result) => {
        if (!active) return
        setData(result)
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
  }, [days])

  const windowSelector = (
    <div className="segmented" role="group" aria-label="Window">
      {WINDOWS.map((option) => (
        <button key={option} type="button" className={days === option ? 'active' : ''} aria-pressed={days === option} onClick={() => setDays(option)}>
          {option} days
        </button>
      ))}
    </div>
  )

  const estimateMethod = data?.method.estimatedAvoidedRunnerSeconds ?? ''
  const measuredMethod = data?.method.ciSeconds ?? ''
  // Pricing is one server setting, so the total's cost says whether any row can have one.
  const showCost = data !== null && data.total.cost !== null

  return (
    <section className="page">
      <PageHeader
        title="CI Cost"
        description="CI capacity the runs used (measured) and what superseding runs before admission avoided (estimated, ADR-050)."
        action={windowSelector}
      />

      {state === 'loading' && <div className="panel empty-table">Loading CI cost…</div>}
      {state === 'error' && <div className="inline-error" role="alert">Could not load CI cost: {error}</div>}
      {state === 'ready' && data && (
        <>
          <p className="muted-text" data-testid="ci-cost-window">
            {new Date(data.window.from).toLocaleString()} – {new Date(data.window.to).toLocaleString()} ({data.window.days} days)
          </p>

          {data.total.estimatedAvoidedIncomplete && (
            <div className="inline-warning" role="status" data-testid="ci-cost-incomplete">
              <AlertTriangle size={14} />
              The total estimate leaves some applications out: they have runs superseded before admission but no succeeded run to estimate from.
            </div>
          )}
          {data.total.stagesWithoutDuration > 0 && (
            <div className="inline-warning" role="status" data-testid="ci-cost-stages-without-duration">
              <AlertTriangle size={14} />
              {data.total.stagesWithoutDuration} stage(s) have no recorded duration and are not counted in the stage sum.
            </div>
          )}
          {data.total.runsWithoutCiTiming > 0 && (
            <div className="inline-warning" role="status" data-testid="ci-cost-untimed-runs">
              <AlertTriangle size={14} />
              {data.total.runsWithoutCiTiming} finished run(s) predate CI timing and are not counted in CI time.
            </div>
          )}
          {!showCost && (
            <p className="preview-notice" data-testid="ci-cost-no-price">Set NETCI_CI_PRICE_PER_RUNNER_HOUR to show cost.</p>
          )}

          <div className="panel table-panel">
            <table className="data-table ci-cost-table" data-testid="ci-cost-table">
              <thead>
                <tr className="table-head">
                  <th>Application</th>
                  <th>Runs</th>
                  <th>Succeeded / failed / cancelled</th>
                  <th>Superseded</th>
                  <th>CI time (measured)</th>
                  <th>Queue p50 / p95</th>
                  <th>Estimated avoided runner time</th>
                  {showCost && <th>Cost</th>}
                </tr>
              </thead>
              <tbody>
                {data.applications.length === 0 ? (
                  <tr>
                    <td colSpan={showCost ? 8 : 7}>
                      <div className="empty-table"><p>No application you can see has a run in this window</p></div>
                    </td>
                  </tr>
                ) : (
                  data.applications.map((application) => (
                    <tr key={application.applicationId} data-testid={`ci-cost-row-${application.applicationId}`}>
                      <td><strong>{application.name}</strong></td>
                      <CounterCells counters={application} method={estimateMethod} showCost={showCost} />
                    </tr>
                  ))
                )}
              </tbody>
              <tfoot>
                <tr data-testid="ci-cost-total" className="ci-cost-total">
                  <td><strong>Total</strong></td>
                  <CounterCells counters={data.total} method={estimateMethod} showCost={showCost} />
                </tr>
              </tfoot>
            </table>
          </div>

          <ul className="ci-cost-footnotes" data-testid="ci-cost-method">
            <li><small className="measured-tag">measured</small> CI time: {measuredMethod}.</li>
            <li><span className="estimate-tag">estimate</span> ≈ Estimated avoided runner time: {estimateMethod}.</li>
            <li>Superseded runs end cancelled, so they are also counted in Cancelled (ADR-050).</li>
          </ul>
        </>
      )}
    </section>
  )
}
