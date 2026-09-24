import { useEffect, useState } from 'react'
import { RefreshCw, Search, ShieldAlert } from 'lucide-react'
import {
  getRunningVulnerabilities,
  getVulnerabilityExposure,
  rescanVulnerabilities,
  type VulnerabilityExposure,
} from './api/netciClient'
import { PageHeader, StatusPill } from './PortalShell'

type LoadState = 'loading' | 'ready' | 'error'

/** Format sources as friendly labels: ci -> build scan, rescan -> netCI rescan. */
function formatSources(sources: string[]): string {
  if (!sources || sources.length === 0) return ''
  return sources
    .map((source) => {
      if (source === 'ci') return 'build scan'
      if (source === 'rescan') return 'netCI rescan'
      return source
    })
    .join(', ')
}

/** Formats date using vi-VN locale. */
function formatRescanDate(iso: string): string {
  return new Date(iso).toLocaleString('vi-VN')
}

export function VulnerabilitiesPage() {
  const [searchQuery, setSearchQuery] = useState('')
  const [submittedQuery, setSubmittedQuery] = useState('')
  const [severity, setSeverity] = useState<'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW'>('HIGH')
  const [exposure, setExposure] = useState<VulnerabilityExposure | null>(null)
  const [state, setState] = useState<LoadState>('loading')
  const [error, setError] = useState('')
  const [rescanError, setRescanError] = useState('')
  const [rescanning, setRescanning] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let active = true
    setState('loading')
    setError('')
    const requestPromise = submittedQuery
      ? getVulnerabilityExposure(submittedQuery)
      : getRunningVulnerabilities(severity)

    requestPromise
      .then((data) => {
        if (!active) return
        setExposure(data)
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
  }, [submittedQuery, severity, attempt])

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault()
    setRescanError('')
    const trimmed = searchQuery.trim()
    if (trimmed === submittedQuery) {
      setAttempt((n) => n + 1)
    } else {
      setSubmittedQuery(trimmed)
    }
  }

  const handleRescan = async () => {
    setRescanError('')
    setRescanning(true)
    try {
      await rescanVulnerabilities()
      setAttempt((n) => n + 1)
    } catch (cause) {
      setRescanError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setRescanning(false)
    }
  }

  return (
    <section className="page">
      <PageHeader
        title="Vulnerabilities"
        description="Known vulnerabilities in what is running now, and what netCI could not check."
        action={
          <button className="secondary-button" onClick={handleRescan} disabled={rescanning}>
            <RefreshCw size={15} />
            Rescan now
          </button>
        }
      />

      {rescanError && (
        <div className="inline-error" role="alert">
          {rescanError}
        </div>
      )}

      <div className="panel" style={{ padding: '12px 16px', marginBottom: '16px' }}>
        <form onSubmit={handleSearch} style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
          <label className="input-with-icon" style={{ flex: '1', minWidth: '240px' }}>
            <Search size={16} />
            <input
              data-testid="vuln-search"
              placeholder="Search vulnerability ID (e.g. CVE-2024-1234)…"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
            />
          </label>
          <select
            data-testid="vuln-severity"
            value={severity}
            onChange={(e) => setSeverity(e.target.value as 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW')}
          >
            <option value="CRITICAL">CRITICAL</option>
            <option value="HIGH">HIGH</option>
            <option value="MEDIUM">MEDIUM</option>
            <option value="LOW">LOW</option>
          </select>
          <button type="submit" className="primary-button">
            Search
          </button>
          {submittedQuery && (
            <button
              type="button"
              className="secondary-button"
              onClick={() => {
                setSearchQuery('')
                setSubmittedQuery('')
              }}
            >
              Clear
            </button>
          )}
        </form>
      </div>

      {state === 'loading' && <div className="panel empty-table">Loading vulnerability exposure…</div>}
      {state === 'error' && <div className="inline-error" role="alert">Could not load vulnerabilities: {error}</div>}
      {state === 'ready' && exposure && (
        <>
          <div className="panel calendar-summary" data-testid="vuln-coverage">
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
              <span>
                <ShieldAlert size={16} />
                {exposure.coverage.inService} running artifacts · {exposure.coverage.withSbom} with SBOM · {exposure.coverage.rescanned} rescanned · {exposure.coverage.rescanFailed} rescans failed
              </span>
              <small>
                {exposure.coverage.oldestRescanAt
                  ? `last full rescan: ${formatRescanDate(exposure.coverage.oldestRescanAt)}`
                  : 'never rescanned'}
              </small>
            </div>
            {exposure.coverage.notCovered.length > 0 && (
              <div style={{ marginTop: '10px', paddingTop: '10px', borderTop: '1px solid var(--border)' }}>
                <p style={{ margin: '0 0 6px', fontWeight: 500 }}>
                  These are not covered: the answer above says nothing about them.
                </p>
                <ul style={{ margin: 0, paddingLeft: '18px', display: 'flex', flexDirection: 'column', gap: '3px' }}>
                  {exposure.coverage.notCovered.map((item, idx) => (
                    <li key={`${item.moduleId}-${item.environment}-${idx}`}>
                      {item.moduleId} · {item.environment} · {item.reason}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>

          <div className="panel table-panel">
            <table className="data-table" data-testid="vuln-affected" style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}>
              <thead>
                <tr className="table-head">
                  <th style={{ padding: '10px 14px' }}>Module</th>
                  <th style={{ padding: '10px 14px' }}>Environment</th>
                  <th style={{ padding: '10px 14px' }}>Vulnerability</th>
                  <th style={{ padding: '10px 14px' }}>Severity</th>
                  <th style={{ padding: '10px 14px' }}>Package</th>
                  <th style={{ padding: '10px 14px' }}>Found by</th>
                </tr>
              </thead>
              <tbody>
                {exposure.affected.length === 0 ? (
                  <tr>
                    <td colSpan={6} style={{ textAlign: 'center', padding: '32px' }}>
                      <div className="empty-table">
                        <p>No running artifact is known to carry it</p>
                      </div>
                    </td>
                  </tr>
                ) : (
                  exposure.affected.map((item, idx) => {
                    const fixVersionText = item.fixedVersion ? item.fixedVersion : 'no fix yet'
                    return (
                      <tr
                        key={`${item.moduleId}-${item.environment}-${item.vulnerabilityId}-${item.package}-${idx}`}
                        style={{ borderBottom: '1px solid var(--border)', fontSize: '12px' }}
                      >
                        <td style={{ padding: '10px 14px' }}><strong>{item.moduleId}</strong></td>
                        <td style={{ padding: '10px 14px' }}>{item.environment}</td>
                        <td style={{ padding: '10px 14px' }}><code>{item.vulnerabilityId}</code></td>
                        <td style={{ padding: '10px 14px' }}><StatusPill status={item.severity} /></td>
                        <td style={{ padding: '10px 14px' }}>{item.package} {item.installedVersion} → {fixVersionText}</td>
                        <td style={{ padding: '10px 14px' }}>{formatSources(item.sources)}</td>
                      </tr>
                    )
                  })
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  )
}
