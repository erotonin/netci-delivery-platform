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
      '# [CI Gate 1] Chạy bộ unit test và xuất báo cáo coverage',
      'netci-builtin unit-test',
      '',
      '# @stage build "Build" builtin',
      '# [CI Gate 2] Đóng gói container image và gắn mã băm SHA256 bất biến',
      'netci-builtin build',
      '',
      '# @stage sbom "Generate SBOM" builtin',
      '# [CI Gate 3] Xuất danh mục thành phần phần mềm CycloneDX JSON',
      'netci-builtin sbom',
      '',
      '# @stage vulnerability-scan "Vulnerability Scan" builtin',
      '# [CI Gate 4] Quét lỗ hổng bảo mật offline bằng Trivy CVE DB',
      'netci-builtin vulnerability-scan',
      '',
      '# @stage sign "Sign Artifact" builtin',
      '# [CI Gate 5] Ký số nguồn gốc Provenance theo chuẩn SLSA v1 bằng Cosign',
      'netci-builtin sign',
      '',
      '# @stage publish "Publish Artifact" builtin',
      '# [CI Gate 6] Đẩy container image đã ký số lên Harbor Registry',
      'netci-builtin publish',
      '',
    ].join('\n')
    setDesignerScript(fullTemplate)
    feedback.notify('Đã nạp toàn bộ kịch bản CI chuẩn SLSA v1 đầy đủ!')
  }

  if (view.type === 'list') {
    return (
      <div className="pl-container">
        <PageHeader 
          title="Shared Pipelines" 
          description="Quản lý và thiết kế các kịch bản CI pipeline dùng chung (test, build, SBOM, scan, ký số, publish). CD (deploy) do netCI Temporal worker thực hiện theo từng module."
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
              <strong style={{ color: '#1e40af' }}>Module {moduleId} dùng pipeline:</strong>
              <div style={{ fontSize: '0.875rem', color: '#1d4ed8' }}>
                {currentModulePipeline ? `Đang dùng pipeline: ${currentModulePipeline}` : 'Chưa gán shared pipeline nào (đang dùng stage catalog riêng của module).'}
              </div>
            </div>
            <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
              <select 
                value={currentModulePipeline} 
                onChange={e => setCurrentModulePipeline(e.target.value)}
                style={{ padding: '0.4rem', borderRadius: '4px', border: '1px solid #93c5fd' }}
              >
                <option value="">-- Không dùng shared pipeline --</option>
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
                    feedback.notify('Đã cập nhật pipeline cho module!')
                  } catch (err) {
                    feedback.notify(errorText(err), 'error')
                  } finally {
                    setSavingModule(false)
                  }
                }}
              >
                {savingModule ? 'Lưu…' : 'Save'}
              </button>
            </div>
          </div>
        )}

        {loadingPipelines && <div>Đang tải danh sách pipelines...</div>}
        {errorPipelines && <div style={{ color: 'red' }}>Lỗi: {errorPipelines}</div>}

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
                    <span style={{ fontSize: '0.75rem', color: '#dc2626', background: '#fee2e2', padding: '2px 8px', borderRadius: '12px' }}>chưa có bản được duyệt</span>
                  )}
                </div>
                <p style={{ fontSize: '0.875rem', color: '#6b7280', margin: '0 0 1rem 0' }}>{p.description || 'Không có mô tả'}</p>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', marginBottom: '1rem' }}>
                  {p.stages.map(s => (
                    <span key={s.id} className={`pl-chip ${s.builtin ? 'pl-chip-builtin' : 'pl-chip-custom'}`} style={{ margin: 0 }}>
                      {s.name}
                    </span>
                  ))}
                </div>
              </div>
              <div style={{ borderTop: '1px solid var(--border, #e5e7eb)', paddingTop: '0.75rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: '0.75rem', color: '#6b7280' }}>
                <span>Sử dụng bởi: {p.usedBy.length} modules</span>
                {p.pendingVersions.length > 0 && (
                  <span style={{ color: '#d97706', fontWeight: 500 }}>chờ duyệt v{p.pendingVersions.join(', v')}</span>
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
          <ChevronLeft size={16}/> Quay lại
        </button>

        <PageHeader 
          title={view.pipelineName ? `Propose new version for ${view.pipelineName}` : 'Tạo pipeline mới'}
          description="Thiết kế kịch bản CI Pipeline tự động. Chọn các stage từ danh mục hoặc bổ sung các lệnh shell tuỳ chỉnh để thực thi trong Jenkins."
        />

        {/* Visual Pipeline Flow Ribbon */}
        <div className="pl-flow-banner">
          <div className="pl-flow-header">
            <h4>
              <GitBranch size={16} />
              Luồng thực thi Pipeline (Execution Flow Preview)
            </h4>
            <span style={{ fontSize: '12px', color: 'var(--muted, #64748b)' }}>
              {parsedStages.length} stage(s) được cấu hình
            </span>
          </div>

          <div className="pl-flow-track">
            <div className="pl-flow-node start">
              <Code size={14} />
              <span>Checkout SCM</span>
            </div>

            <ArrowRight size={14} className="pl-flow-arrow" />

            {parsedStages.map((s, idx) => (
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
                    {s.builtin ? 'CI Gate' : 'Custom'}
                  </small>
                </div>
                <ArrowRight size={14} className="pl-flow-arrow" />
              </div>
            ))}

            <div className="pl-flow-node cd" title="CD Deployment do netCI Temporal Worker đảm nhiệm">
              <Check size={14} />
              <span>CD Deployment</span>
              <small style={{ fontSize: '10px', background: '#10b981', color: '#fff', padding: '1px 4px', borderRadius: '3px' }}>Temporal</small>
            </div>
          </div>
        </div>
        
        {designerLoading && <div>Đang tải building blocks...</div>}
        {designerError && <div style={{ color: 'red' }}>Lỗi: {designerError}</div>}
        
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
                    title="Nạp toàn bộ kịch bản chuẩn SLSA v1"
                  >
                    ⚡ Điền mẫu chuẩn
                  </button>
                </div>
                <p style={{ margin: '0 0 10px 0', fontSize: '11.5px', color: 'var(--muted, #64748b)' }}>
                  Các cổng kiểm soát chất lượng & an toàn thực thi bằng toolbox bảo mật của netCI:
                </p>

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
                        <div className="pl-block-title">
                          <span>{b.name}</span>
                          {b.required && <span style={{ color: '#ef4444', fontSize: '10.5px' }}>(Required)</span>}
                        </div>
                        <div className="pl-block-desc">
                          {b.id === 'unit-test' && 'Chạy Unit Test & kiểm tra Code Coverage'}
                          {b.id === 'build' && 'Compile source code & đóng gói Docker container'}
                          {b.id === 'sbom' && 'Xuất file CycloneDX SBOM kiểm kê phụ thuộc'}
                          {b.id === 'vulnerability-scan' && 'Quét lỗ hổng offline bằng Trivy CVE DB'}
                          {b.id === 'sign' && 'Ký số nguồn gốc Provenance SLSA v1 bằng Cosign'}
                          {b.id === 'publish' && 'Đẩy image & chữ ký số lên Harbor Registry'}
                        </div>
                        {added ? (
                          <div style={{ fontSize: '11px', color: '#10b981', marginTop: '4px', fontWeight: 600, display: 'flex', alignItems: 'center', gap: '4px' }}>
                            <Check size={12} /> added
                          </div>
                        ) : (
                          <div style={{ fontSize: '11px', color: '#2563eb', marginTop: '4px', fontWeight: 500, display: 'flex', alignItems: 'center', gap: '4px' }}>
                            <Plus size={12} /> Thêm vào pipeline
                          </div>
                        )}
                      </button>
                    )
                  })}
                </div>
                
                <h4 style={{ marginTop: '1.5rem', marginBottom: '0.75rem', fontSize: '0.9rem', display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <Terminal size={16} color="#7c3aed" />
                  Templates
                </h4>
                <p style={{ margin: '0 0 10px 0', fontSize: '11.5px', color: 'var(--muted, #64748b)' }}>
                  Các bước kiểm tra mở rộng (chạy trong sandbox không cấp credentials):
                </p>

                <div className="pl-blocks-list">
                  {blocks.templates.map(t => (
                    <button 
                      key={t.id} 
                      className="pl-block-item"
                      onClick={() => setDesignerScript(prev => prev + (prev.endsWith('\n') || !prev ? '' : '\n') + t.block + '\n')}
                    >
                      <div className="pl-block-title">
                        <span>{t.name}</span>
                        <Plus size={13} color="#7c3aed" />
                      </div>
                      <div className="pl-block-desc">{t.description}</div>
                    </button>
                  ))}
                </div>
              </div>
            </div>
            
            {/* Right Column: Code Editor & Previews */}
            <div className="pl-editor-col">
              {!view.pipelineName && (
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
                  <div>
                    <label style={{ display: 'block', fontSize: '12px', fontWeight: 600, marginBottom: '4px' }}>Tên Pipeline</label>
                    <input className="pl-input" placeholder="Tên pipeline (vd: my-pipeline)" value={designerName} onChange={e => setDesignerName(e.target.value)} pattern="^[a-z][a-z0-9-]{1,62}$" />
                  </div>
                  <div>
                    <label style={{ display: 'block', fontSize: '12px', fontWeight: 600, marginBottom: '4px' }}>Mô tả kịch bản</label>
                    <input className="pl-input" placeholder="Mô tả ngắn" value={designerDesc} onChange={e => setDesignerDesc(e.target.value)} />
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
                  <span>pipeline.sh (CI Script Thực Thi)</span>
                </button>
                <button 
                  type="button" 
                  className={`pl-tab-button ${editorTab === 'compliance' ? 'active' : ''}`}
                  onClick={() => setEditorTab('compliance')}
                >
                  <ShieldCheck size={15} />
                  <span>Kiểm toán Tuân thủ SLSA & Quality Gate</span>
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
                          ⚡ Sinh tự động từ các stages được chọn
                        </span>
                        <button type="button" className="pl-editor-btn" onClick={handleCopyJenkinsfile}>
                          {copiedJenkinsfile ? <Check size={12} color="#10b981" /> : <Copy size={12} />}
                          {copiedJenkinsfile ? 'Đã chép Jenkinsfile' : 'Sao chép Jenkinsfile'}
                        </button>
                      </>
                    )}
                    {editorTab === 'script' && (
                      <>
                        <span style={{ fontSize: '11px', color: '#94a3b8' }}>
                          {designerScript.split('\n').length} dòng · {parsedStages.length} stage(s)
                        </span>
                        <button type="button" className="pl-editor-btn" onClick={handleCopyScript}>
                          {copiedScript ? <Check size={12} color="#10b981" /> : <Copy size={12} />}
                          {copiedScript ? 'Đã chép' : 'Sao chép'}
                        </button>
                        <button type="button" className="pl-editor-btn" onClick={() => setDesignerScript('')}>
                          Làm sạch
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
                  placeholder="# Kịch bản pipeline CI thực thi trên runner..."
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
                      Báo cáo Tuân thủ Cổng Kiểm soát (Gate Verification)
                    </h4>
                    <p style={{ margin: '0 0 14px 0', fontSize: '12px', color: '#94a3b8' }}>
                      netCI áp dụng nguyên tắc 100% Fail-closed. Bản build chỉ được thăng cấp (promote) khi vượt qua toàn bộ các cổng bảo mật bắt buộc:
                    </p>

                    <div className="pl-compliance-grid">
                      <div className="pl-compliance-card" style={{ borderColor: hasBuild ? '#10b981' : '#f87171' }}>
                        <strong style={{ color: hasBuild ? '#059669' : '#dc2626' }}>
                          {hasBuild ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}
                          Compile & Build Container
                        </strong>
                        <small>{hasBuild ? 'Gắn SHA256 digest bất biến' : 'Chưa có stage build'}</small>
                      </div>

                      <div className="pl-compliance-card" style={{ borderColor: hasSbom ? '#10b981' : '#f87171' }}>
                        <strong style={{ color: hasSbom ? '#059669' : '#dc2626' }}>
                          {hasSbom ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}
                          Xuất SBOM CycloneDX
                        </strong>
                        <small>{hasSbom ? 'Kiểm kê nguồn gốc thư viện' : 'Thiếu stage tạo SBOM'}</small>
                      </div>

                      <div className="pl-compliance-card" style={{ borderColor: hasScan ? '#10b981' : '#f87171' }}>
                        <strong style={{ color: hasScan ? '#059669' : '#dc2626' }}>
                          {hasScan ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}
                          Quét Lỗ Hổng Trivy CVE
                        </strong>
                        <small>{hasScan ? 'Đối chiếu air-gapped CVE DB' : 'Thiếu stage quét CVE'}</small>
                      </div>

                      <div className="pl-compliance-card" style={{ borderColor: hasSign ? '#10b981' : '#f87171' }}>
                        <strong style={{ color: hasSign ? '#059669' : '#dc2626' }}>
                          {hasSign ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}
                          Ký Số SLSA v1 (Cosign)
                        </strong>
                        <small>{hasSign ? 'Chứng thực chữ ký in-toto' : 'Thiếu chữ ký số SLSA'}</small>
                      </div>

                      <div className="pl-compliance-card" style={{ borderColor: hasPublish ? '#10b981' : '#f87171' }}>
                        <strong style={{ color: hasPublish ? '#059669' : '#dc2626' }}>
                          {hasPublish ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}
                          Phát hành Harbor Registry
                        </strong>
                        <small>{hasPublish ? 'Lưu trữ OCI image an toàn' : 'Thiếu stage đẩy Harbor'}</small>
                      </div>
                    </div>
                  </div>
                )}
              </div>
              
              {/* Preview stages card */}
              <div className="pl-card">
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
                  <h4 style={{ margin: 0, fontSize: '0.875rem' }}>Thứ tự thực thi các Stages ({parsedStages.length})</h4>
                  <span style={{ fontSize: '11px', color: 'var(--muted, #64748b)' }}>Thực thi tuần tự trong build toolbox</span>
                </div>
                <ol className="pl-preview-list">
                  {parsedStages.map((s, idx) => (
                    <li key={idx}>
                      <strong>{s.name}</strong> <code>{s.id}</code> {s.builtin && <span style={{ color: '#1d4ed8', fontSize: '0.75rem', fontWeight: 600 }}>(builtin)</span>}
                    </li>
                  ))}
                </ol>
                {designerHints.length > 0 && (
                  <div className="pl-hints">
                    {designerHints.map((h, idx) => <div key={idx} style={{ display: 'flex', alignItems: 'center', gap: '6px' }}><AlertTriangle size={14} />{h}</div>)}
                  </div>
                )}
              </div>
              
              {designerSubmitError && (
                <div style={{ color: 'red', padding: '1rem', background: '#fee2e2', borderRadius: '6px', fontSize: '13px' }}>
                  Lỗi: {designerSubmitError}
                </div>
              )}
              
              <div style={{ display: 'flex', gap: '10px', alignItems: 'center' }}>
                <button className="primary-button" style={{ alignSelf: 'flex-start' }} disabled={designerSubmitting} onClick={handleDesignerSubmit}>
                  {designerSubmitting ? 'Đang gửi…' : 'Gửi để duyệt'}
                </button>
                <small style={{ color: 'var(--muted, #64748b)', fontSize: '12px' }}>
                  * Sau khi gửi, cần một Quản trị viên (Platform Admin) khác phê duyệt để phiên bản có hiệu lực (Quy tắc 4 mắt ADR-058).
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
          <ChevronLeft size={16}/> Quay lại danh sách
        </button>
        
        {detailLoading && <div>Đang tải...</div>}
        {detailError && <div style={{ color: 'red' }}>Lỗi: {detailError}</div>}
        
        {detailPipeline && (
          <>
            <PageHeader title={detailPipeline.name} description={detailPipeline.description || 'Không có mô tả'} />

            {/* Visual Pipeline Flow Ribbon for Active Pipeline */}
            <div className="pl-flow-banner">
              <div className="pl-flow-header">
                <h4>
                  <GitBranch size={16} />
                  Sơ đồ Stages của Pipeline {detailPipeline.name}
                </h4>
                <span style={{ fontSize: '12px', color: 'var(--muted, #64748b)' }}>
                  Phiên bản active: {detailPipeline.activeVersion !== null ? `v${detailPipeline.activeVersion}` : 'Chưa có'}
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
                  <Plus size={15} style={{ marginRight: '6px' }} />
                  Propose new version
                </button>
              </div>
            </div>

            <div className="pl-card">
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '1rem' }}>
                <h4 style={{ margin: 0 }}>Version History</h4>
                <small style={{ color: 'var(--muted, #64748b)' }}>Nguyên tắc 4 mắt: người tạo không được tự duyệt phiên bản của mình</small>
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
                          {v.rejectionReason && <div style={{ color: '#991b1b', fontSize: '12px' }}>Lý do: {v.rejectionReason}</div>}
                        </td>
                        <td>
                          <button className="link-button" onClick={() => setViewScriptVersion(viewScriptVersion === v.version ? null : v.version)}>
                            {viewScriptVersion === v.version ? 'Ẩn kịch bản' : 'View script'}
                          </button>
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
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
                    <h4 style={{ margin: 0 }}>Kịch bản phiên bản v{viewScriptVersion}</h4>
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
