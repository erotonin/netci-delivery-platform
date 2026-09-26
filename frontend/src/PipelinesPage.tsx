import { useEffect, useId, useMemo, useRef, useState } from 'react'
import {
  AlertCircle,
  ArrowRight,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  GitBranch,
  Info,
  Lock,
  Plus,
  Search,
  X,
} from 'lucide-react'
import {
  createModulePipelineProposal,
  getModulePipeline,
  getModulePipelineStageCode,
  listModules,
  type ModulePipeline,
  type ModulePipelineCatalogStage,
  type ModulePipelineProposalCreate,
  type ModulePipelineProposalResult,
  type ModulePipelineStage,
  type ModulePipelineStageCode,
  type PortalModule,
} from './api/netciClient'
import { PageHeader, type Navigate } from './PortalShell'

const CUSTOM_ID_REGEX = /^[a-z][a-z0-9-]{1,40}$/

const CATEGORY_ORDER = ['source', 'test', 'build', 'security', 'publish', 'verify', 'custom'] as const

const CATEGORY_LABELS: Record<string, string> = {
  source: 'Source',
  test: 'Test',
  build: 'Build',
  security: 'Security',
  publish: 'Publish',
  verify: 'Verify',
  custom: 'Tùy chỉnh (Custom)',
}

type LoadState = 'loading' | 'ready' | 'error'

/**
 * Monospace code editor with synchronized line-number gutter.
 * Uses only styled <textarea> and line numbers, no npm dependencies.
 */
function MonospaceEditor({
  code,
  readOnly,
  onChange,
  path,
}: {
  code: string
  readOnly: boolean
  onChange: (val: string) => void
  path?: string
}) {
  const lineCount = Math.max(1, code.split('\n').length)
  const lineNumbers = useMemo(() => Array.from({ length: lineCount }, (_, i) => i + 1), [lineCount])
  const gutterRef = useRef<HTMLDivElement>(null)

  const handleScroll = (e: React.UIEvent<HTMLTextAreaElement>) => {
    if (gutterRef.current) {
      gutterRef.current.scrollTop = e.currentTarget.scrollTop
    }
  }

  return (
    <div
      className="monospace-editor-wrap"
      style={{
        display: 'flex',
        flexDirection: 'column',
        border: '1px solid var(--border)',
        borderRadius: '7px',
        background: '#181c24',
        overflow: 'hidden',
      }}
    >
      <div
        style={{
          height: '36px',
          borderBottom: '1px solid #2d323d',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '0 12px',
          color: '#aab1bd',
          fontSize: '11px',
        }}
      >
        <span className="mono">{path || 'script.sh'}</span>
        <span>{readOnly ? 'Chỉ đọc (Read-only)' : 'Có thể chỉnh sửa'}</span>
      </div>
      <div style={{ display: 'flex', position: 'relative', height: '360px' }}>
        <div
          ref={gutterRef}
          aria-hidden="true"
          style={{
            width: '44px',
            padding: '10px 8px 10px 0',
            background: '#161a22',
            color: '#656d78',
            textAlign: 'right',
            fontFamily: 'Consolas, Monaco, "Courier New", monospace',
            fontSize: '12px',
            lineHeight: '20px',
            userSelect: 'none',
            overflow: 'hidden',
            borderRight: '1px solid #2b303b',
          }}
        >
          {lineNumbers.map((num) => (
            <div key={num}>{num}</div>
          ))}
        </div>
        <textarea
          aria-label="Mã nguồn stage"
          readOnly={readOnly}
          value={code}
          onChange={(e) => onChange(e.target.value)}
          onScroll={handleScroll}
          spellCheck={false}
          style={{
            flex: 1,
            margin: 0,
            padding: '10px 12px',
            border: 'none',
            outline: 'none',
            background: '#181c24',
            color: '#e2e7ee',
            fontFamily: 'Consolas, Monaco, "Courier New", monospace',
            fontSize: '12px',
            lineHeight: '20px',
            resize: 'none',
            whiteSpace: 'pre',
            overflowWrap: 'normal',
            overflowX: 'auto',
            overflowY: 'auto',
          }}
        />
      </div>
    </div>
  )
}

