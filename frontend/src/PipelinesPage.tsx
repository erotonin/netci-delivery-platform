import { useEffect, useState, useMemo } from 'react'
import { PageHeader, StatusPill, type Navigate } from './PortalShell'
import { usePortalFeedback } from './PortalFeedback'
import {
  NetciApiError,
  SharedPipeline,
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
import {
  AlertTriangle,
  ArrowRight,
  Box,
  Check,
  CheckCircle2,
  ChevronLeft,
  Code,
  Copy,
  FileCode,
  GitBranch,
  Layers,
  Plus,
  RefreshCw,
  Shield,
  ShieldCheck,
  Sliders,
  Terminal,
  Trash2,
  X,
} from 'lucide-react'
import './pipelines.css'

type ViewState = 
  | { type: 'list' } 
  | { type: 'designer', pipelineName?: string, prefillScript?: string } 
  | { type: 'detail', name: string }

type EditorTab = 'script' | 'jenkinsfile' | 'compliance'

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

function generateDeclarativeJenkinsfile(stages: Array<{ id: string; name: string; builtin: boolean }>, script: string): string {
  const stageDefs = stages.map(st => {
    if (st.builtin) {
      return `        stage('${st.name}') {
            steps {
                netciInBuilder {
                    // netCI Built-in Quality Gate (Executed in isolated container)
                    sh 'netci-builtin ${st.id}'
                }
            }
        }`
    }
    const lines = script.split('\n')
    const startIdx = lines.findIndex(l => l.startsWith(`# @stage ${st.id}`))
    const customCommands: string[] = []
    if (startIdx !== -1) {
      for (let idx = startIdx + 1; idx < lines.length; idx++) {
        if (lines[idx].startsWith('# @stage')) break
        if (lines[idx].trim() && !lines[idx].trim().startsWith('#')) {
          customCommands.push(lines[idx])
        }
      }
    }
    const bodyCode = customCommands.length ? customCommands.join('\n                    ') : 'echo "Running custom stage..."'
    return `        stage('${st.name}') {
            steps {
                netciInBuilder {
                    // Custom pipeline stage: bash commands
                    sh '''
                    ${bodyCode}
                    '''
                }
            }
        }`
  }).join('\n\n')

  return `// ==============================================================================
// netCI Production Declarative Jenkinsfile (Air-gapped VTNet Delivery)
// Pipeline Model: Shared CI Pipeline (ADR-058)
// Executed by: netCI Jenkins Controller & Kubernetes Pod Agent
// ==============================================================================
@Library('netci-shared-library@netci-0.4.2') _

pipeline {
    agent {
        kubernetes {
            inheritFrom 'netci-default-agent'
            yaml '''
              spec:
                containers:
                  - name: netci-builder
                    image: 172.17.0.1:8930/netci/agent-toolbox:2026.1
                    securityContext:
                      privileged: false
                      allowPrivilegeEscalation: false
            '''
        }
    }
    options {
        timeout(time: 60, unit: 'MINUTES')
        disableConcurrentBuilds()
        ansiColor('xterm')
    }
    environment {
        NETCI_TOOLBOX_VERSION = '2026.1'
        NETCI_SLSA_LEVEL      = '1'
        NETCI_TRIVY_DB_MIRROR = '172.17.0.1:8930/mirror/aquasec/trivy-db:2'
    }
    stages {
        stage('Checkout SCM') {
            steps {
                checkout scm
            }
        }

${stageDefs}

        stage('Publish Attestation & Evidence') {
            steps {
                netciInBuilder {
                    // Export SLSA v1 provenance, SBOM and Trivy scan evidence to netCI platform
                    sh 'python3 \${NETCI_TOOLING_DIR}/scripts/netci_callback.py evidence'
                }
            }
        }
    }
    post {
        always {
            archiveArtifacts artifacts: '.netci-out/**', allowEmptyArchive: true
            cleanWs(deleteDirs: true)
        }
        success {
            echo "==> CI Succeeded! Artifact ready for CD Deployment via Temporal."
        }
        failure {
            echo "==> CI Quality Gate Failed! Deployment blocked (100% fail-closed)."
        }
    }
}`
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
  const [editorTab, setEditorTab] = useState<EditorTab>('jenkinsfile')
  const [copiedScript, setCopiedScript] = useState(false)
  const [copiedJenkinsfile, setCopiedJenkinsfile] = useState(false)

  // Detail state
  const [detailPipeline, setDetailPipeline] = useState<SharedPipeline | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')
  const [rejectReason, setRejectReason] = useState('')
  const [rejectingVersion, setRejectingVersion] = useState<number | null>(null)
  const [viewScriptVersion, setViewScriptVersion] = useState<number | null>(null)
  const [detailTab, setDetailTab] = useState<'script' | 'jenkinsfile'>('jenkinsfile')

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
        feedback.notify('Success! Pipeline version saved and activated.')
        setView({ type: 'detail', name: view.pipelineName })
      } else {
        await createSharedPipeline({ name: designerName, description: designerDesc, script: designerScript })
        feedback.notify('Success! Pipeline created and activated.')
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

  const removeStage = (stageId: string) => {
    if (stageId === 'build' || stageId === 'publish') {
      feedback.notify('Build and Publish stages are required by policy and cannot be removed.', 'error')
      return
    }
    const lines = designerScript.split('\n')
    const filteredLines: string[] = []
    let skipping = false
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i]
      const match = line.match(/^# @stage (\S+)/)
      if (match) {
        if (match[1] === stageId) {
          skipping = true
          continue
        } else {
          skipping = false
        }
      }
      if (!skipping) {
        filteredLines.push(line)
      }
    }
    const newScript = filteredLines.join('\n').replace(/\n{3,}/g, '\n\n').trim() + '\n'
    setDesignerScript(newScript)
    feedback.notify(`Removed stage ${stageId}`)
  }

  const insertStage = (blockText: string, stageId: string, isBuiltin: boolean) => {
    const trimmedBlock = blockText.trim()
    if (!designerScript.trim()) {
      setDesignerScript(trimmedBlock + '\n')
      feedback.notify(`Added stage ${stageId}`)
      return
    }

    const lines = designerScript.split('\n')
    const builtinOrder = blocks?.order || ['unit-test', 'build', 'sbom', 'vulnerability-scan', 'sign', 'publish']

    if (isBuiltin) {
      const targetIdx = builtinOrder.indexOf(stageId)
      let insertIdx = -1
      for (let i = 0; i < lines.length; i++) {
        const m = lines[i].match(/^# @stage (\S+)/)
        if (m) {
          const exIdx = builtinOrder.indexOf(m[1])
          if (exIdx !== -1 && exIdx > targetIdx) {
            insertIdx = i
            break
          }
        }
      }
      if (insertIdx !== -1) {
        lines.splice(insertIdx, 0, trimmedBlock, '')
        setDesignerScript(lines.join('\n').replace(/\n{3,}/g, '\n\n').trim() + '\n')
        feedback.notify(`Inserted stage ${stageId}`)
        return
      }
    } else {
      // Custom stage: insert before 'publish' if publish exists, otherwise append
      let publishIdx = -1
      for (let i = 0; i < lines.length; i++) {
        if (lines[i].startsWith('# @stage publish')) {
          publishIdx = i
          break
        }
      }
      if (publishIdx !== -1) {
        lines.splice(publishIdx, 0, trimmedBlock, '')
        setDesignerScript(lines.join('\n').replace(/\n{3,}/g, '\n\n').trim() + '\n')
        feedback.notify(`Inserted custom stage ${stageId}`)
        return
      }
    }

    // Default append
    const updated = (designerScript.trimEnd() + '\n\n' + trimmedBlock).trim() + '\n'
    setDesignerScript(updated)
    feedback.notify(`Added stage ${stageId}`)
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
      if (!currentIds.includes(req)) hints.push(`Missing required stage: ${req}`)
    })
    let lastOrderIdx = -1
    parsedStages.filter(s => s.builtin).forEach(s => {
      const orderIdx = blocks.order.indexOf(s.id)
      if (orderIdx !== -1) {
        if (orderIdx < lastOrderIdx) {
          hints.push(`Incorrect order: ${s.id} is misplaced`)
        } else {
          lastOrderIdx = orderIdx
        }
      }
    })
    return hints
  }, [blocks, parsedStages])

  const declarativeJenkinsfile = useMemo(() => {
    return generateDeclarativeJenkinsfile(parsedStages, designerScript)
  }, [parsedStages, designerScript])

  const handleCopyScript = () => {
    navigator.clipboard.writeText(designerScript)
    setCopiedScript(true)
    setTimeout(() => setCopiedScript(false), 2000)
  }

  const handleCopyJenkinsfile = () => {
    navigator.clipboard.writeText(declarativeJenkinsfile)
    setCopiedJenkinsfile(true)
    setTimeout(() => setCopiedJenkinsfile(false), 2000)
  }

  const handleApplyFullCiTemplate = () => {
    const fullTemplate = [
      '# @stage unit-test "Unit Tests" builtin',
      '# [CI Gate 1] Run unit tests and publish coverage report',
      'netci-builtin unit-test',
      '',
      '# @stage build "Build" builtin',
      '# [CI Gate 2] Package container image and compute immutable SHA256 digest',
      'netci-builtin build',
      '',
      '# @stage sbom "Generate SBOM" builtin',
      '# [CI Gate 3] Export CycloneDX JSON software bill of materials',
      'netci-builtin sbom',
      '',
      '# @stage vulnerability-scan "Vulnerability Scan" builtin',
      '# [CI Gate 4] Scan for vulnerabilities offline using air-gapped Trivy CVE DB',
      'netci-builtin vulnerability-scan',
      '',
      '# @stage sign "Sign Artifact" builtin',
      '# [CI Gate 5] Cryptographically sign provenance attestations (SLSA v1) with Cosign',
      'netci-builtin sign',
      '',
      '# @stage publish "Publish Artifact" builtin',
      '# [CI Gate 6] Push signed container image to Harbor Registry',
      'netci-builtin publish',
      '',
    ].join('\n')
    setDesignerScript(fullTemplate)
    feedback.notify('SLSA v1 standard CI template loaded!')
  }

  if (view.type === 'list') {
    return (
      <div className="pl-container">
        <PageHeader 
          title="Shared Pipelines" 
          description="Manage and design shared CI pipeline workflows (test, build, SBOM, scan, sign, publish). CD (deployment) is handled per module by netCI Temporal workers."
          action={
            <button className="primary-button" onClick={() => setView({ type: 'designer' })}>
              <Plus size={16} style={{ marginRight: '6px' }} />
              New pipeline
            </button>
          }
        />

        {moduleId && (
          <div className="pl-card" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', background: '#eff6ff', borderColor: '#bfdbfe' }}>
            <div>
              <strong style={{ color: '#1e40af' }}>Module {moduleId} shared pipeline:</strong>
              <div style={{ fontSize: '0.875rem', color: '#1d4ed8' }}>
                {currentModulePipeline ? `Current pipeline: ${currentModulePipeline}` : 'No shared pipeline assigned (using module-specific stage catalog).'}
              </div>
            </div>
            <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
              <select 
                value={currentModulePipeline} 
                onChange={e => setCurrentModulePipeline(e.target.value)}
                style={{ padding: '0.4rem', borderRadius: '4px', border: '1px solid #93c5fd' }}
              >
                <option value="">-- No shared pipeline --</option>
                {pipelines.filter(p => p.activeVersion !== null).map(p => (
                  <option key={p.name} value={p.name}>{p.name} (v{p.activeVersion})</option>
                ))}
              </select>
              <button 
                className="primary-button"
                disabled={savingModule}
                onClick={async () => {
                  setSavingModule(true)
                  try {
                    await setModuleSharedPipeline(moduleId, currentModulePipeline || null)
                    feedback.notify('Updated pipeline for module!')
                  } catch (err) {
                    feedback.notify(errorText(err), 'error')
                  } finally {
                    setSavingModule(false)
                  }
                }}
              >
                {savingModule ? 'Saving…' : 'Save'}
              </button>
            </div>
          </div>
        )}

        {loadingPipelines && <div>Loading pipelines...</div>}
        {errorPipelines && <div style={{ color: 'red' }}>Error: {errorPipelines}</div>}

        <div className="pl-grid">
          {pipelines.map(p => (
            <div key={p.name} className="pl-card" style={{ display: 'flex', flexDirection: 'column', justifyContent: 'space-between' }}>
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '0.5rem' }}>
                  <h3 style={{ margin: 0, fontSize: '1.125rem' }}>
                    <button className="link-button" onClick={() => setView({ type: 'detail', name: p.name })} style={{ fontSize: '1.125rem', fontWeight: 600 }}>
                      {p.name}
                    </button>
                  </h3>
                  {p.activeVersion !== null ? (
                    <span className="pl-chip pl-chip-builtin" style={{ margin: 0 }}>v{p.activeVersion}</span>
                  ) : (
                    <span style={{ fontSize: '0.75rem', color: '#dc2626', background: '#fee2e2', padding: '2px 8px', borderRadius: '12px' }}>no approved version</span>
                  )}
                </div>
                <p style={{ fontSize: '0.875rem', color: '#6b7280', margin: '0 0 1rem 0' }}>{p.description || 'No description'}</p>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', marginBottom: '1rem' }}>
                  {p.stages.map(s => (
                    <span key={s.id} className={`pl-chip ${s.builtin ? 'pl-chip-builtin' : 'pl-chip-custom'}`} style={{ margin: 0 }}>
                      {s.name}
                    </span>
                  ))}
                </div>
              </div>
              <div style={{ borderTop: '1px solid var(--border, #e5e7eb)', paddingTop: '0.75rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: '0.75rem', color: '#6b7280' }}>
                <span>Used by: {p.usedBy.length} modules</span>
                {p.pendingVersions.length > 0 && (
                  <span style={{ color: '#d97706', fontWeight: 500 }}>pending approval v{p.pendingVersions.join(', v')}</span>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    )
  }

  if (view.type === 'designer') {
    const hasBuild = parsedStages.some(s => s.id === 'build')
    const hasSbom = parsedStages.some(s => s.id === 'sbom')
    const hasScan = parsedStages.some(s => s.id === 'vulnerability-scan')
    const hasSign = parsedStages.some(s => s.id === 'sign')
    const hasPublish = parsedStages.some(s => s.id === 'publish')

    return (
      <div className="pl-container">
        <button className="link-button" onClick={() => setView(view.pipelineName ? { type: 'detail', name: view.pipelineName } : { type: 'list' })} style={{ alignSelf: 'flex-start', display: 'flex', alignItems: 'center' }}>
          <ChevronLeft size={16}/> Back
        </button>

        <PageHeader 
          title={view.pipelineName ? `Propose new version for ${view.pipelineName}` : 'Create New Pipeline'}
          description="Design automated CI pipeline workflows. Select stages from the building block catalog or customize shell execution commands in Jenkins."
        />

        {/* Visual Pipeline Flow Ribbon */}
        <div className="pl-flow-banner">
          <div className="pl-flow-header">
            <h4>
              <GitBranch size={16} />
              Pipeline Execution Flow Preview
            </h4>
            <span style={{ fontSize: '12px', color: 'var(--muted, #64748b)' }}>
              {parsedStages.length} configured stage(s)
            </span>
          </div>

          <div className="pl-flow-track">
            <div className="pl-flow-node start">
              <Code size={14} />
              <span>Checkout SCM</span>
            </div>

            <ArrowRight size={14} className="pl-flow-arrow" />

            {parsedStages.map((s, idx) => {
              const isRequired = s.id === 'build' || s.id === 'publish'
              return (
                <div key={idx} style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                  <div className={`pl-flow-node ${isRequired ? 'required' : s.builtin ? 'builtin' : 'custom'}`} style={{ position: 'relative' }}>
                    {s.id === 'unit-test' && <CheckCircle2 size={14} />}
                    {s.id === 'build' && <Box size={14} />}
                    {s.id === 'sbom' && <Layers size={14} />}
                    {s.id === 'vulnerability-scan' && <Shield size={14} />}
                    {s.id === 'sign' && <ShieldCheck size={14} />}
                    {s.id === 'publish' && <Box size={14} />}
                    {!['unit-test', 'build', 'sbom', 'vulnerability-scan', 'sign', 'publish'].includes(s.id) && <Terminal size={14} />}
                    <span>{s.name}</span>
                    <small style={{ fontSize: '10px', opacity: 0.8, textTransform: 'uppercase' }}>
                      {isRequired ? 'Required' : s.builtin ? 'CI Gate' : 'Custom'}
                    </small>
                    {!isRequired && (
                      <button
                        type="button"
                        aria-label={`Remove stage ${s.name}`}
                        title={`Remove ${s.name} from pipeline`}
                        onClick={() => removeStage(s.id)}
                        style={{
                          marginLeft: '6px',
                          background: 'rgba(239, 68, 68, 0.15)',
                          border: 'none',
                          borderRadius: '50%',
                          width: '18px',
                          height: '18px',
                          display: 'inline-flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          cursor: 'pointer',
                          color: '#ef4444',
                          fontWeight: 'bold',
                          fontSize: '13px',
                          lineHeight: '1',
                          padding: 0,
                        }}
                      >
                        ×
                      </button>
                    )}
                  </div>
                  <ArrowRight size={14} className="pl-flow-arrow" />
                </div>
              )
            })}

            <div className="pl-flow-node cd" title="CD Deployment coordinated by netCI Temporal Worker">
              <Check size={14} />
              <span>CD Deployment</span>
              <small style={{ fontSize: '10px', background: '#10b981', color: '#fff', padding: '1px 4px', borderRadius: '3px' }}>Temporal</small>
            </div>
          </div>
        </div>
        
        {designerLoading && <div>Loading building blocks...</div>}
        {designerError && <div style={{ color: 'red' }}>Error: {designerError}</div>}
        
        {blocks && (
          <div className="pl-designer">
            {/* Left Sidebar: Stage Library */}
            <div className="pl-sidebar-col">
              <div className="pl-card">
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75rem' }}>
                  <h4 style={{ margin: 0, fontSize: '0.9rem', display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <ShieldCheck size={16} color="#2563eb" />
                    Built-in stages
                  </h4>
                  <button 
                    type="button" 
                    className="pl-editor-btn" 
                    onClick={handleApplyFullCiTemplate}
                    title="Load complete SLSA v1 standard template"
                  >
                    ⚡ Load Standard Template
                  </button>
                </div>
                <p style={{ margin: '0 0 10px 0', fontSize: '11.5px', color: 'var(--muted, #64748b)' }}>
                  Standard quality & security gates executed in netCI build toolbox:
                </p>

                <div className="pl-blocks-list">
                  {blocks.builtins.map(b => {
                    const added = parsedStages.some(s => s.id === b.id)
                    const isRequired = b.required || b.id === 'build' || b.id === 'publish'
                    if (added && isRequired) {
                      return (
                        <button
                          key={b.id}
                          type="button"
                          className="pl-block-item"
                          disabled={true}
                          style={{ cursor: 'not-allowed', background: '#f8fafc' }}
                        >
                          <div className="pl-block-title">
                            <span>{b.name}</span>
                            <span style={{ color: '#ef4444', fontSize: '10.5px' }}>(Required)</span>
                          </div>
                          <div className="pl-block-desc">
                            {b.id === 'unit-test' && 'Run unit tests & code coverage analysis'}
                            {b.id === 'build' && 'Compile source code & package Docker container'}
                            {b.id === 'sbom' && 'Export CycloneDX SBOM dependency inventory'}
                            {b.id === 'vulnerability-scan' && 'Scan for vulnerabilities offline using air-gapped Trivy DB'}
                            {b.id === 'sign' && 'Cryptographically sign provenance attestations (SLSA v1) via Cosign'}
                            {b.id === 'publish' && 'Publish container image & signatures to Harbor Registry'}
                          </div>
                          <div style={{ fontSize: '11px', color: '#10b981', marginTop: '4px', fontWeight: 600, display: 'flex', alignItems: 'center', gap: '4px' }}>
                            <Check size={12} /> added
                          </div>
                        </button>
                      )
                    }
                    if (added) {
                      return (
                        <div key={b.id} className="pl-block-item" style={{ cursor: 'default', background: '#f8fafc' }}>
                          <div className="pl-block-title">
                            <span>{b.name}</span>
                          </div>
                          <div className="pl-block-desc">
                            {b.id === 'unit-test' && 'Run unit tests & code coverage analysis'}
                            {b.id === 'build' && 'Compile source code & package Docker container'}
                            {b.id === 'sbom' && 'Export CycloneDX SBOM dependency inventory'}
                            {b.id === 'vulnerability-scan' && 'Scan for vulnerabilities offline using air-gapped Trivy DB'}
                            {b.id === 'sign' && 'Cryptographically sign provenance attestations (SLSA v1) via Cosign'}
                            {b.id === 'publish' && 'Publish container image & signatures to Harbor Registry'}
                          </div>
                          <div style={{ marginTop: '8px', display: 'flex', gap: '8px', alignItems: 'center' }}>
                            <div style={{ fontSize: '11px', color: '#10b981', fontWeight: 600, display: 'flex', alignItems: 'center', gap: '4px' }}>
                              <Check size={12} /> added
                            </div>
                            <button
                              type="button"
                              className="secondary-button"
                              style={{ fontSize: '11px', padding: '2px 8px', color: '#ef4444', borderColor: '#fca5a5', display: 'inline-flex', alignItems: 'center', gap: '4px' }}
                              onClick={() => removeStage(b.id)}
                            >
                              <Trash2 size={11} /> Remove
                            </button>
                          </div>
                        </div>
                      )
                    }
                    return (
                      <button
                        key={b.id}
                        type="button"
                        className="pl-block-item"
                        disabled={false}
                        onClick={() => insertStage(b.block, b.id, true)}
                      >
                        <div className="pl-block-title">
                          <span>{b.name}</span>
                          {isRequired && <span style={{ color: '#ef4444', fontSize: '10.5px' }}>(Required)</span>}
                        </div>
                        <div className="pl-block-desc">
                          {b.id === 'unit-test' && 'Run unit tests & code coverage analysis'}
                          {b.id === 'build' && 'Compile source code & package Docker container'}
                          {b.id === 'sbom' && 'Export CycloneDX SBOM dependency inventory'}
                          {b.id === 'vulnerability-scan' && 'Scan for vulnerabilities offline using air-gapped Trivy DB'}
                          {b.id === 'sign' && 'Cryptographically sign provenance attestations (SLSA v1) via Cosign'}
                          {b.id === 'publish' && 'Publish container image & signatures to Harbor Registry'}
                        </div>
                        <div style={{ fontSize: '11px', color: '#2563eb', marginTop: '6px', fontWeight: 500, display: 'flex', alignItems: 'center', gap: '4px' }}>
                          <Plus size={12} /> Add to pipeline
                        </div>
                      </button>
                    )
                  })}
                </div>
                
                <h4 style={{ marginTop: '1.5rem', marginBottom: '0.75rem', fontSize: '0.9rem', display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <Terminal size={16} color="#7c3aed" />
                  Templates
                </h4>
                <p style={{ margin: '0 0 10px 0', fontSize: '11.5px', color: 'var(--muted, #64748b)' }}>
                  Custom verification steps (executed in sandbox without privileged credentials):
                </p>

                <div className="pl-blocks-list">
                  {blocks.templates.map(t => {
                    const added = parsedStages.some(s => s.id === t.id)
                    if (added) {
                      return (
                        <div key={t.id} className="pl-block-item" style={{ cursor: 'default', background: '#f8fafc' }}>
                          <div className="pl-block-title">
                            <span>{t.name}</span>
                          </div>
                          <div className="pl-block-desc">{t.description}</div>
                          <div style={{ marginTop: '8px', display: 'flex', gap: '8px', alignItems: 'center' }}>
                            <div style={{ fontSize: '11px', color: '#10b981', fontWeight: 600, display: 'flex', alignItems: 'center', gap: '4px' }}>
                              <Check size={12} /> added
                            </div>
                            <button
                              type="button"
                              className="secondary-button"
                              style={{ fontSize: '11px', padding: '2px 8px', color: '#ef4444', borderColor: '#fca5a5', display: 'inline-flex', alignItems: 'center', gap: '4px' }}
                              onClick={() => removeStage(t.id)}
                            >
                              <Trash2 size={11} /> Remove
                            </button>
                          </div>
                        </div>
                      )
                    }
                    return (
                      <button
                        key={t.id}
                        type="button"
                        className="pl-block-item"
                        onClick={() => insertStage(t.block, t.id, false)}
                      >
                        <div className="pl-block-title">
                          <span>{t.name}</span>
                          <Plus size={13} color="#7c3aed" />
                        </div>
                        <div className="pl-block-desc">{t.description}</div>
                      </button>
                    )
                  })}
                </div>
              </div>
            </div>
            
            {/* Right Column: Code Editor & Previews */}
            <div className="pl-editor-col">
              {!view.pipelineName && (
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
                  <div>
                    <label style={{ display: 'block', fontSize: '12px', fontWeight: 600, marginBottom: '4px' }}>Pipeline Name</label>
                    <input className="pl-input" placeholder="Pipeline name (e.g. my-pipeline)" value={designerName} onChange={e => setDesignerName(e.target.value)} pattern="^[a-z][a-z0-9-]{1,62}$" />
                  </div>
                  <div>
                    <label style={{ display: 'block', fontSize: '12px', fontWeight: 600, marginBottom: '4px' }}>Description</label>
                    <input className="pl-input" placeholder="Short description" value={designerDesc} onChange={e => setDesignerDesc(e.target.value)} />
                  </div>
                </div>
              )}

              {/* Editor Tabs Switcher */}
              <div className="pl-editor-tabs">
                <button 
                  type="button" 
                  className={`pl-tab-button ${editorTab === 'jenkinsfile' ? 'active' : ''}`}
                  onClick={() => setEditorTab('jenkinsfile')}
                >
                  <Sliders size={15} />
                  <span>Jenkinsfile (Declarative Pipeline)</span>
                </button>
                <button 
                  type="button" 
                  className={`pl-tab-button ${editorTab === 'script' ? 'active' : ''}`}
                  onClick={() => setEditorTab('script')}
                >
                  <FileCode size={15} />
                  <span>pipeline.sh (CI Script)</span>
                </button>
                <button 
                  type="button" 
                  className={`pl-tab-button ${editorTab === 'compliance' ? 'active' : ''}`}
                  onClick={() => setEditorTab('compliance')}
                >
                  <ShieldCheck size={15} />
                  <span>SLSA & Quality Gate Compliance Audit</span>
                </button>
              </div>

              {/* Editor Shell */}
              <div className="pl-editor-shell">
                <div className="pl-editor-header">
                  <div className="pl-editor-title">
                    {editorTab === 'jenkinsfile' && (
                      <>
                        <Sliders size={14} color="#a78bfa" />
                        <span>Jenkinsfile</span>
                        <span className="file-badge" style={{ background: '#7c3aed' }}>DECLARATIVE GROOVY</span>
                      </>
                    )}
                    {editorTab === 'script' && (
                      <>
                        <FileCode size={14} color="#60a5fa" />
                        <span>pipeline.sh</span>
                        <span className="file-badge">BASH / NETCI RUNNER</span>
                      </>
                    )}
                    {editorTab === 'compliance' && (
                      <>
                        <ShieldCheck size={14} color="#34d399" />
                        <span>Security & Policy Gate Audit</span>
                      </>
                    )}
                  </div>

                  <div className="pl-editor-actions">
                    {editorTab === 'jenkinsfile' && (
                      <>
                        <span style={{ fontSize: '11px', color: '#a78bfa', marginRight: '6px' }}>
                          ⚡ Auto-generated from selected stages
                        </span>
                        <button type="button" className="pl-editor-btn" onClick={handleCopyJenkinsfile}>
                          {copiedJenkinsfile ? <Check size={12} color="#10b981" /> : <Copy size={12} />}
                          {copiedJenkinsfile ? 'Copied Jenkinsfile' : 'Copy Jenkinsfile'}
                        </button>
                      </>
                    )}
                    {editorTab === 'script' && (
                      <>
                        <span style={{ fontSize: '11px', color: '#94a3b8' }}>
                          {designerScript.split('\n').length} lines · {parsedStages.length} stage(s)
                        </span>
                        <button type="button" className="pl-editor-btn" onClick={handleCopyScript}>
                          {copiedScript ? <Check size={12} color="#10b981" /> : <Copy size={12} />}
                          {copiedScript ? 'Copied' : 'Copy'}
                        </button>
                        <button type="button" className="pl-editor-btn" onClick={() => setDesignerScript('')}>
                          Clear
                        </button>
                      </>
                    )}
                  </div>
                </div>

                <textarea 
                  className="pl-textarea" 
                  aria-label="Pipeline script" 
                  spellCheck={false}
                  value={designerScript}
                  onChange={e => setDesignerScript(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder="# CI pipeline execution script running on runner..."
                  style={{ display: editorTab === 'script' ? 'block' : 'none' }}
                />

                {editorTab === 'jenkinsfile' && (
                  <pre className="pl-jenkinsfile-pre">
                    {declarativeJenkinsfile}
                  </pre>
                )}

                {editorTab === 'compliance' && (
                  <div style={{ padding: '1.25rem', color: '#e2e8f0' }}>
                    <h4 style={{ margin: '0 0 10px 0', fontSize: '14px', color: '#fff' }}>
                      Quality Gate Compliance Verification
                    </h4>
                    <p style={{ margin: '0 0 14px 0', fontSize: '12px', color: '#94a3b8' }}>
                      netCI enforces a strict 100% Fail-closed policy. Builds are promoted only when all mandatory quality gates pass:
                    </p>

                    <div className="pl-compliance-grid">
                      <div className="pl-compliance-card" style={{ borderColor: hasBuild ? '#10b981' : '#f87171' }}>
                        <strong style={{ color: hasBuild ? '#059669' : '#dc2626' }}>
                          {hasBuild ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}
                          Compile & Build (Required)
                        </strong>
                        <small>{hasBuild ? 'Attaches immutable SHA-256 digest' : 'Required build stage missing'}</small>
                      </div>

                      <div className="pl-compliance-card" style={{ borderColor: hasSbom ? '#10b981' : '#cbd5e1' }}>
                        <strong style={{ color: hasSbom ? '#059669' : '#64748b' }}>
                          {hasSbom ? <CheckCircle2 size={15} /> : <Layers size={15} />}
                          CycloneDX SBOM (Optional)
                        </strong>
                        <small>{hasSbom ? 'Inventories software dependencies' : 'SBOM stage not configured'}</small>
                      </div>

                      <div className="pl-compliance-card" style={{ borderColor: hasScan ? '#10b981' : '#cbd5e1' }}>
                        <strong style={{ color: hasScan ? '#059669' : '#64748b' }}>
                          {hasScan ? <CheckCircle2 size={15} /> : <Shield size={15} />}
                          Trivy CVE Scan (Optional)
                        </strong>
                        <small>{hasScan ? 'Verified against air-gapped CVE database' : 'Vulnerability scan not configured'}</small>
                      </div>

                      <div className="pl-compliance-card" style={{ borderColor: hasSign ? '#10b981' : '#cbd5e1' }}>
                        <strong style={{ color: hasSign ? '#059669' : '#64748b' }}>
                          {hasSign ? <CheckCircle2 size={15} /> : <ShieldCheck size={15} />}
                          SLSA v1 Provenance (Optional)
                        </strong>
                        <small>{hasSign ? 'Attests cryptographic provenance signature' : 'Cosign signing not configured'}</small>
                      </div>

                      <div className="pl-compliance-card" style={{ borderColor: hasPublish ? '#10b981' : '#f87171' }}>
                        <strong style={{ color: hasPublish ? '#059669' : '#dc2626' }}>
                          {hasPublish ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}
                          Publish Artifact (Required)
                        </strong>
                        <small>{hasPublish ? 'Stores secure OCI image in registry' : 'Required publish stage missing'}</small>
                      </div>
                    </div>
                  </div>
                )}
              </div>
              
              {/* Preview stages card */}
              <div className="pl-card">
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
                  <h4 style={{ margin: 0, fontSize: '0.875rem' }}>Execution Stage Order ({parsedStages.length})</h4>
                  <span style={{ fontSize: '11px', color: 'var(--muted, #64748b)' }}>Sequential execution in build toolbox</span>
                </div>
                <ol className="pl-preview-list">
                  {parsedStages.map((s, idx) => {
                    const isRequired = s.id === 'build' || s.id === 'publish'
                    return (
                      <li key={idx} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <div>
                          <strong>{s.name}</strong> <code>{s.id}</code>{' '}
                          {isRequired ? (
                            <span style={{ color: '#ef4444', fontSize: '0.75rem', fontWeight: 600 }}>(required)</span>
                          ) : s.builtin ? (
                            <span style={{ color: '#1d4ed8', fontSize: '0.75rem', fontWeight: 600 }}>(builtin)</span>
                          ) : (
                            <span style={{ color: '#7c3aed', fontSize: '0.75rem', fontWeight: 600 }}>(custom)</span>
                          )}
                        </div>
                        {!isRequired && (
                          <button
                            type="button"
                            className="secondary-button"
                            style={{ fontSize: '10.5px', padding: '1px 6px', color: '#ef4444', borderColor: '#fca5a5', display: 'inline-flex', alignItems: 'center', gap: '3px' }}
                            onClick={() => removeStage(s.id)}
                            title={`Remove stage ${s.name}`}
                          >
                            <Trash2 size={10} /> Remove
                          </button>
                        )}
                      </li>
                    )
                  })}
                </ol>
                {designerHints.length > 0 && (
                  <div className="pl-hints">
                    {designerHints.map((h, idx) => <div key={idx} style={{ display: 'flex', alignItems: 'center', gap: '6px' }}><AlertTriangle size={14} />{h}</div>)}
                  </div>
                )}
              </div>
              
              {designerSubmitError && (
                <div style={{ color: 'red', padding: '1rem', background: '#fee2e2', borderRadius: '6px', fontSize: '13px' }}>
                  Error: {designerSubmitError}
                </div>
              )}
              
              <div style={{ display: 'flex', gap: '10px', alignItems: 'center' }}>
                <button className="primary-button" style={{ alignSelf: 'flex-start' }} disabled={designerSubmitting} onClick={handleDesignerSubmit}>
                  {designerSubmitting ? 'Saving…' : view.pipelineName ? 'Save pipeline version' : 'Create pipeline'}
                </button>
                <small style={{ color: 'var(--muted, #64748b)', fontSize: '12px' }}>
                  * Pipeline takes effect immediately and is ready to be used by all modules.
                </small>
              </div>
            </div>
          </div>
        )}
      </div>
    )
  }

  if (view.type === 'detail') {
    return (
      <div className="pl-container">
        <button className="link-button" onClick={() => setView({ type: 'list' })} style={{ alignSelf: 'flex-start', display: 'flex', alignItems: 'center' }}>
          <ChevronLeft size={16}/> Back to list
        </button>
        
        {detailLoading && <div>Loading...</div>}
        {detailError && <div style={{ color: 'red' }}>Error: {detailError}</div>}
        
        {detailPipeline && (
          <>
            <PageHeader title={detailPipeline.name} description={detailPipeline.description || 'No description'} />

            {/* Visual Pipeline Flow Ribbon for Active Pipeline */}
            <div className="pl-flow-banner">
              <div className="pl-flow-header">
                <h4>
                  <GitBranch size={16} />
                  Stage Execution Flow for Pipeline {detailPipeline.name}
                </h4>
                <span style={{ fontSize: '12px', color: 'var(--muted, #64748b)' }}>
                  Active version: {detailPipeline.activeVersion !== null ? `v${detailPipeline.activeVersion}` : 'None'}
                </span>
              </div>

              <div className="pl-flow-track">
                <div className="pl-flow-node start">
                  <Code size={14} />
                  <span>Checkout SCM</span>
                </div>

                <ArrowRight size={14} className="pl-flow-arrow" />

                {detailPipeline.stages.map((s, idx) => (
                  <div key={idx} style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                    <div className={`pl-flow-node ${s.builtin ? 'required' : 'custom'}`}>
                      {s.id === 'unit-test' && <CheckCircle2 size={14} />}
                      {s.id === 'build' && <Box size={14} />}
                      {s.id === 'sbom' && <Layers size={14} />}
                      {s.id === 'vulnerability-scan' && <Shield size={14} />}
                      {s.id === 'sign' && <ShieldCheck size={14} />}
                      {s.id === 'publish' && <Box size={14} />}
                      {!['unit-test', 'build', 'sbom', 'vulnerability-scan', 'sign', 'publish'].includes(s.id) && <Terminal size={14} />}
                      <span>{s.name}</span>
                      <small style={{ fontSize: '10px', opacity: 0.8, textTransform: 'uppercase' }}>
                        {s.builtin ? 'Built-in' : 'Custom'}
                      </small>
                    </div>
                    <ArrowRight size={14} className="pl-flow-arrow" />
                  </div>
                ))}

                <div className="pl-flow-node cd">
                  <Check size={14} />
                  <span>CD Deployment (Temporal)</span>
                </div>
              </div>
            </div>

            <div className="pl-card">
              <div style={{ marginBottom: '1rem' }}>
                <span style={{ fontWeight: 500 }}>Active version: </span>
                <span>{detailPipeline.activeVersion !== null ? `v${detailPipeline.activeVersion}` : 'no approved version'}</span>
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
                <span style={{ color: '#6b7280' }}>{detailPipeline.usedBy.join(', ') || 'Not used by any module yet'}</span>
              </div>
              <div style={{ marginTop: '1.5rem' }}>
                <button 
                  className="primary-button" 
                  onClick={() => {
                    const activeVer = detailPipeline.versions.find(v => v.version === detailPipeline.activeVersion) || detailPipeline.versions[0]
                    setView({ type: 'designer', pipelineName: detailPipeline.name, prefillScript: activeVer?.script || '' })
                  }}
                >
                  <Plus size={15} style={{ marginRight: '6px' }} />
                  Propose new version
                </button>
              </div>
            </div>

            <div className="pl-card">
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '1rem' }}>
                <h4 style={{ margin: 0 }}>Version History</h4>
                <small style={{ color: 'var(--muted, #64748b)' }}>Separation of duties: Authors cannot approve their own proposed versions</small>
              </div>
              
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
                          {v.rejectionReason && <div style={{ color: '#991b1b', fontSize: '12px' }}>Reason: {v.rejectionReason}</div>}
                        </td>
                        <td>
                          <button className="link-button" onClick={() => setViewScriptVersion(viewScriptVersion === v.version ? null : v.version)}>
                            {viewScriptVersion === v.version ? 'Hide script' : 'View script'}
                          </button>
                          {v.status === 'proposed' && (
                            <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.5rem' }}>
                              <button 
                                className="primary-button" 
                                style={{ padding: '0.25rem 0.5rem', fontSize: '0.75rem' }}
                                onClick={() => act(() => approveSharedPipelineVersion(detailPipeline.name, v.version),
                                  `Approved v${v.version}: new runs will use this version.`, () => loadDetail(detailPipeline.name))}
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
                                placeholder="Rejection reason" 
                                value={rejectReason} 
                                onChange={e => setRejectReason(e.target.value)} 
                              />
                              <button 
                                className="primary-button" 
                                style={{ padding: '0.25rem 0.5rem', fontSize: '0.75rem' }}
                                disabled={!rejectReason.trim()}
                                onClick={() => act(() => rejectSharedPipelineVersion(detailPipeline.name, v.version, rejectReason.trim()),
                                  `Rejected v${v.version}.`, () => {
                                    setRejectingVersion(null)
                                    setRejectReason('')
                                    loadDetail(detailPipeline.name)
                                  })}
                              >Submit</button>
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
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
                    <h4 style={{ margin: 0 }}>Pipeline Script (v{viewScriptVersion})</h4>
                    <div className="pl-editor-tabs" style={{ marginBottom: 0 }}>
                      <button 
                        type="button" 
                        className={`pl-tab-button ${detailTab === 'script' ? 'active' : ''}`}
                        onClick={() => setDetailTab('script')}
                      >
                        <FileCode size={14} /> pipeline.sh
                      </button>
                      <button 
                        type="button" 
                        className={`pl-tab-button ${detailTab === 'jenkinsfile' ? 'active' : ''}`}
                        onClick={() => setDetailTab('jenkinsfile')}
                      >
                        <Sliders size={14} /> Jenkinsfile
                      </button>
                    </div>
                  </div>

                  {(() => {
                    const ver = detailPipeline.versions.find(v => v.version === viewScriptVersion)
                    if (!ver) return null
                    
                    if (detailTab === 'jenkinsfile') {
                      const jf = generateDeclarativeJenkinsfile(ver.stages || [], ver.script || '')
                      return (
                        <div className="pl-editor-shell">
                          <div className="pl-editor-header">
                            <span className="pl-editor-title">Jenkinsfile (v{viewScriptVersion})</span>
                          </div>
                          <pre className="pl-jenkinsfile-pre">{jf}</pre>
                        </div>
                      )
                    }

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
                      <div className="pl-editor-shell">
                        <div className="pl-editor-header">
                          <span className="pl-editor-title">pipeline.sh (v{viewScriptVersion})</span>
                        </div>
                        <pre className="pl-jenkinsfile-pre">
                          {ver.script}
                        </pre>
                      </div>
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
