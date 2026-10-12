import { useState } from 'react'
import { Plus, Trash2 } from 'lucide-react'
import {
  simulateReleasePlan,
  type ReleasePlanModuleInput,
  type ReleasePlanSimulationResult,
} from './api/netciClient'
import { PageHeader } from './PortalShell'

type Row = { key: number; moduleId: string; dependencies: string }

const VALID_EXAMPLE: [string, string][] = [
  ['db-migration', ''],
  ['redis', ''],
  ['auth', 'db-migration, redis'],
  ['payment', 'db-migration'],
  ['frontend', 'auth, payment'],
  ['api-gateway', 'auth'],
]

const CYCLE_EXAMPLE: [string, string][] = [
  ['a', 'b'],
  ['b', 'c'],
  ['c', 'a'],
]

let nextKey = 1
const rowsFrom = (pairs: [string, string][]): Row[] =>
  pairs.map(([moduleId, dependencies]) => ({ key: nextKey++, moduleId, dependencies }))

function toModules(rows: Row[]): ReleasePlanModuleInput[] {
  return rows
    .filter((row) => row.moduleId.trim() || row.dependencies.trim())
    .map((row) => ({
      moduleId: row.moduleId.trim(),
      dependencies: row.dependencies.split(',').map((item) => item.trim()).filter(Boolean),
    }))
}

export function ReleasePlanPage() {
  const [rows, setRows] = useState<Row[]>(() => rowsFrom(VALID_EXAMPLE))
  const [result, setResult] = useState<ReleasePlanSimulationResult | null>(null)
  // The plan the result was computed for -- the form may have been edited since, and
  // "depends on" under a chip must describe what the server actually evaluated.
  const [submitted, setSubmitted] = useState<ReleasePlanModuleInput[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function simulate(plan: Row[]) {
    const modules = toModules(plan)
    setBusy(true)
    setError('')
    setResult(null)
    try {
      const response = await simulateReleasePlan(modules)
      setSubmitted(modules)
      setResult(response)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  function loadExample(pairs: [string, string][]) {
    const next = rowsFrom(pairs)
    setRows(next)
    void simulate(next)
  }

  function updateRow(key: number, patch: Partial<Row>) {
    setRows((current) => current.map((row) => (row.key === key ? { ...row, ...patch } : row)))
  }

  const dependenciesOf = new Map(submitted.map((module) => [module.moduleId, module.dependencies]))

  return (
    <section className="page">
      <PageHeader
        title="Release Plan"
        description="Simulate how netCI would order a multi-module release. The server runs the same dependency algorithm a production request uses; nothing is stored or started."
        action={
          <div style={{ display: 'flex', gap: '8px' }}>
            <button type="button" className="secondary-button" onClick={() => loadExample(VALID_EXAMPLE)}>Valid plan</button>
            <button type="button" className="secondary-button" onClick={() => loadExample(CYCLE_EXAMPLE)}>Cycle</button>
          </div>
        }
      />

      <form
        className="panel release-plan-form"
        onSubmit={(event) => {
          event.preventDefault()
          void simulate(rows)
        }}
      >
        <div className="release-plan-row release-plan-row-head">
          <span>Module id</span>
          <span>Depends on (comma-separated module ids)</span>
          <span />
        </div>
        {rows.map((row, index) => (
          <div className="release-plan-row" key={row.key}>
            <input
              aria-label={`Module id ${index + 1}`}
              value={row.moduleId}
              placeholder="module-id"
              onChange={(event) => updateRow(row.key, { moduleId: event.target.value })}
            />
            <input
              aria-label={`Dependencies of module ${index + 1}`}
              value={row.dependencies}
              placeholder="none"
              onChange={(event) => updateRow(row.key, { dependencies: event.target.value })}
            />
            <button
              type="button"
              className="icon-button"
              aria-label={`Remove module ${index + 1}`}
              onClick={() => setRows((current) => current.filter((item) => item.key !== row.key))}
            >
              <Trash2 size={15} />
            </button>
          </div>
        ))}
        <div className="release-plan-actions">
          <button type="button" className="secondary-button" onClick={() => setRows((current) => [...current, ...rowsFrom([['', '']])])}>
            <Plus size={15} />Add module
          </button>
          <button type="submit" className="primary-button" disabled={busy}>{busy ? 'Simulating…' : 'Simulate'}</button>
        </div>
      </form>

      {error && <div className="inline-error" role="alert">Could not simulate the plan: {error}</div>}

      {result && result.valid && (
        <div className="panel release-plan-result" data-testid="release-plan-waves">
          <div className="panel-heading">
            <div>
              <h2>{result.waves.length} {result.waves.length === 1 ? 'wave' : 'waves'}</h2>
              <p>
                {result.groupedBy === 'deploymentOrder'
                  ? 'No module named a dependency, so the waves follow deployment order.'
                  : 'A wave starts once every module in the waves before it is released.'}
              </p>
            </div>
          </div>
          <div className="release-plan-waves">
            {result.waves.map((wave, index) => (
              <div className="release-plan-wave" key={index} aria-label={`Wave ${index + 1}`}>
                <h3>Wave {index + 1}</h3>
                {wave.map((moduleId) => {
                  const deps = dependenciesOf.get(moduleId) ?? []
                  return (
                    <div className="release-plan-chip" key={moduleId}>
                      <strong>{moduleId}</strong>
                      {deps.length > 0 && <small>depends on {deps.join(', ')}</small>}
                    </div>
                  )
                })}
              </div>
            ))}
          </div>
        </div>
      )}

      {result && !result.valid && (
        <div className="release-plan-blocked" role="alert" data-testid="release-plan-blocked">
          <h2>
            {result.code === 'CYCLIC_DEPENDENCY'
              ? 'Cyclic dependency detected -- release blocked'
              : 'Plan refused -- release blocked'}
          </h2>
          {result.cycle && result.cycle.length > 0 && (
            <>
              <p className="release-plan-cycle">{[...result.cycle, result.cycle[0]].join(' → ')}</p>
              <p>Each module depends on the next; no module in the loop can be released first. Remove one of these dependencies.</p>
            </>
          )}
          <p><code>{result.code}</code> {result.message}</p>
        </div>
      )}
    </section>
  )
}