/**
 * Pipeline designer: edit pipeline stages, code, and propose MR (ADR-057).
 */
export function PipelineDesigner({ moduleId, navigate }: { moduleId: string; navigate: Navigate }) {
  const [pipelineData, setPipelineData] = useState<ModulePipeline | null>(null)
  const [currentStages, setCurrentStages] = useState<ModulePipelineStage[]>([])
  const [catalogStages, setCatalogStages] = useState<ModulePipelineCatalogStage[]>([])
  const [selectedStageId, setSelectedStageId] = useState<string>('')
  const [catalogSearch, setCatalogSearch] = useState('')
  const [loadState, setLoadState] = useState<LoadState>('loading')
  const [loadError, setLoadError] = useState('')

  // Code cache and per-stage unsaved edits
  const [codeCache, setCodeCache] = useState<Record<string, ModulePipelineStageCode>>({})
  const [codeEdits, setCodeEdits] = useState<Record<string, string>>({})
  const [codeLoading, setCodeLoading] = useState(false)
  const [codeError, setCodeError] = useState('')

  // Custom stage creation form
  const [showCustomModal, setShowCustomModal] = useState(false)
  const [customId, setCustomId] = useState('')
  const [customName, setCustomName] = useState('')
  const [customAfter, setCustomAfter] = useState('')
  const [customFormError, setCustomFormError] = useState('')

  // Proposal submission state
  const [submitting, setSubmitting] = useState(false)
  const [proposalResult, setProposalResult] = useState<ModulePipelineProposalResult | null>(null)
  const [proposalError, setProposalError] = useState('')

  useEffect(() => {
    let active = true
    setLoadState('loading')
    setLoadError('')
    getModulePipeline(moduleId)
      .then((data) => {
        if (!active) return
        setPipelineData(data)
        setCurrentStages(data.stages)
        setCatalogStages(data.catalog)
        if (data.stages.length > 0) {
          setSelectedStageId(data.stages[0].id)
        }
        setLoadState('ready')
      })
      .catch((cause) => {
        if (!active) return
        setLoadError(cause instanceof Error ? cause.message : String(cause))
        setLoadState('error')
      })
    return () => {
      active = false
    }
  }, [moduleId])

  // Load code for selected stage when selectedStageId changes
  useEffect(() => {
    if (!selectedStageId || !moduleId) return
    if (codeCache[selectedStageId] || codeEdits[selectedStageId] !== undefined) return

    let active = true
    setCodeLoading(true)
    setCodeError('')
    getModulePipelineStageCode(moduleId, selectedStageId)
      .then((codeRes) => {
        if (!active) return
        setCodeCache((prev) => ({ ...prev, [selectedStageId]: codeRes }))
        setCodeLoading(false)
      })
      .catch((cause) => {
        if (!active) return
        setCodeError(cause instanceof Error ? cause.message : String(cause))
        setCodeLoading(false)
      })
    return () => {
      active = false
    }
  }, [selectedStageId, moduleId, codeCache, codeEdits])

  const selectedStage = currentStages.find((s) => s.id === selectedStageId)
  const isCustom = selectedStage?.kind === 'custom'
  const isEditable = Boolean(isCustom)

  const currentCode = useMemo(() => {
    if (!selectedStageId) return ''
    if (codeEdits[selectedStageId] !== undefined) {
      return codeEdits[selectedStageId]
    }
    return codeCache[selectedStageId]?.content ?? ''
  }, [selectedStageId, codeEdits, codeCache])

  const handleCodeChange = (newVal: string) => {
    if (!isEditable) return
    setCodeEdits((prev) => ({ ...prev, [selectedStageId]: newVal }))
  }

  // Append catalog stage to pipeline in template position
  const handleAddFromCatalog = (catStage: ModulePipelineCatalogStage) => {
    const existingIdx = currentStages.findIndex((s) => s.id === catStage.id)
    if (existingIdx !== -1) {
      setSelectedStageId(catStage.id)
      return
    }

    if (catStage.kind === 'builtin') {
      const templateBuiltins = catalogStages.filter((s) => s.kind === 'builtin')
      const myCatIdx = templateBuiltins.findIndex((s) => s.id === catStage.id)

      // Find first built-in in pipeline whose template index is greater than this stage
      const nextBuiltinIdx = currentStages.findIndex((s) => {
        if (s.kind !== 'builtin') return false
        const sCatIdx = templateBuiltins.findIndex((tb) => tb.id === s.id)
        return sCatIdx > myCatIdx
      })

      const newStage: ModulePipelineStage = {
        id: catStage.id,
        name: catStage.name,
        category: catStage.category,
        kind: 'builtin',
        required: catStage.required,
        after: null,
        script: null,
      }

      const nextStages = [...currentStages]
      if (nextBuiltinIdx !== -1) {
        nextStages.splice(nextBuiltinIdx, 0, newStage)
      } else {
        nextStages.push(newStage)
      }
      setCurrentStages(nextStages)
      setSelectedStageId(catStage.id)
    } else {
      const newStage: ModulePipelineStage = {
        id: catStage.id,
        name: catStage.name,
        category: catStage.category,
        kind: 'custom',
        required: catStage.required,
        after: null,
        script: null,
      }
      setCurrentStages([...currentStages, newStage])
      setSelectedStageId(catStage.id)
    }
  }

  const handleRemoveStage = (stageId: string) => {
    const stageToRemove = currentStages.find((s) => s.id === stageId)
    if (stageToRemove?.required) return

    const remaining = currentStages.filter((s) => s.id !== stageId)
    setCurrentStages(remaining)
    if (selectedStageId === stageId) {
      setSelectedStageId(remaining[0]?.id ?? '')
    }
  }

  const handleOpenCustomModal = () => {
    setCustomId('')
    setCustomName('')
    const firstBuiltin = currentStages.find((s) => s.kind === 'builtin')?.id || 'unit-test'
    setCustomAfter(firstBuiltin)
    setCustomFormError('')
    setShowCustomModal(true)
  }

  const handleCreateCustomStage = () => {
    const trimmedId = customId.trim()
    const trimmedName = customName.trim()

    if (!CUSTOM_ID_REGEX.test(trimmedId)) {
      setCustomFormError('Mã stage (id) phải bắt đầu bằng chữ thường, gồm 2-41 ký tự (chữ thường, số, dấu gạch ngang).')
      return
    }
    if (!trimmedName) {
      setCustomFormError('Vui lòng nhập tên stage.')
      return
    }
    if (!customAfter) {
      setCustomFormError('Vui lòng chọn stage chạy trước (after).')
      return
    }
    if (currentStages.some((s) => s.id === trimmedId) || catalogStages.some((s) => s.id === trimmedId)) {
      setCustomFormError('Mã stage đã tồn tại trong pipeline hoặc catalog.')
      return
    }

    const newCustom: ModulePipelineStage = {
      id: trimmedId,
      name: trimmedName,
      category: 'custom',
      kind: 'custom',
      required: false,
      after: customAfter,
      script: `.netci/stages/${trimmedId}.sh`,
    }

    // Insert after the anchor built-in stage
    const anchorIdx = currentStages.findIndex((s) => s.id === customAfter)
    let insertIdx = anchorIdx !== -1 ? anchorIdx + 1 : currentStages.length
    while (
      insertIdx < currentStages.length &&
      currentStages[insertIdx].kind === 'custom' &&
      currentStages[insertIdx].after === customAfter
    ) {
      insertIdx++
    }

    const nextStages = [...currentStages]
    nextStages.splice(insertIdx, 0, newCustom)
    setCurrentStages(nextStages)
    setSelectedStageId(trimmedId)
    setCodeEdits((prev) => ({
      ...prev,
      [trimmedId]: `#!/usr/bin/env bash\nset -euo pipefail\n\n# Custom script for ${trimmedName}\n`,
    }))
    setShowCustomModal(false)
  }

  const handleCreateProposal = async () => {
    if (!pipelineData?.repository.supportsProposals) return
    setSubmitting(true)
    setProposalError('')
    setProposalResult(null)

    try {
      const payload: ModulePipelineProposalCreate = {
        stages: currentStages.map((stage) => {
          if (stage.kind === 'custom') {
            const code =
              codeEdits[stage.id] !== undefined
                ? codeEdits[stage.id]
                : codeCache[stage.id]?.content ?? ''
            return {
              id: stage.id,
              name: stage.name,
              after: stage.after ?? null,
              code,
            }
          }
          return {
            id: stage.id,
            name: stage.name,
            after: null,
            code: null,
          }
        }),
      }
      const res = await createModulePipelineProposal(moduleId, payload)
      setProposalResult(res)
    } catch (cause) {
      const msg = cause instanceof Error ? cause.message : String(cause)
      setProposalError(msg)
    } finally {
      setSubmitting(false)
    }
  }

  const filteredCatalog = useMemo(() => {
    const q = catalogSearch.trim().toLowerCase()
    if (!q) return catalogStages
    return catalogStages.filter(
      (s) =>
        s.name.toLowerCase().includes(q) ||
        s.id.toLowerCase().includes(q) ||
        (s.description && s.description.toLowerCase().includes(q))
    )
  }, [catalogStages, catalogSearch])

  const groupedCatalog = useMemo(() => {
    const groups: Record<string, ModulePipelineCatalogStage[]> = {}
    for (const cat of CATEGORY_ORDER) {
      groups[cat] = []
    }
    for (const stage of filteredCatalog) {
      const cat = stage.category || 'custom'
      if (!groups[cat]) groups[cat] = []
      groups[cat].push(stage)
    }
    return groups
  }, [filteredCatalog])

  const builtinsInPipeline = useMemo(
    () => currentStages.filter((s) => s.kind === 'builtin'),
    [currentStages]
  )

  return (
    <section className="page pipeline-designer-page">
      <button
        type="button"
        className="back-button"
        style={{ marginBottom: '14px' }}
        onClick={() => navigate('pipelines', { moduleId: '' })}
      >
        <ChevronLeft size={16} /> Quay lại danh sách Pipelines
      </button>

      <PageHeader
        title={`Thiết kế Pipeline: ${moduleId}`}
        description="Lựa chọn các stage từ catalog, chỉnh sửa mã nguồn và đề xuất merge request lên git repository."
      />

      {loadState === 'loading' && <div className="panel empty-table">Đang tải cấu hình pipeline…</div>}
      {loadState === 'error' && (
        <div className="inline-error" role="alert">
          Không thể tải pipeline: {loadError}
        </div>
      )}

      {loadState === 'ready' && pipelineData && (
        <div
          className="designer-layout"
          style={{
            display: 'grid',
            gridTemplateColumns: '320px minmax(0, 1fr)',
            gap: '16px',
            alignItems: 'start',
          }}
        >
          {/* LEFT PANEL: Catalog (Task assistant) */}
          <aside
            className="panel catalog-panel"
            style={{
              padding: '16px',
              display: 'flex',
              flexDirection: 'column',
              gap: '14px',
              maxHeight: 'calc(100vh - 180px)',
              overflowY: 'auto',
            }}
          >
            <div>
              <h3 style={{ fontSize: '14px', fontWeight: 600 }}>Catalog Stages</h3>
              <p style={{ color: 'var(--muted)', fontSize: '11px', marginTop: '2px' }}>
                Bấm vào một stage để thêm vào pipeline.
              </p>
            </div>

            <div className="input-with-icon" style={{ minWidth: 0, width: '100%' }}>
              <Search size={14} />
              <input
                aria-label="Tìm kiếm stage trong catalog"
                placeholder="Tìm kiếm stage…"
                value={catalogSearch}
                onChange={(e) => setCatalogSearch(e.target.value)}
              />
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
              {CATEGORY_ORDER.map((catKey) => {
                const stages = groupedCatalog[catKey] || []
                if (stages.length === 0 && catKey !== 'custom') return null
                return (
                  <div key={catKey} className="catalog-group">
                    <span
                      style={{
                        display: 'block',
                        fontSize: '10px',
                        fontWeight: 700,
                        textTransform: 'uppercase',
                        letterSpacing: '.05em',
                        color: 'var(--subtle)',
                        marginBottom: '6px',
                      }}
                    >
                      {CATEGORY_LABELS[catKey] || catKey}
                    </span>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                      {stages.map((catStage) => {
                        const isInPipeline = currentStages.some((s) => s.id === catStage.id)
                        return (
                          <button
                            key={catStage.id}
                            type="button"
                            className="catalog-item-btn"
                            style={{
                              width: '100%',
                              padding: '8px 10px',
                              textAlign: 'left',
                              border: '1px solid var(--border)',
                              borderRadius: '6px',
                              background: isInPipeline ? '#f4fbf7' : '#fff',
                              borderColor: isInPipeline ? '#bbf7d0' : 'var(--border)',
                              display: 'flex',
                              justifyContent: 'space-between',
                              alignItems: 'center',
                              gap: '6px',
                            }}
                            onClick={() => handleAddFromCatalog(catStage)}
                          >
                            <div style={{ minWidth: 0 }}>
                              <strong style={{ fontSize: '12px', display: 'block' }}>{catStage.name}</strong>
                              <code className="mono" style={{ fontSize: '10px', color: 'var(--muted)' }}>
                                {catStage.id}
                              </code>
                            </div>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                              {catStage.required && (
                                <Lock size={13} style={{ color: 'var(--muted)' }} aria-label="Bắt buộc" />
                              )}
                              <Plus size={14} style={{ color: 'var(--accent)' }} />
                            </div>
                          </button>
                        )
                      })}

                      {catKey === 'custom' && (
                        <button
                          type="button"
                          className="secondary-button"
                          style={{
                            width: '100%',
                            justifyContent: 'center',
                            marginTop: '4px',
                            fontSize: '11px',
                          }}
                          onClick={handleOpenCustomModal}
                        >
                          <Plus size={13} /> + Script tùy chỉnh
                        </button>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          </aside>

          {/* RIGHT SIDE: Top cards + Center editor + Footer */}
          <main style={{ display: 'flex', flexDirection: 'column', gap: '14px', minWidth: 0 }}>
            {/* TOP: Ordered stage cards */}
            <section
              className="panel stage-cards-panel"
              style={{
                padding: '12px 16px',
                background: '#fafbfc',
                overflowX: 'auto',
              }}
            >
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '8px',
                  minWidth: 'max-content',
                }}
              >
                {currentStages.map((stage, idx) => {
                  const isSelected = stage.id === selectedStageId
                  return (
                    <div
                      key={stage.id}
                      role="button"
                      tabIndex={0}
                      className={`stage-card ${isSelected ? 'selected' : ''}`}
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: '8px',
                        padding: '8px 12px',
                        border: isSelected ? '1px solid var(--accent)' : '1px solid var(--border)',
                        borderRadius: '6px',
                        background: isSelected ? '#fffafb' : '#fff',
                        boxShadow: isSelected ? '0 0 0 2px rgba(242,5,63,.1)' : '0 1px 2px rgba(0,0,0,0.03)',
                        cursor: 'pointer',
                        userSelect: 'none',
                      }}
                      onClick={() => setSelectedStageId(stage.id)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          setSelectedStageId(stage.id)
                        }
                      }}
                    >
                      <span
                        className="stage-num"
                        style={{
                          width: '18px',
                          height: '18px',
                          borderRadius: '50%',
                          background: isSelected ? 'var(--accent)' : '#e5e7eb',
                          color: isSelected ? '#fff' : 'var(--text)',
                          fontSize: '10px',
                          display: 'grid',
                          placeItems: 'center',
                          fontWeight: 700,
                        }}
                      >
                        {idx + 1}
                      </span>
                      <strong style={{ fontSize: '12px' }}>{stage.name}</strong>
                      {stage.required ? (
                        <Lock size={12} style={{ color: 'var(--muted)' }} aria-label="Bắt buộc" />
                      ) : (
                        <button
                          type="button"
                          className="icon-button"
                          style={{ width: '20px', height: '20px', padding: 0 }}
                          aria-label={`Xóa stage ${stage.name}`}
                          onClick={(e) => {
                            e.stopPropagation()
                            handleRemoveStage(stage.id)
                          }}
                        >
                          <X size={12} />
                        </button>
                      )}
                    </div>
                  )
                })}
              </div>
            </section>

            {/* CENTER: Selected stage code editor */}
            <section className="panel editor-panel" style={{ padding: '16px' }}>
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'flex-start',
                  marginBottom: '12px',
                }}
              >
                <div>
                  <h3 style={{ fontSize: '14px', fontWeight: 600 }}>
                    {selectedStage ? selectedStage.name : 'Chưa chọn stage'}
                  </h3>
                  {selectedStage && (
                    <p style={{ color: 'var(--muted)', fontSize: '11px', marginTop: '2px' }}>
                      ID: <code className="mono">{selectedStage.id}</code> · Loại:{' '}
                      <span className={`type-badge ${selectedStage.kind === 'builtin' ? 'blue' : 'purple'}`}>
                        {selectedStage.kind === 'builtin' ? 'Built-in' : 'Custom script'}
                      </span>
                      {selectedStage.after && <span> · Chạy sau: {selectedStage.after}</span>}
                    </p>
                  )}
                </div>
              </div>

              {!isEditable && selectedStage && (
                <div
                  className="builtin-note"
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px',
                    padding: '8px 12px',
                    background: '#edf4ff',
                    border: '1px solid #bfdbfe',
                    borderRadius: '6px',
                    color: '#1e40af',
                    fontSize: '12px',
                    marginBottom: '12px',
                  }}
                >
                  <Info size={15} />
                  <span>Stage có sẵn: code do shared library quản lý</span>
                </div>
              )}

              {codeLoading && <div className="empty-table">Đang tải code của stage…</div>}
              {codeError && (
                <div className="inline-error" role="alert">
                  Không thể tải code: {codeError}
                </div>
              )}

              {!codeLoading && (
                <MonospaceEditor
                  code={currentCode}
                  readOnly={!isEditable}
                  onChange={handleCodeChange}
                  path={codeCache[selectedStageId]?.path || (isCustom ? `.netci/stages/${selectedStageId}.sh` : '')}
                />
              )}
            </section>

            {/* FOOTER: Propose merge request */}
            <footer
              className="panel designer-footer-panel"
              style={{
                padding: '16px',
                display: 'flex',
                flexDirection: 'column',
                gap: '10px',
              }}
            >
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  flexWrap: 'wrap',
                  gap: '12px',
                }}
              >
                <div>
                  <button
                    type="button"
                    className="primary-button"
                    disabled={!pipelineData.repository.supportsProposals || submitting}
                    onClick={handleCreateProposal}
                  >
                    {submitting ? 'Đang tạo merge request…' : 'Tạo merge request'}
                  </button>
                </div>

                {!pipelineData.repository.supportsProposals && (
                  <span style={{ color: 'var(--muted)', fontSize: '12px' }}>
                    Kho lưu trữ không hỗ trợ tạo merge request (chỉ hỗ trợ GitLab).
                  </span>
                )}
              </div>

              {proposalResult && (
                <div
                  className="proposal-success-box"
                  style={{
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '6px',
                    padding: '12px',
                    background: '#eaf8f2',
                    border: '1px solid #20a972',
                    borderRadius: '6px',
                    color: '#16875c',
                    fontSize: '12px',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontWeight: 600 }}>
                    <CheckCircle2 size={16} />
                    <span>Tạo merge request thành công!</span>
                  </div>
                  <div>
                    Nhánh: <code className="mono">{proposalResult.branch}</code>
                  </div>
                  <div>
                    Merge Request:{' '}
                    <a href={proposalResult.mergeRequestUrl} target="_blank" rel="noopener noreferrer">
                      {proposalResult.mergeRequestUrl}
                    </a>
                  </div>
                </div>
              )}

              {proposalError && (
                <div className="inline-error" role="alert">
                  <CircleAlert size={15} />
                  <span>{proposalError}</span>
                </div>
              )}
            </footer>
          </main>
        </div>
      )}

      {/* Modal for adding custom stage */}
      {showCustomModal && (
        <div
          className="modal-backdrop"
          role="presentation"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setShowCustomModal(false)
          }}
        >
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="custom-stage-title"
            style={{ width: '460px' }}
          >
            <header>
              <div>
                <h2 id="custom-stage-title">Thêm Script tùy chỉnh</h2>
                <p>Khai báo stage mới chạy script riêng của module.</p>
              </div>
              <button
                type="button"
                className="icon-button"
                aria-label="Đóng"
                onClick={() => setShowCustomModal(false)}
              >
                <X size={18} />
              </button>
            </header>
            <div className="modal-body" style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              <label className="field">
                <span>Mã stage (ID)</span>
                <input
                  aria-label="Mã stage"
                  placeholder="vi-du-lint"
                  value={customId}
                  onChange={(e) => {
                    setCustomId(e.target.value)
                    setCustomFormError('')
                  }}
                />
                <small style={{ color: 'var(--muted)', fontSize: '10px' }}>
                  Bắt đầu bằng chữ thường, gồm chữ thường, số, dấu gạch ngang (2-41 ký tự).
                </small>
              </label>

              <label className="field">
                <span>Tên hiển thị</span>
                <input
                  aria-label="Tên hiển thị stage"
                  placeholder="Lint & Static Analysis"
                  value={customName}
                  onChange={(e) => {
                    setCustomName(e.target.value)
                    setCustomFormError('')
                  }}
                />
              </label>

              <label className="field">
                <span>Chạy sau stage (Runs after)</span>
                <select
                  aria-label="Stage chạy trước"
                  value={customAfter}
                  onChange={(e) => setCustomAfter(e.target.value)}
                >
                  {builtinsInPipeline.map((b) => (
                    <option key={b.id} value={b.id}>
                      {b.name} ({b.id})
                    </option>
                  ))}
                </select>
              </label>

              {customFormError && (
                <div className="inline-error" role="alert">
                  <CircleAlert size={14} />
                  <span>{customFormError}</span>
                </div>
              )}
            </div>
            <footer>
              <button type="button" className="secondary-button" onClick={() => setShowCustomModal(false)}>
                Hủy
              </button>
              <button type="button" className="primary-button" onClick={handleCreateCustomStage}>
                Thêm stage
              </button>
            </footer>
          </section>
        </div>
      )}
    </section>
  )
}

