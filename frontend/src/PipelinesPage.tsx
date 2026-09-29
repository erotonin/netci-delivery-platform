import { useEffect, useState, useMemo } from 'react'
import { PageHeader, StatusPill, type Navigate } from './PortalShell'
import { usePortalFeedback } from './PortalFeedback'
import {
  NetciApiError,
  SharedPipeline,
  SharedPipelineVersion,
  PipelineBuildingBlocks,
  PortalModule,
  listSharedPipelines,
  getSharedPipeline,
  getPipelineBuildingBlocks,
  createSharedPipeline,
  proposeSharedPipelineVersion,
  approveSharedPipelineVersion,
  rejectSharedPipelineVersion,
  setModuleSharedPipeline,
  listModules,
} from './api/netciClient'
import { ChevronLeft } from 'lucide-react'
import './pipelines.css'

type ViewState = 
  | { type: 'list' } 
  | { type: 'designer', pipelineName?: string, prefillScript?: string } 
  | { type: 'detail', name: string }

function computeDiff(oldText: string, newText: string): { type: 'added' | 'removed' | 'unchanged', text: string }[] {
  const oldLines = oldText.split('\n')
  const newLines = newText.split('\n')
  
  const dp: number[][] = Array(oldLines.length + 1).fill(0).map(() => Array(newLines.length + 1).fill(0))
  for (let i = 1; i <= oldLines.length; i++) {
    for (let j = 1; j <= newLines.length; j++) {
      if (oldLines[i - 1] === newLines[j - 1]) {
        dp[i][j] = dp[i - 1][j - 1] + 1
      } else {
        dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1])
      }
    }
  }
  
  let i = oldLines.length
  let j = newLines.length
  const result: { type: 'added' | 'removed' | 'unchanged', text: string }[] = []
  
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && oldLines[i - 1] === newLines[j - 1]) {
      result.unshift({ type: 'unchanged', text: oldLines[i - 1] })
      i--
      j--
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      result.unshift({ type: 'added', text: newLines[j - 1] })
      j--
    } else if (i > 0 && (j === 0 || dp[i][j - 1] < dp[i - 1][j])) {
      result.unshift({ type: 'removed', text: oldLines[i - 1] })
      i--
    }
  }
  return result
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

