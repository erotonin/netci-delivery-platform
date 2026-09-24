import { useEffect, useState } from 'react'
import { ChevronRight } from 'lucide-react'
import { listScorecards, type ScorecardListItem } from './api/netciClient'
import { PageHeader, type Navigate } from './PortalShell'

type LoadState = 'loading' | 'ready' | 'error'

export function ScorecardsPage({ navigate }: { navigate: Navigate }) {
  const [items, setItems] = useState<ScorecardListItem[]>([])
  const [state, setState] = useState<LoadState>('loading')
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    setState('loading')
    setError('')
    listScorecards()
      .then((result) => {
        if (!active) return
        setItems(result)
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
  }, [])

  return (
    <section className="page">
      <PageHeader
        title="Scorecards"
        description="Each module's checks -- passed, failed or unknown -- against its known total."
      />

      {state === 'loading' && <div className="panel empty-table">Loading scorecards…</div>}
      {state === 'error' && <div className="inline-error" role="alert">Could not load scorecards: {error}</div>}
      {state === 'ready' && (
        <div className="panel table-panel">
          <table className="data-table" data-testid="scorecards-table" style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}>
            <thead>
              <tr className="table-head">
                <th style={{ padding: '10px 14px' }}>Module</th>
                <th style={{ padding: '10px 14px' }}>System</th>
                <th style={{ padding: '10px 14px' }}>Score</th>
                <th style={{ padding: '10px 14px' }} />
              </tr>
            </thead>
            <tbody>
              {items.length === 0 ? (
                <tr>
                  <td colSpan={4} style={{ textAlign: 'center', padding: '32px' }}>
                    <div className="empty-table">
                      <p>No module has a scorecard yet</p>
                    </div>
                  </td>
                </tr>
              ) : (
                items.map((item) => {
                  // `known` excludes checks netCI could not evaluate -- the bar and the
                  // ratio both read against what was actually checked, not the full total.
                  const percent = item.score.known > 0 ? Math.round((item.score.passed / item.score.known) * 100) : 0
                  return (
                    <tr
                      key={item.moduleId}
                      className="table-button"
                      style={{ borderBottom: '1px solid var(--border)', fontSize: '12px', cursor: 'pointer' }}
                      onClick={() => navigate('module', { systemId: item.systemId, moduleId: item.moduleId })}
                    >
                      <td style={{ padding: '10px 14px' }}><strong>{item.name}</strong></td>
                      <td style={{ padding: '10px 14px' }}>{item.systemId}</td>
                      <td style={{ padding: '10px 14px' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                          <span>{item.score.passed}/{item.score.known} ({item.score.total})</span>
                          <i className="progress"><b className="success-fill" style={{ width: `${percent}%` }} /></i>
                        </div>
                      </td>
                      <td style={{ padding: '10px 14px' }}><ChevronRight size={16} /></td>
                    </tr>
                  )
                })
              )}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