/**
 * Pipelines page listing every module with its current stage sequence as chips.
 */
export function PipelinesListPage({ navigate }: { navigate: Navigate }) {
  const [modules, setModules] = useState<PortalModule[]>([])
  const [pipelinesMap, setPipelinesMap] = useState<Record<string, ModulePipelineStage[]>>({})
  const [state, setState] = useState<LoadState>('loading')
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    setState('loading')
    setError('')

    listModules()
      .then(async (modList) => {
        if (!active) return
        setModules(modList)
        setState('ready')

        // Fetch current pipeline sequence for each module
        for (const mod of modList) {
          getModulePipeline(mod.id)
            .then((pipe) => {
              if (!active) return
              setPipelinesMap((prev) => ({ ...prev, [mod.id]: pipe.stages }))
            })
            .catch(() => {
              // Ignore individual module pipeline failures in list overview
            })
        }
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
    <section className="page pipelines-list-page">
      <PageHeader
        title="Pipelines"
        description="Danh sách các module và chuỗi stage pipeline hiện tại. Bấm Thiết kế để tùy chỉnh luồng CI/CD."
      />

      {state === 'loading' && <div className="panel empty-table">Đang tải danh sách pipelines…</div>}
      {state === 'error' && (
        <div className="inline-error" role="alert">
          Không thể tải danh sách pipelines: {error}
        </div>
      )}

      {state === 'ready' && (
        <div className="panel table-panel">
          <table
            className="data-table"
            data-testid="pipelines-table"
            style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}
          >
            <thead>
              <tr className="table-head">
                <th style={{ padding: '10px 14px' }}>Module</th>
                <th style={{ padding: '10px 14px' }}>Hệ thống</th>
                <th style={{ padding: '10px 14px' }}>Runtime</th>
                <th style={{ padding: '10px 14px' }}>Chuỗi stage hiện tại</th>
                <th style={{ padding: '10px 14px', textAlign: 'right' }}>Thao tác</th>
              </tr>
            </thead>
            <tbody>
              {modules.length === 0 ? (
                <tr>
                  <td colSpan={5} style={{ textAlign: 'center', padding: '32px' }}>
                    <div className="empty-table">
                      <p>Chưa có module nào</p>
                    </div>
                  </td>
                </tr>
              ) : (
                modules.map((mod) => {
                  const stages = pipelinesMap[mod.id] || []
                  return (
                    <tr
                      key={mod.id}
                      style={{ borderBottom: '1px solid var(--border)', fontSize: '13px' }}
                    >
                      <td style={{ padding: '12px 14px' }}>
                        <strong>{mod.name}</strong>
                        <div className="mono" style={{ fontSize: '11px', color: 'var(--muted)' }}>
                          {mod.id}
                        </div>
                      </td>
                      <td style={{ padding: '12px 14px' }}>{mod.systemId}</td>
                      <td style={{ padding: '12px 14px' }}>
                        <em className="type-badge blue">{mod.runtime}</em>
                      </td>
                      <td style={{ padding: '12px 14px' }}>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', alignItems: 'center' }}>
                          {stages.length === 0 ? (
                            <span style={{ color: 'var(--muted)', fontSize: '11px' }}>
                              Đang tải chuỗi stage…
                            </span>
                          ) : (
                            stages.map((st) => (
                              <span
                                key={st.id}
                                className={`type-badge ${st.kind === 'builtin' ? 'blue' : 'purple'}`}
                                style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}
                              >
                                {st.name}
                              </span>
                            ))
                          )}
                        </div>
                      </td>
                      <td style={{ padding: '12px 14px', textAlign: 'right' }}>
                        <button
                          type="button"
                          className="secondary-button"
                          onClick={() => navigate('pipelines', { moduleId: mod.id })}
                        >
                          Thiết kế
                        </button>
                      </td>
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

/**
 * Main entry point for the Pipelines page.
 * Renders the designer if a moduleId is selected, or the list if not.
 */
export function PipelinesPage({ moduleId, navigate }: { moduleId?: string; navigate: Navigate }) {
  if (moduleId) {
    return <PipelineDesigner moduleId={moduleId} navigate={navigate} />
  }
  return <PipelinesListPage navigate={navigate} />
}