export function PipelinesPage({ moduleId, navigate }: { moduleId?: string; navigate: Navigate }) {
  const [view, setView] = useState<ViewState>({ type: 'list' })
  const feedback = usePortalFeedback()

  // List view state
  const [pipelines, setPipelines] = useState<SharedPipeline[]>([])
  const [loadingPipelines, setLoadingPipelines] = useState(false)
  const [errorPipelines, setErrorPipelines] = useState('')
  
  // Module context state
  const [currentModulePipeline, setCurrentModulePipeline] = useState<string>('')
  const [savingModule, setSavingModule] = useState(false)
  
  // Designer state
  const [blocks, setBlocks] = useState<PipelineBuildingBlocks | null>(null)
  const [designerName, setDesignerName] = useState('')
  const [designerDesc, setDesignerDesc] = useState('')
  const [designerScript, setDesignerScript] = useState('')
  const [designerLoading, setDesignerLoading] = useState(false)
  const [designerError, setDesignerError] = useState('')
  const [designerSubmitError, setDesignerSubmitError] = useState('')
  const [designerSubmitting, setDesignerSubmitting] = useState(false)

  // Detail state
  const [detailPipeline, setDetailPipeline] = useState<SharedPipeline | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')
  const [rejectReason, setRejectReason] = useState('')
  const [rejectingVersion, setRejectingVersion] = useState<number | null>(null)
  const [viewScriptVersion, setViewScriptVersion] = useState<number | null>(null)

  useEffect(() => {
    if (view.type === 'list') {
      let active = true
      setLoadingPipelines(true)
      setErrorPipelines('')
      listSharedPipelines().then(res => {
        if (active) { setPipelines(res); setLoadingPipelines(false) }
      }).catch(err => {
        if (active) { setErrorPipelines(err instanceof Error ? err.message : String(err)); setLoadingPipelines(false) }
      })
      if (moduleId) {
        listModules().then((mods: PortalModule[]) => {
          if (active) {
            const mod = mods.find(m => m.id === moduleId)
            if (mod) setCurrentModulePipeline(mod.pipeline || '')
          }
        }).catch(err => { if (active) setErrorPipelines(errorText(err)) })
      }
      return () => { active = false }
    }
  }, [view.type, moduleId])

  useEffect(() => {
    if (view.type === 'designer') {
      let active = true
      setDesignerLoading(true)
      getPipelineBuildingBlocks().then(res => {
        if (active) {
          setBlocks(res)
          setDesignerLoading(false)
          setDesignerName(view.pipelineName || '')
          setDesignerDesc('')
          setDesignerSubmitError('')
          if (view.prefillScript) {
            setDesignerScript(view.prefillScript)
          } else {
            // Prefill required blocks
            const prefill = res.order
              .map(id => res.builtins.find(b => b.id === id))
              .filter((b): b is typeof res.builtins[0] => b !== undefined && b.required)
              .map(b => b.block)
              .join('\n')
            setDesignerScript(prefill ? prefill + '\n' : '')
          }
        }
      }).catch(err => {
        if (active) { setDesignerError(err instanceof Error ? err.message : String(err)); setDesignerLoading(false) }
      })
      return () => { active = false }
    }
  }, [view])

  useEffect(() => {
    if (view.type === 'detail') {
      loadDetail(view.name)
    }
  }, [view])

  // Approve, reject and attach answer with the server's reason when refused -- a second
  // administrator is required (403), or the version was decided meanwhile (409).
  const act = async (action: () => Promise<unknown>, success: string, after?: () => void) => {
    try {
      await action()
      feedback.notify(success)
      after?.()
    } catch (err) {
      feedback.notify(errorText(err), 'error')
    }
  }

  const loadDetail = (name: string) => {
    setDetailLoading(true)
    setDetailError('')
    getSharedPipeline(name).then(res => {
      setDetailPipeline(res)
      setDetailLoading(false)
    }).catch(err => {
      setDetailError(err instanceof Error ? err.message : String(err))
      setDetailLoading(false)
    })
  }

  const handleDesignerSubmit = async () => {
    setDesignerSubmitting(true)
    setDesignerSubmitError('')
    try {
      if (view.type === 'designer' && view.pipelineName) {
        await proposeSharedPipelineVersion(view.pipelineName, designerScript)
        feedback.notify('Thành công! Cần một quản trị viên khác duyệt để phiên bản có hiệu lực.')
        setView({ type: 'detail', name: view.pipelineName })
      } else {
        await createSharedPipeline({ name: designerName, description: designerDesc, script: designerScript })
        feedback.notify('Thành công! Cần một quản trị viên khác duyệt để phiên bản có hiệu lực.')
        setView({ type: 'detail', name: designerName })
      }
    } catch (err) {
      if (err instanceof NetciApiError) {
        setDesignerSubmitError(err.message)
      } else {
        setDesignerSubmitError(String(err))
      }
    } finally {
      setDesignerSubmitting(false)
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Tab') {
      e.preventDefault()
      const target = e.target as HTMLTextAreaElement
      const start = target.selectionStart
      const end = target.selectionEnd
      const newScript = designerScript.substring(0, start) + '  ' + designerScript.substring(end)
      setDesignerScript(newScript)
      setTimeout(() => {
        target.selectionStart = target.selectionEnd = start + 2
      }, 0)
    }
  }

  const parsedStages = useMemo(() => {
    if (!designerScript) return []
    const lines = designerScript.split('\n')
    const stages: { id: string, name: string, builtin: boolean }[] = []
    lines.forEach(line => {
      const match = line.match(/^# @stage (\S+) "([^"]+)"( builtin)?\s*$/)
      if (match) {
        stages.push({ id: match[1], name: match[2], builtin: !!match[3] })
      }
    })
    return stages
  }, [designerScript])

  const designerHints = useMemo(() => {
    if (!blocks) return []
    const hints: string[] = []
    const currentIds = parsedStages.map(s => s.id)
    blocks.required.forEach(req => {
      if (!currentIds.includes(req)) hints.push(`Thiếu stage bắt buộc: ${req}`)
    })
    let lastOrderIdx = -1
    parsedStages.filter(s => s.builtin).forEach(s => {
      const orderIdx = blocks.order.indexOf(s.id)
      if (orderIdx !== -1) {
        if (orderIdx < lastOrderIdx) {
          hints.push(`Sai thứ tự: ${s.id} bị đặt sai vị trí`)
        } else {
          lastOrderIdx = orderIdx
        }
      }
    })
    return hints
  }, [blocks, parsedStages])

  if (view.type === 'list') {
    return (
      <div className="pl-container">
        <PageHeader 
          title="Pipelines" 
          description="Pipeline là phần CI dùng chung (test, build, SBOM, scan, ký, publish), được duyệt bởi người thứ hai. CD (deploy) do netCI/Temporal thực hiện theo từng module. Module chọn pipeline theo tên."
        />
        {moduleId && (
          <div className="pl-card" style={{ display: 'flex', gap: '1rem', alignItems: 'center' }}>
            <span>Module {moduleId} dùng pipeline:</span>
            <select 
              value={currentModulePipeline} 
              onChange={e => setCurrentModulePipeline(e.target.value)}
              style={{ padding: '0.5rem', borderRadius: '4px', border: '1px solid var(--border)' }}
            >
              <option value="">&lt;None&gt;</option>
              {pipelines.filter(p => p.activeVersion !== null).map(p => (
                <option key={p.name} value={p.name}>{p.name}</option>
              ))}
            </select>
            <button 
              className="primary-button" 
              disabled={savingModule}
              onClick={async () => {
                setSavingModule(true)
                await act(() => setModuleSharedPipeline(moduleId, currentModulePipeline || null),
                  currentModulePipeline ? `Module ${moduleId} dùng pipeline ${currentModulePipeline} từ lần chạy tới.` : `Module ${moduleId} không còn dùng pipeline chung.`)
                setSavingModule(false)
              }}
            >Save</button>
          </div>
        )}
        
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <h3>Danh sách Pipelines</h3>
          <button className="primary-button" onClick={() => setView({ type: 'designer' })}>New pipeline</button>
        </div>

        {loadingPipelines && <div>Đang tải...</div>}
        {errorPipelines && <div style={{ color: 'red' }}>Lỗi: {errorPipelines}</div>}
        {!loadingPipelines && pipelines.length === 0 && (
          <div className="pl-card text-center">
            <p>Chưa có pipeline nào. Hãy tạo một pipeline mới.</p>
          </div>
        )}
        <div className="pl-grid">
          {pipelines.map(p => (
            <div key={p.name} className="pl-card" style={{ cursor: 'pointer' }} onClick={() => setView({ type: 'detail', name: p.name })}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'start' }}>
                <h4 style={{ margin: '0 0 0.5rem 0' }}>{p.name}</h4>
                {p.pendingVersions.length > 0 && (
                  <span style={{ fontSize: '0.75rem', background: '#fef3c7', color: '#92400e', padding: '2px 6px', borderRadius: '4px' }}>chờ duyệt v{p.pendingVersions[0]}</span>
                )}
              </div>
              <p style={{ fontSize: '0.875rem', color: '#6b7280', margin: '0 0 1rem 0' }}>{p.description}</p>
              <div style={{ marginBottom: '1rem' }}>
                <span style={{ fontSize: '0.875rem', fontWeight: 500 }}>Active version: </span>
                <span style={{ fontSize: '0.875rem' }}>{p.activeVersion !== null ? `v${p.activeVersion}` : 'chưa có bản được duyệt'}</span>
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap' }}>
                {p.stages.map(s => (
                  <span key={s.id} className={`pl-chip ${s.builtin ? 'pl-chip-builtin' : 'pl-chip-custom'}`}>{s.name}</span>
                ))}
              </div>
              <div style={{ fontSize: '0.75rem', color: '#6b7280', marginTop: '0.5rem' }}>
                Sử dụng bởi: {p.usedBy.length} modules
              </div>
            </div>
          ))}
        </div>
      </div>
    )
  }

  if (view.type === 'designer') {
    return (
      <div className="pl-container">
        <button className="link-button" onClick={() => setView(view.pipelineName ? { type: 'detail', name: view.pipelineName } : { type: 'list' })} style={{ alignSelf: 'flex-start', display: 'flex', alignItems: 'center' }}><ChevronLeft size={16}/> Quay lại</button>
        <PageHeader 
          title={view.pipelineName ? `Propose new version for ${view.pipelineName}` : 'Tạo pipeline mới'}
          description="Thiết kế kịch bản pipeline bằng cách thêm các stage từ danh sách hoặc tự viết mã bash."
        />
        
        {designerLoading && <div>Đang tải building blocks...</div>}
        {designerError && <div style={{ color: 'red' }}>Lỗi: {designerError}</div>}
        
        {blocks && (
          <div className="pl-designer">
            <div>
              <h4>Built-in stages</h4>
              <div className="pl-blocks-list">
                {blocks.builtins.map(b => {
                  const added = parsedStages.some(s => s.id === b.id)
                  return (
                    <button 
                      key={b.id} 
                      className="pl-block-item" 
                      disabled={added}
                      onClick={() => setDesignerScript(prev => prev + (prev.endsWith('\n') || !prev ? '' : '\n') + b.block + '\n')}
                    >
                      <div style={{ fontWeight: 500 }}>{b.name} {b.required && <span style={{ color: '#ef4444', fontSize: '0.75rem' }}>(Required)</span>}</div>
                      {added && <div style={{ fontSize: '0.75rem', color: '#10b981' }}>added</div>}
                    </button>
                  )
                })}
              </div>
              
              <h4 style={{ marginTop: '1.5rem' }}>Templates</h4>
              <div className="pl-blocks-list">
                {blocks.templates.map(t => (
                  <button 
                    key={t.id} 
                    className="pl-block-item"
                    onClick={() => setDesignerScript(prev => prev + (prev.endsWith('\n') || !prev ? '' : '\n') + t.block + '\n')}
                  >
                    <div style={{ fontWeight: 500 }}>{t.name}</div>
                    <div style={{ fontSize: '0.75rem', color: '#6b7280' }}>{t.description}</div>
                  </button>
                ))}
              </div>
            </div>
            
            <div className="pl-editor-col">
              {!view.pipelineName && (
                <div style={{ display: 'flex', gap: '1rem' }}>
                  <input className="pl-input" placeholder="Tên pipeline (vd: my-pipeline)" value={designerName} onChange={e => setDesignerName(e.target.value)} pattern="^[a-z][a-z0-9-]{1,62}$" />
                  <input className="pl-input" placeholder="Mô tả ngắn" value={designerDesc} onChange={e => setDesignerDesc(e.target.value)} />
                </div>
              )}
              
              <textarea 
                className="pl-textarea" 
                aria-label="Pipeline script" 
                spellCheck={false}
                value={designerScript}
                onChange={e => setDesignerScript(e.target.value)}
                onKeyDown={handleKeyDown}
              />
              
              <div className="pl-card">
                <h4>Preview stages</h4>
                <ol className="pl-preview-list">
                  {parsedStages.map((s, idx) => (
                    <li key={idx}>
                      <strong>{s.name}</strong> <code>{s.id}</code> {s.builtin && <span style={{ color: '#1d4ed8', fontSize: '0.75rem' }}>(builtin)</span>}
                    </li>
                  ))}
                </ol>
                {designerHints.length > 0 && (
                  <div className="pl-hints">
                    {designerHints.map((h, idx) => <div key={idx}>{h}</div>)}
                  </div>
                )}
              </div>
              
              {designerSubmitError && (
                <div style={{ color: 'red', padding: '1rem', background: '#fee2e2', borderRadius: '6px' }}>
                  Lỗi: {designerSubmitError}
                </div>
              )}
              
              <button className="primary-button" style={{ alignSelf: 'flex-start' }} disabled={designerSubmitting} onClick={handleDesignerSubmit}>
                Gửi để duyệt
              </button>
            </div>
          </div>
        )}
      </div>
    )
  }

  if (view.type === 'detail') {
    return (
      <div className="pl-container">
        <button className="link-button" onClick={() => setView({ type: 'list' })} style={{ alignSelf: 'flex-start', display: 'flex', alignItems: 'center' }}><ChevronLeft size={16}/> Quay lại danh sách</button>
        
        {detailLoading && <div>Đang tải...</div>}
        {detailError && <div style={{ color: 'red' }}>Lỗi: {detailError}</div>}
        
        {detailPipeline && (
          <>
            <PageHeader title={detailPipeline.name} description={detailPipeline.description} />
            <div className="pl-card">
              <div style={{ marginBottom: '1rem' }}>
                <span style={{ fontWeight: 500 }}>Active version: </span>
                <span>{detailPipeline.activeVersion !== null ? `v${detailPipeline.activeVersion}` : 'chưa có bản được duyệt'}</span>
              </div>
              <div style={{ marginBottom: '1rem' }}>
                <span style={{ fontWeight: 500 }}>Stages: </span>
                <div style={{ display: 'flex', flexWrap: 'wrap', marginTop: '0.5rem' }}>
                  {detailPipeline.stages.map(s => (
                    <span key={s.id} className={`pl-chip ${s.builtin ? 'pl-chip-builtin' : 'pl-chip-custom'}`}>{s.name}</span>
                  ))}
                </div>
              </div>
              <div>
                <span style={{ fontWeight: 500 }}>Used by: </span>
                <span style={{ color: '#6b7280' }}>{detailPipeline.usedBy.join(', ') || 'Chưa có module nào sử dụng'}</span>
              </div>
              <div style={{ marginTop: '1.5rem' }}>
                <button 
                  className="primary-button" 
                  onClick={() => {
                    const activeVer = detailPipeline.versions.find(v => v.version === detailPipeline.activeVersion) || detailPipeline.versions[0]
                    setView({ type: 'designer', pipelineName: detailPipeline.name, prefillScript: activeVer?.script || '' })
                  }}
                >
                  Propose new version
                </button>
              </div>
            </div>

            <div className="pl-card">
              <h4>Version History</h4>
              <div style={{ overflowX: 'auto' }}>
                <table className="pl-history-table">
                  <thead>
                    <tr>
                      <th>Version</th>
                      <th>Status</th>
                      <th>Author</th>
                      <th>Created</th>
                      <th>Reviewer</th>
                      <th>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detailPipeline.versions.map(v => (
                      <tr key={v.version}>
                        <td>v{v.version} <div style={{ fontSize: '10px', color: '#9ca3af', fontFamily: 'monospace' }}>{v.sha256?.substring(0, 12)}</div></td>
                        <td><StatusPill status={v.status} /></td>
                        <td>{v.createdBy}</td>
                        <td>{new Date(v.createdAt).toLocaleString()}</td>
                        <td>
                          {v.decidedBy ? `${v.decidedBy} at ${new Date(v.decidedAt!).toLocaleString()}` : '-'}
                          {v.rejectionReason && <div style={{ color: '#991b1b', fontSize: '12px' }}>Lý do: {v.rejectionReason}</div>}
                        </td>
                        <td>
                          <button className="link-button" onClick={() => setViewScriptVersion(viewScriptVersion === v.version ? null : v.version)}>View script</button>
                          {v.status === 'proposed' && (
                            <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.5rem' }}>
                              <button 
                                className="primary-button" 
                                style={{ padding: '0.25rem 0.5rem', fontSize: '0.75rem' }}
                                onClick={() => act(() => approveSharedPipelineVersion(detailPipeline.name, v.version),
                                  `Đã duyệt v${v.version}: các lần chạy mới dùng phiên bản này.`, () => loadDetail(detailPipeline.name))}
                              >Approve</button>
                              <button 
                                className="secondary-button" 
                                style={{ padding: '0.25rem 0.5rem', fontSize: '0.75rem' }}
                                onClick={() => setRejectingVersion(rejectingVersion === v.version ? null : v.version)}
                              >Reject</button>
                            </div>
                          )}
                          {rejectingVersion === v.version && (
                            <div className="pl-inline-form">
                              <input 
                                placeholder="Lý do từ chối" 
                                value={rejectReason} 
                                onChange={e => setRejectReason(e.target.value)} 
                              />
                              <button 
                                className="primary-button"
                                style={{ padding: '0.25rem 0.5rem', fontSize: '0.75rem' }}
                                disabled={!rejectReason.trim()}
                                onClick={() => act(() => rejectSharedPipelineVersion(detailPipeline.name, v.version, rejectReason.trim()),
                                  `Đã từ chối v${v.version}.`, () => {
                                    setRejectingVersion(null)
                                    setRejectReason('')
                                    loadDetail(detailPipeline.name)
                                  })}
                              >Gửi</button>
                            </div>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {viewScriptVersion !== null && (
                <div style={{ marginTop: '1.5rem' }}>
                  <h4>Script for v{viewScriptVersion}</h4>
                  {(() => {
                    const ver = detailPipeline.versions.find(v => v.version === viewScriptVersion)
                    if (!ver) return null
                    if (ver.status === 'proposed' && detailPipeline.activeVersion !== null) {
                      const activeVer = detailPipeline.versions.find(v => v.version === detailPipeline.activeVersion)
                      const diffs = computeDiff(activeVer?.script || '', ver.script || '')
                      return (
                        <pre className="pl-diff-pre">
                          {diffs.map((d, i) => (
                            <div key={i} className={d.type === 'added' ? 'pl-diff-added' : d.type === 'removed' ? 'pl-diff-removed' : ''}>
                              <span style={{ display: 'inline-block', width: '20px', userSelect: 'none', color: '#94a3b8' }}>
                                {d.type === 'added' ? '+' : d.type === 'removed' ? '-' : ' '}
                              </span>
                              {d.text}
                            </div>
                          ))}
                        </pre>
                      )
                    }
                    return (
                      <pre className="pl-diff-pre">
                        {ver.script}
                      </pre>
                    )
                  })()}
                </div>
              )}
            </div>
          </>
        )}
      </div>
    )
  }

  return null
}
