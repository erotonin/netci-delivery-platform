import { useEffect, useState } from 'react'
import {
  ArrowLeft, Box, Check, CheckCircle2, Code2, Copy, ExternalLink, GitBranch,
  History, MoreHorizontal, Play, Plus, RotateCcw, Settings, ShieldAlert,
  TerminalSquare, XCircle, Zap, ZoomIn, ZoomOut,
} from 'lucide-react'
import {
  approveConfigRevision, cancelPipelineRun, createModuleVersion, detectDrift,
  diffConfigRevisions, getDora, getModule, getModuleGitRefs, getModuleOverview, getPipelineLogs,
  getPipelineStages, listConfigRevisions, listModulePipelineRuns, listModuleVersions,
  proposeConfigRevision, rejectConfigRevision, retryPipelineRun, rollbackConfigRevision,
  startModulePipeline, applyModuleConfig, type ConfigApplyResponse, type ConfigDriftReport, type ConfigRevision, type ConfigRevisionDiff,
  type DeploymentEnvironmentConfig, type Environment, type GitRefs, type ModuleOverview, type ModulePipelineConfig, type ModuleVersion,
  type PipelineRun, type PipelineStage, type Runtime,
} from './api/netciClient'
import { usePortalFeedback } from './PortalFeedback'
import { DoraCards, Modal, StatusPill } from './PortalShell'
import type { DoraCardMetric, ModuleTab } from './portalTypes'

const pipelineStages = ['checkout', 'unit-test', 'build', 'sbom', 'vulnerability-scan', 'sign', 'publish', 'deploy', 'health-check']
function EmptyModuleData({ title, description }: { title: string; description: string }) {
  return <section className="panel empty-tab-state"><Box size={29} /><h2>{title}</h2><p>{description}</p></section>
}

const shortDigest = (digest?: string | null) => (digest ? digest.replace(/^sha256:/, '').slice(0, 12) : '—')
const shortSha = (sha?: string | null) => (sha ? sha.slice(0, 8) : '—')
// Older records carry the identity provider's opaque subject; show what fits, keep the rest in the title.
const person = (subject?: string | null) => (!subject ? '—' : /^[0-9a-f]{8}-[0-9a-f]{4}-/.test(subject) ? `${subject.slice(0, 8)}…` : subject)
export function timeAgo(iso?: string | null): string {
  if (!iso) return '—'
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000))
  if (seconds < 60) return 'vừa xong'
  if (seconds < 3600) return `${Math.round(seconds / 60)} phút trước`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} giờ trước`
  return `${Math.round(seconds / 86400)} ngày trước`
}
const environmentLabel: Record<string, string> = { dev: 'Development', staging: 'Staging', prod: 'Production' }
const statusLabel = (status: string) => ({
  never_deployed: 'chưa triển khai', healthy: 'healthy', failed: 'failed', rolled_back: 'rolled back',
  deploying: 'deploying', pending_approval: 'chờ phê duyệt', rollback_in_progress: 'đang rollback', cancelled: 'cancelled', rollback_failed: 'rollback failed',
}[status] ?? status.replace(/_/g, ' '))

function OverviewTab({ moduleId, onOpenRun }: { moduleId: string; onOpenRun?: (run: PipelineRun) => void }) {
  const [overview, setOverview] = useState<ModuleOverview | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    getModuleOverview(moduleId).then((result) => { if (active) { setOverview(result); setError('') } }).catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : 'Unable to load module overview.') })
    return () => { active = false }
  }, [moduleId])
  if (error) return <EmptyModuleData title="Overview unavailable" description={error} />
  if (!overview) return <EmptyModuleData title="Loading delivery evidence" description="Reading deployments, releases and quality reports from netCI." />
  const hasData = overview.recentRuns.length > 0 || overview.recentDeployments.length > 0
  if (!hasData) return <EmptyModuleData title="No delivery activity yet" description="Run the pipeline once: builds, deployments and the evidence behind them will appear here." />
  const q = overview.quality
  return <>
    <div className="env-grid">{overview.environments.map((env) => <article className={`panel env-card env-${env.environment}`} key={env.environment}>
      <header><span className={`env-badge env-${env.environment}`}>{environmentLabel[env.environment] ?? env.environment}</span><StatusPill status={statusLabel(env.status)} /></header>
      {env.status === 'never_deployed' ? <p className="muted">Chưa có deployment nào tới môi trường này.</p> : <dl>
        <div><dt>Artifact</dt><dd className="mono" title={env.artifactDigest ?? ''}>{shortDigest(env.artifactDigest)}{env.version ? <em> · {env.version}</em> : <em className="muted"> · build chưa gắn version</em>}</dd></div>
        <div><dt>Commit</dt><dd className="mono">{shortSha(env.commitSha)}</dd></div>
        <div><dt>Chiến lược</dt><dd>{env.strategy ?? 'rolling'}</dd></div>
        <div><dt>Cập nhật</dt><dd title={env.updatedAt}>{timeAgo(env.updatedAt)}</dd></div>
        {env.approvedBy && <div><dt>Phê duyệt</dt><dd title={env.approvedBy}>{person(env.approvedBy)}</dd></div>}
        {env.status === 'rolled_back' && <p className="muted">Đã rollback: môi trường đang chạy bản trước đó.</p>}
      </dl>}
    </article>)}</div>
    <section className="panel quality-strip">
      <div className="panel-heading"><div><h2>Bằng chứng của artifact mới nhất</h2><p>{q.source ? <>Run <span className="mono">{q.source.pipelineRunId.slice(0, 8)}</span> · commit <span className="mono">{shortSha(q.source.commitSha)}</span> · digest <span className="mono">{shortDigest(q.source.artifactDigest)}</span> · {timeAgo(q.source.at)}</> : 'Chưa có build thành công nào có artifact.'}</p></div>{q.source && <StatusPill status={q.decision === 'allow' ? 'policy: allow' : `policy: ${q.decision ?? 'unknown'}`} />}</div>
      {q.source && <div className="quality-facts">
        <div><span>SBOM</span><strong>{q.sbom?.present ? `${q.sbom.format ?? 'present'} (${q.sbom.generatedBy ?? '?'})` : 'không có'}</strong></div>
        <div><span>Vulnerability scan</span><strong>{q.scan?.scanner ? `${q.scan.scanner}: ${q.scan.status ?? '?'} · critical ${q.scan.critical ?? '?'} · high ${q.scan.high ?? '?'}` : 'không có'}</strong></div>
        <div><span>Chữ ký</span><strong>{q.signature?.verified ? `${q.signature.provider ?? 'cosign'} · verified` : 'chưa xác minh'}</strong></div>
        <div><span>CI quality report</span><strong>{overview.trends.testCoverage !== null ? `coverage ${overview.trends.testCoverage}%` : 'chưa có báo cáo'}{overview.trends.automationPassRate !== null ? ` · autotest ${overview.trends.automationPassRate}%` : ''}</strong></div>
      </div>}
    </section>
    <div className="module-overview-grid">
      <section className="panel"><div className="panel-heading"><div><h2>Build gần đây</h2><p>{overview.recentRuns.length} lượt chạy mới nhất</p></div></div>
        <div className="data-table history-table compact"><div className="table-row table-head"><span>Trạng thái</span><span>Môi trường</span><span>Commit</span><span>Artifact</span><span>Bởi</span><span>Khi nào</span></div>
          {overview.recentRuns.map((run) => <button className="table-row table-button" key={run.id} onClick={() => onOpenRun?.(run)} title={run.id}><StatusPill status={run.status.replace('_', ' ')} /><span>{run.environment}</span><span className="mono">{shortSha(run.commitSha)}</span><span className="mono">{shortDigest(run.artifactDigest)}</span><span title={run.startedBy ?? ''}>{person(run.startedBy)}</span><span title={run.createdAt}>{timeAgo(run.createdAt)}</span></button>)}
        </div>
      </section>
      <section className="panel"><div className="panel-heading"><div><h2>Deployment gần đây</h2><p>{overview.recentDeployments.length} bản ghi mới nhất</p></div></div>
        <div className="data-table history-table compact five"><div className="table-row table-head"><span>Trạng thái</span><span>Môi trường</span><span>Artifact</span><span>Duyệt bởi</span><span>Khi nào</span></div>
          {overview.recentDeployments.map((d) => <div className="table-row" key={d.id} title={d.id}><StatusPill status={statusLabel(d.status)} /><span>{d.environment}</span><span className="mono">{shortDigest(d.artifactDigest)}{d.version ? ` · ${d.version}` : ''}</span><span title={d.approvedBy ?? ''}>{person(d.approvedBy)}</span><span title={d.updatedAt}>{timeAgo(d.updatedAt)}</span></div>)}
        </div>
      </section>
    </div>
  </>
}

type PipelineDefinition = { id: string; name: string; number: string; sha: string; actor: string; time: string; duration: string; status: string; branch: string; stages: string[] }

const stageLabels: Record<string, string> = {
  checkout: 'Checkout', 'unit-test': 'Unit Test', build: 'Build', sbom: 'Generate SBOM',
  'vulnerability-scan': 'Vulnerability Scan', sign: 'Sign Artifact', publish: 'Publish Artifact',
  deploy: 'Deploy', 'health-check': 'Health Check',
}

function stageCategory(stage: string): string {
  if (['unit-test', 'Integration Test', 'Lint', 'Type Check'].includes(stage)) return 'test'
  if (['sbom', 'vulnerability-scan', 'sign', 'Code Analysis', 'Security Scan'].includes(stage)) return 'security'
  if (['build', 'Build', 'Build Docker Image', 'Generate Docs'].includes(stage)) return 'build'
  if (['publish', 'deploy', 'health-check', 'Package Artifact', 'Publish Artifact'].includes(stage)) return 'publish'
  return 'source'
}

function PipelineRunView({
  pipeline,
  liveRun,
  moduleId,
  onBack,
  onRunUpdated,
}: {
  pipeline: PipelineDefinition
  liveRun: PipelineRun | null
  moduleId?: string
  onBack: () => void
  onRunUpdated: (updated: PipelineRun) => void
}) {
  const { notify } = usePortalFeedback()
  const [stage, setStage] = useState(pipeline.stages[0] ?? 'Checkout')
  const [zoom, setZoom] = useState(1)
  const selectedStageLabel = stageLabels[stage] ?? stage
  const [log, setLog] = useState('No log lines have been reported for this run.')
  const [stages, setStages] = useState<PipelineStage[]>([])
  const [cancelling, setCancelling] = useState(false)
  const [retrying, setRetrying] = useState(false)

  useEffect(() => {
    if (!liveRun) {
      setLog('No pipeline run selected.')
      setStages([])
      return
    }

    const refreshData = () => {
      getPipelineLogs(liveRun.id)
        .then((result) => setLog(result.lines.length ? result.lines.join('\n') : 'No log lines have been reported for this run.'))
        .catch((error) => setLog(error instanceof Error ? error.message : 'Unable to load pipeline logs.'))

      getPipelineStages(liveRun.id)
        .then((res) => setStages(res.items))
        .catch(() => setStages([]))

      if (moduleId && (liveRun.status === 'queued' || liveRun.status === 'running')) {
        listModulePipelineRuns(moduleId)
          .then((res) => {
            const matched = res.items.find((r) => r.id === liveRun.id)
            if (matched && (matched.status !== liveRun.status || matched.jenkinsRunId !== liveRun.jenkinsRunId)) {
              onRunUpdated(matched)
            }
          })
          .catch(() => {})
      }
    }

    refreshData()

    if (liveRun.status === 'queued' || liveRun.status === 'running') {
      const interval = setInterval(refreshData, 1500)
      return () => clearInterval(interval)
    }
  }, [liveRun?.id, liveRun?.status, moduleId])

  const stageObjMap = new Map<string, PipelineStage>()
  for (const s of stages) {
    stageObjMap.set(s.stageId.toLowerCase(), s)
  }

  const copyLog = async () => {
    try {
      await navigator.clipboard.writeText(log)
      notify(`Đã sao chép log của stage ${selectedStageLabel}.`)
    } catch {
      notify('Trình duyệt không cho phép truy cập clipboard.', 'error')
    }
  }

  const handleCancel = async () => {
    if (!liveRun) return
    setCancelling(true)
    try {
      const updated = await cancelPipelineRun(liveRun.id, 'Cancelled via portal')
      onRunUpdated(updated)
      notify('Pipeline run đã được hủy thành công.')
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Không thể hủy pipeline run.', 'error')
    } finally {
      setCancelling(false)
    }
  }

  const handleRetry = async () => {
    if (!liveRun) return
    setRetrying(true)
    try {
      const newRun = await retryPipelineRun(liveRun.id)
      onRunUpdated(newRun)
      notify(`Pipeline retry đã được tạo (#${newRun.id.slice(0, 8)}).`)
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Không thể retry pipeline run.', 'error')
    } finally {
      setRetrying(false)
    }
  }

  const canCancel = !!liveRun && (liveRun.status === 'queued' || liveRun.status === 'running' || liveRun.status === 'waiting_approval')
  const canRetry = !!liveRun && (liveRun.status === 'failed' || liveRun.status === 'cancelled' || liveRun.status === 'rolled_back' || liveRun.status === 'succeeded')

  return <section className="run-view">
    <button className="back-button" onClick={onBack}><ArrowLeft size={16} />Back to build history</button>
    <div className="run-heading">
      <div>
        <div className="title-status">
          <h2>{pipeline.name} {liveRun ? `#${liveRun.jenkinsRunId ?? liveRun.id.slice(0, 8)}` : ''}</h2>
          <StatusPill status={liveRun?.status.replace('_', ' ') ?? 'Not started'} />
          {liveRun?.retryOf && <span className="mono" style={{ marginLeft: 8, fontSize: '0.85em', opacity: 0.8 }}>(retry of #{liveRun.retryOf.slice(0, 8)})</span>}
        </div>
        <p>{liveRun ? `${liveRun.branch} · ${liveRun.commitSha} · triggered by ${liveRun.startedBy ?? 'unknown actor'}` : 'No pipeline run selected.'}</p>
      </div>
      <div className="run-actions">
        {canCancel && (
          <button className="secondary-button" disabled={cancelling} onClick={handleCancel}>
            <XCircle size={15} />{cancelling ? 'Cancelling…' : 'Cancel'}
          </button>
        )}
        <button className="secondary-button" disabled={retrying || !canRetry} onClick={handleRetry}>
          <RotateCcw size={15} />{retrying ? 'Retrying…' : 'Retry'}
        </button>
        <button className="secondary-button" disabled={!liveRun?.consoleUrl} title={liveRun?.consoleUrl ? 'Open Jenkins console' : 'Jenkins run URL is not part of the current API response'} onClick={() => { if (liveRun?.consoleUrl) window.open(liveRun.consoleUrl, '_blank', 'noopener,noreferrer') }}>
          <ExternalLink size={15} />Open Jenkins
        </button>
      </div>
    </div>
    <section className="pipeline-canvas panel">
      <div className="canvas-toolbar">
        <span>
          <strong>Configured pipeline stages</strong>
          <small>{pipeline.stages.length} stages · {stages.length ? `${stages.length} reported` : 'per-stage results pending'} · {Math.round(zoom * 100)}%</small>
        </span>
        <div>
          <button aria-label="Thu nhỏ" disabled={zoom <= .75} onClick={() => setZoom((value) => Math.max(.75, value - .25))}><ZoomOut size={16} /></button>
          <button aria-label="Phóng to" disabled={zoom >= 1.5} onClick={() => setZoom((value) => Math.min(1.5, value + .25))}><ZoomIn size={16} /></button>
          <button aria-label="Khôi phục" disabled={zoom === 1} onClick={() => setZoom(1)}><RotateCcw size={15} /></button>
        </div>
      </div>
      <div className="stage-graph" style={{ transform: `scale(${zoom})`, transformOrigin: 'left top' }}>
        {pipeline.stages.map((stageId, index) => {
          const name = stageLabels[stageId] ?? stageId
          const observed = stageObjMap.get(stageId.toLowerCase()) || stageObjMap.get(name.toLowerCase().replace(/\s+/g, '-'))
          const statusText = observed ? `${observed.status}${observed.durationMs != null ? ` (${(observed.durationMs / 1000).toFixed(1)}s)` : ''}` : 'Pending'
          return (
            <button
              className={`stage-node category-${stageCategory(stageId)} ${stage === stageId ? 'selected' : ''}`}
              onClick={() => setStage(stageId)}
              key={`${stageId}-${index}`}
            >
              <span>{index + 1}</span>
              <strong>{name}</strong>
              <small>{statusText}</small>
            </button>
          )
        })}
      </div>
    </section>
    <section className="log-panel">
      <div>
        <span><TerminalSquare size={16} />{selectedStageLabel}</span>
        <button aria-label="Sao chép log" onClick={copyLog}><Copy size={15} /></button>
      </div>
      <pre>{log}</pre>
    </section>
  </section>
}

function cleanBranchName(raw: string, defaultBranch = 'main'): string {
  if (!raw) return defaultBranch
  const first = raw.split(',')[0].trim().replace(/\*/g, '')
  const sanitized = first.replace(/[^0-9a-zA-Z._\-/]/g, '')
  return sanitized || defaultBranch
}

function pipelineEnvironment(pipelineId: string, configuredEnvs?: DeploymentEnvironmentConfig[]): Environment {
  if (pipelineId === 'cd-staging') return 'staging'
  if (pipelineId === 'cd-prod') return 'prod'
  if (pipelineId === 'cd-dev') return 'dev'
  if (configuredEnvs && configuredEnvs.length > 0) {
    const envs = configuredEnvs.map((e) => e.environment)
    if (envs.includes('dev')) return 'dev'
    if (envs.includes('prod')) return 'prod'
    if (envs.includes('staging')) return 'staging'
    return envs[0]
  }
  return 'dev'
}

function environmentPipelines(config: Partial<ModulePipelineConfig> = {}, environments?: DeploymentEnvironmentConfig[]): PipelineDefinition[] {
  // One pipeline per configured environment: a netCI run always builds *and* deploys
  // to the environment it was started for, so "CI" and "CD dev" were the same run
  // shown twice, and "last build" on every card was the same oldest record.
  const configured = (environments ?? []).map((e) => e.environment)
  const order: Environment[] = ['dev', 'staging', 'prod']
  const envs = order.filter((e) => configured.includes(e))
  const list = envs.length ? envs : (['dev'] as Environment[])
  const keyFor: Record<string, string> = { dev: 'CD Dev', staging: 'CD Staging', prod: 'CD Prod' }
  return list.map((env) => {
    const fromConfig = config?.pipelines?.[keyFor[env]] ?? config?.pipelines?.CI
    return {
      id: `cd-${env}`, name: `${environmentLabel[env] ?? env}`, number: '', sha: '', actor: '', time: '', duration: '', status: 'not_started',
      branch: (fromConfig?.branch || 'main').trim(),
      stages: fromConfig?.stages?.length ? fromConfig.stages : [...pipelineStages],
    }
  })
}

function PipelineTab({ moduleId, pipelineConfig, deploymentEnvironments, initialRun }: { moduleId: string; pipelineConfig: Partial<ModulePipelineConfig>; deploymentEnvironments?: DeploymentEnvironmentConfig[]; initialRun?: PipelineRun | null }) {
  const { notify } = usePortalFeedback()
  const definitions = environmentPipelines(pipelineConfig, deploymentEnvironments)
  const [historyPipeline, setHistoryPipeline] = useState<PipelineDefinition | null>(null)
  const [run, setRun] = useState<{ pipeline: PipelineDefinition; liveRun: PipelineRun | null } | null>(() => {
    if (!initialRun) return null
    const pipeline = definitions.find((d) => d.id === `cd-${initialRun.environment}`) ?? definitions[0]
    return pipeline ? { pipeline, liveRun: initialRun } : null
  })
  const [liveRuns, setLiveRuns] = useState<PipelineRun[]>([])
  const [busyPipeline, setBusyPipeline] = useState<string | null>(null)
  const [triggered, setTriggered] = useState<string | null>(null)
  const [refs, setRefs] = useState<GitRefs | null>(null)

  useEffect(() => {
    let active = true
    listModulePipelineRuns(moduleId).then((result) => { if (active) setLiveRuns([...result.items].sort((a, b) => b.createdAt.localeCompare(a.createdAt))) }).catch((error) => { if (active) notify(error instanceof Error ? error.message : 'Không tải được pipeline history.', 'error') })
    // Branches and tags come from the module's own repository, read by the server. The
    // Portal used to offer netCI's *own* build commit here, which does not exist in the
    // module's repository, so the default "Run" failed at checkout.
    getModuleGitRefs(moduleId).then((result) => { if (active) setRefs(result) }).catch(() => { if (active) setRefs(null) })
    return () => { active = false }
  }, [moduleId])
  const [runModalPipeline, setRunModalPipeline] = useState<PipelineDefinition | null>(null)
  const [modalBranch, setModalBranch] = useState('')
  const [modalRevision, setModalRevision] = useState('')
  const [modalEnv, setModalEnv] = useState<Environment>('dev')
  const [modalError, setModalError] = useState('')
  const branchSha = (name: string) => refs?.branches.find((b) => b.name === name)?.sha ?? refs?.tags.find((t) => t.name === name)?.sha ?? ''

  const openRunModal = (pipeline: PipelineDefinition) => {
    setRunModalPipeline(pipeline)
    const wanted = cleanBranchName(pipeline.branch)
    const branch = refs?.branches.some((b) => b.name === wanted) ? wanted : (refs?.branches[0]?.name ?? wanted)
    setModalBranch(branch)
    setModalRevision(branchSha(branch))
    setModalEnv((pipeline.id.replace('cd-', '') as Environment) || 'dev')
    setModalError('')
  }

  const executeModalRun = async () => {
    if (!runModalPipeline) return
    const rev = modalRevision.trim()
    if (!/^[0-9a-f]{7,64}$/i.test(rev)) {
      setModalError('Commit SHA phải là 7–64 ký tự hex. Chọn một nhánh để netCI điền commit đầu nhánh.')
      return
    }
    setBusyPipeline(runModalPipeline.id)
    setModalError('')
    try {
      const next = await startModulePipeline(moduleId, {
        commitSha: rev,
        branch: modalBranch.trim() || 'main',
        environment: modalEnv,
        parameters: { portalPipeline: runModalPipeline.id },
      })
      setLiveRuns((current) => [next, ...current.filter((item) => item.id !== next.id)])
      setTriggered(runModalPipeline.name)
      notify(`Đã đưa vào hàng đợi: build ${rev.slice(0, 8)} → ${modalEnv}.`)
      setRunModalPipeline(null)
      setRun({ pipeline: runModalPipeline, liveRun: next })
    } catch (error) {
      setModalError(error instanceof Error ? error.message : 'Không thể trigger pipeline.')
    } finally {
      setBusyPipeline(null)
    }
  }

  const runsForPipeline = (pipeline: PipelineDefinition) => liveRuns.filter((item) => item.environment === pipeline.id.replace('cd-', ''))
  if (run) {
    return (
      <PipelineRunView
        pipeline={run.pipeline}
        liveRun={run.liveRun}
        moduleId={moduleId}
        onBack={() => setRun(null)}
        onRunUpdated={(updated) => {
          setRun({ pipeline: run.pipeline, liveRun: updated })
          setLiveRuns((current) => [updated, ...current.filter((item) => item.id !== updated.id)])
        }}
      />
    )
  }
  if (historyPipeline) { const historyRuns = runsForPipeline(historyPipeline); return <section className="history-view"><button className="back-button" onClick={() => setHistoryPipeline(null)}><ArrowLeft size={16} />Tất cả môi trường</button><div className="run-heading"><div><h2>{historyPipeline.name} · lịch sử build</h2><p>Các lượt chạy tới môi trường này, mới nhất trước; trạng thái do Jenkins và worker báo về.</p></div><button className="primary-button" disabled={busyPipeline === historyPipeline.id} onClick={() => openRunModal(historyPipeline)}><Play size={15} />{busyPipeline === historyPipeline.id ? 'Đang xếp hàng…' : 'Chạy pipeline'}</button></div><section className="panel table-panel"><div className="data-table history-table"><div className="table-row table-head"><span>Build</span><span>Commit</span><span>Nhánh</span><span>Bởi</span><span>Bắt đầu</span><span>Trạng thái</span><span /></div>{historyRuns.map((item) => <button className="table-row table-button" onClick={() => setRun({ pipeline: historyPipeline, liveRun: item })} key={item.id}><span className="request-id" title={item.jenkinsRunId ?? item.id}>#{item.jenkinsRunId ? item.jenkinsRunId.split('#').pop() : item.id.slice(0, 8)}</span><span className="mono" title={item.commitSha}>{shortSha(item.commitSha)}</span><span>{item.branch}</span><span title={item.startedBy ?? ''}>{person(item.startedBy)}</span><span title={item.createdAt}>{new Date(item.createdAt).toLocaleString('vi-VN')}</span><StatusPill status={item.status.replace('_', ' ')} /><ExternalLink size={15} /></button>)}</div>{!historyRuns.length && <div className="empty-table"><History size={22} /><strong>Chưa có lượt chạy nào tới môi trường này</strong><span>Bấm "Chạy pipeline" để build một commit và triển khai.</span></div>}</section>{triggered && <div className="toast success-toast"><CheckCircle2 size={17} />{triggered} đã được xếp hàng.</div>}</section> }
  return <><section className="panel repo-strip"><div><span>Repository</span><strong className="mono">{refs?.repositoryUrl ?? '…'}</strong></div>{refs?.error ? <div className="repo-error"><ShieldAlert size={14} />Không đọc được nhánh từ repository: {refs.error}</div> : <div><span>Nhánh</span><strong>{refs ? refs.branches.map((b) => `${b.name} @ ${b.sha.slice(0, 7)}`).join(' · ') || 'không có nhánh' : 'đang đọc…'}</strong></div>}{refs && refs.tags.length > 0 && <div><span>Tag mới nhất</span><strong>{refs.tags.slice(0, 3).map((t) => t.name).join(' · ')}</strong></div>}</section><div className="pipeline-card-grid">{definitions.map((pipeline) => {
    const live = runsForPipeline(pipeline)[0]
    const env = pipeline.id.replace('cd-', '')
    return <article className="pipeline-card panel" key={pipeline.id}><div className="pipeline-card-title"><span className={`pipeline-icon pipeline-${pipeline.id}`}><GitBranch size={18} /></span><div><h3>{pipeline.name}</h3><p>build → publish → deploy tới <b>{env}</b> · nhánh {pipeline.branch}{env === 'prod' ? ' · cần phê duyệt' : ''}</p></div><button aria-label={`Mở lịch sử ${pipeline.name}`} onClick={() => setHistoryPipeline(pipeline)}><MoreHorizontal size={18} /></button></div><div className="last-build"><span>Lượt chạy gần nhất</span><strong title={live?.jenkinsRunId ?? live?.id}>{live ? `#${live.jenkinsRunId ? live.jenkinsRunId.split('#').pop() : live.id.slice(0, 8)}` : '—'}</strong><StatusPill status={live?.status.replace('_', ' ') ?? 'chưa chạy'} /></div><dl><div><dt>Commit</dt><dd className="mono" title={live?.commitSha}>{shortSha(live?.commitSha)}{live?.branch ? ` (${live.branch})` : ''}</dd></div><div><dt>Artifact</dt><dd className="mono" title={live?.artifactDigest ?? ''}>{shortDigest(live?.artifactDigest)}</dd></div><div><dt>Bởi</dt><dd title={live?.startedBy ?? ''}>{person(live?.startedBy)}</dd></div><div><dt>Khi nào</dt><dd title={live?.createdAt}>{live ? timeAgo(live.createdAt) : 'chưa có'}</dd></div></dl><footer><button className="secondary-button" onClick={() => setHistoryPipeline(pipeline)}><History size={15} />Lịch sử</button><button className="primary-button" style={{ height: '32px', padding: '0 12px', fontSize: '0.82rem', display: 'flex', alignItems: 'center', gap: '6px' }} disabled={busyPipeline === pipeline.id} aria-label={`Run ${pipeline.name}`} onClick={() => openRunModal(pipeline)}><Play size={14} />Chạy</button></footer></article>
  })}</div>
  {runModalPipeline && (
    <Modal
      title={`Chạy pipeline → ${runModalPipeline.name}`}
      description={`Build commit đã chọn trên Jenkins (pod tạm cho mỗi build), ký & quét artifact, rồi triển khai tới ${modalEnv}${modalEnv === 'prod' ? ' sau khi được phê duyệt' : ''}.`}
      onClose={() => setRunModalPipeline(null)}
      footer={
        <>
          <button className="secondary-button" onClick={() => setRunModalPipeline(null)}>Hủy</button>
          <button className="primary-button" disabled={busyPipeline === runModalPipeline.id || !modalRevision.trim()} onClick={executeModalRun}>
            <Play size={15} />{busyPipeline === runModalPipeline.id ? 'Đang xếp hàng…' : 'Chạy pipeline'}
          </button>
        </>
      }
    >
      <div className="form-grid" style={{ gap: '14px' }}>
        <label className="field full">
          <span>Nhánh</span>
          {refs && refs.branches.length > 0 ? <select value={modalBranch} onChange={(e) => { setModalBranch(e.target.value); setModalRevision(branchSha(e.target.value)) }}>
            {refs.branches.map((b) => <option key={b.name} value={b.name}>{b.name} — {b.sha.slice(0, 7)}</option>)}
            {refs.tags.slice(0, 20).map((t) => <option key={`tag:${t.name}`} value={t.name}>tag {t.name} — {t.sha.slice(0, 7)}</option>)}
          </select> : <input value={modalBranch} onChange={(e) => setModalBranch(e.target.value)} placeholder="main" />}
          <small>{refs?.error ? `Không đọc được repository (${refs.error}); nhập commit thủ công.` : 'Chọn nhánh hoặc tag: netCI điền commit đầu nhánh, đọc từ repository của module.'}</small>
        </label>
        <label className="field full">
          <span>Commit SHA</span>
          <input className="mono" value={modalRevision} onChange={(e) => setModalRevision(e.target.value)} placeholder="7–64 ký tự hex" />
          <small>Commit bất biến được ghi vào lượt chạy và gửi tới Jenkins; có thể sửa để build một commit cũ hơn của nhánh.</small>
        </label>
        <label className="field full">
          <span>Môi trường</span>
          <select value={modalEnv} onChange={(e) => setModalEnv(e.target.value as Environment)}>
            {definitions.map((d) => { const env = d.id.replace('cd-', '') as Environment; return <option key={env} value={env}>{environmentLabel[env] ?? env}{env === 'prod' ? ' (cần phê duyệt)' : ''}</option> })}
          </select>
        </label>
        {modalError && <div className="login-error" role="alert">{modalError}</div>}
      </div>
    </Modal>
  )}
  {triggered && <div className="toast success-toast"><CheckCircle2 size={17} />{triggered} was queued successfully.<button aria-label="Đóng thông báo" onClick={() => setTriggered(null)}>×</button></div>}</>
}

type VersionRow = { tag: string; date: string; user: string; commit: string; coverage: number | null; dev: string; staging: string; prod: string; autoTest: string; signed: boolean; sbom: string; scan: string; promotable: boolean }

function versionRow(item: ModuleVersion): VersionRow {
  return {
    tag: item.version,
    date: item.createdAt ? new Date(item.createdAt).toLocaleString('vi-VN') : 'Not reported',
    user: item.createdBy || 'unknown',
    commit: item.ciReport?.commit || item.artifactDigest || 'Not reported',
    coverage: typeof item.ciReport?.coveragePercentage === 'number' ? item.ciReport.coveragePercentage : null,
    dev: item.environments.dev,
    staging: item.environments.staging,
    prod: item.environments.prod,
    autoTest: item.ciReport?.autoTest || (typeof item.ciReport?.automationPassRate === 'number' ? `${item.ciReport.automationPassRate}%` : 'Not reported'),
    signed: item.signed,
    sbom: item.sbom,
    scan: item.scan,
    promotable: Boolean(item.promotable),
  }
}

function VersionsTab({ moduleId }: { moduleId: string }) {
  const { notify } = usePortalFeedback()
  const [modal, setModal] = useState(false)
  const [tag, setTag] = useState('')
  const [gitTagUrl, setGitTagUrl] = useState('')
  const [artifactUrl, setArtifactUrl] = useState('')
  const [pipelineRunId, setPipelineRunId] = useState('')
  const [artifactDigest, setArtifactDigest] = useState('')
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')
  const [selectedTag, setSelectedTag] = useState<string | null>(null)
  const [versionItems, setVersionItems] = useState<VersionRow[]>([])
  const loadVersions = () => listModuleVersions(moduleId).then((result) => setVersionItems(result.items.map(versionRow))).catch((error) => notify(error instanceof Error ? error.message : 'Unable to load versions.', 'error'))
  useEffect(() => { void loadVersions() }, [moduleId])
  const example = ['{', '  "coverage": 87,', '  "autoTest": "passed",', '  "sast": "passed",', '  "sastIssues": 0,', '  "vulnerabilities": { "critical": 0, "high": 0, "medium": 2 },', '  "commit": "a1c4e2f"', '}'].join('\n')
  const saveVersion = async () => {
    if (!pipelineRunId.trim() || !/^sha256:[0-9a-f]{64}$/.test(artifactDigest)) {
      setFormError('A successful pipeline run ID and its sha256 artifact digest are required.')
      return
    }
    setSaving(true)
    setFormError('')
    try {
      await createModuleVersion(moduleId, { tag, gitTagUrl, artifactUrl, pipelineRunId, artifactDigest })
      await loadVersions()
      setModal(false)
      notify(`${tag} đã được đăng ký. CI có thể gửi quality report.`)
    } catch (error) {
      setFormError(error instanceof Error ? error.message : 'Không thể đăng ký version.')
    } finally {
      setSaving(false)
    }
  }
  return <>
    <div className="tab-toolbar"><div><h2>Versions</h2><p>Register immutable release artifacts and receive CI quality reports.</p></div><button className="primary-button" onClick={() => setModal(true)}><Plus size={16} />New Version</button></div>
    <section className="panel table-panel"><div className="data-table versions-table"><div className="table-row table-head"><span>Version</span><span>Released</span><span>By</span><span>Coverage</span><span>Dev</span><span>Staging</span><span>Production</span><span>Auto Test</span><span /></div>{versionItems.map((version) => <div className="table-row" key={version.tag}><span><strong>{version.tag}</strong><small className="mono">{version.commit}</small></span><span>{version.date}</span><span>{version.user}</span><span className="coverage-cell"><i><b style={{ width: `${version.coverage ?? 0}%` }} /></i>{version.coverage === null ? 'Not reported' : `${version.coverage}%`}</span><StatusPill status={version.dev.replace('_', ' ')} /><StatusPill status={version.staging.replace('_', ' ')} /><StatusPill status={version.prod.replace('_', ' ')} /><StatusPill status={version.autoTest} /><button className="row-more" aria-label={`Chi tiết ${version.tag}`} onClick={() => setSelectedTag(version.tag)}><MoreHorizontal size={17} /></button></div>)}</div>{!versionItems.length && <div className="empty-table"><strong>No release versions</strong><span>Register a verified artifact from a successful pipeline run.</span></div>}</section>
    {modal && <Modal title="Register verified version" description="Link a semantic version to the exact artifact and successful pipeline that produced it." onClose={() => setModal(false)} footer={<><button className="secondary-button" onClick={() => setModal(false)}>Cancel</button><button className="primary-button" disabled={!/^v?\d+\.\d+\.\d+/.test(tag) || !/^https?:\/\//.test(gitTagUrl) || !/^https?:\/\//.test(artifactUrl) || !pipelineRunId.trim() || !/^sha256:[0-9a-f]{64}$/.test(artifactDigest) || saving} onClick={saveVersion}>{saving ? 'Creating…' : 'Register Version'}</button></>}><div className="form-grid"><label className="field full"><span>Version tag</span><input value={tag} onChange={(event) => setTag(event.target.value)} placeholder="v2.5.0" /></label><label className="field full"><span>Source pipeline run ID</span><input value={pipelineRunId} onChange={(event) => setPipelineRunId(event.target.value)} placeholder="UUID of the successful run" /></label><label className="field full"><span>Immutable artifact digest</span><input className="mono" value={artifactDigest} onChange={(event) => setArtifactDigest(event.target.value)} placeholder="sha256:…" /></label><label className="field full"><span>Git tag link</span><input value={gitTagUrl} onChange={(event) => setGitTagUrl(event.target.value)} placeholder="https://git.example.net/.../tags/v2.5.0" /></label><label className="field full"><span>Artifact link</span><input value={artifactUrl} onChange={(event) => setArtifactUrl(event.target.value)} placeholder="https://artifacts.example.net/.../v2.5.0" /></label>{formError && <div className="inline-error full" role="alert">{formError}</div>}</div><div className="api-contract"><div><Code2 size={17} /><strong>CI report integration</strong></div><p>Publish quality metrics after the CI pipeline completes:</p><code>POST /api/modules/{moduleId}/versions/{'{tag}'}/ci-report</code><small>Authorization: Bearer &lt;pipeline-api-key&gt;</small><pre>{example}</pre></div></Modal>}
    {selectedTag && (() => { const version = versionItems.find((item) => item.tag === selectedTag); return version ? <Modal title={version.tag} description="Immutable release evidence recorded by netCI." onClose={() => setSelectedTag(null)} footer={<button className="secondary-button" onClick={() => setSelectedTag(null)}>Close</button>}><div className="review-grid"><div><span>Digest / report</span><strong className="mono">{version.commit}</strong></div><div><span>Released</span><strong>{version.date}</strong></div><div><span>Coverage</span><strong>{version.coverage === null ? 'Not reported' : `${version.coverage}%`}</strong></div><div><span>Promotion</span><strong>{version.promotable ? 'Eligible' : 'Blocked'}</strong></div><div><span>Signature</span><strong>{version.signed ? 'Verified' : 'Not verified'}</strong></div><div><span>SBOM / scan</span><strong>{version.sbom} / {version.scan}</strong></div></div></Modal> : null })()}
  </>
}

function DoraTab({ moduleId }: { moduleId: string }) {
  const { notify } = usePortalFeedback()
  const [metrics, setMetrics] = useState<DoraCardMetric[]>([])
  const [loading, setLoading] = useState(false)
  const [updatedAt, setUpdatedAt] = useState('not loaded')
  const [windowLabel, setWindowLabel] = useState('server-defined window')
  // null until the API answers: the card must never imply these came from real events.
  const [sourceEventCount, setSourceEventCount] = useState<number | null>(null)

  const load = async () => {
    setLoading(true)
    try {
      const result = await getDora('modules', moduleId)
      const tones: Record<string, string> = { deploymentFrequency: 'purple', leadTime: 'blue', changeFailureRate: 'red', timeToRestoreService: 'green' }
      const next = result.metrics.map((metric) => ({
        key: metric.key,
        label: metric.label,
        value: String(metric.value),
        unit: metric.unit,
        hint: metric.hint,
        tone: tones[metric.key] ?? 'blue',
      }))
      setMetrics(next)
      setSourceEventCount(result.sourceEventCount ?? 0)
      setWindowLabel(`${result.window.from} to ${result.window.to} (${result.window.days} days)`)
      setUpdatedAt(new Date().toLocaleTimeString('vi-VN'))
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Không tải được DORA metrics.', 'error')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [moduleId])

  const provenance = sourceEventCount === null
    ? 'loading delivery-event projection'
    : sourceEventCount === 0
      ? 'no delivery events recorded yet'
      : `projected from ${sourceEventCount} delivery event${sourceEventCount === 1 ? '' : 's'}`

  return <><div className="dora-toolbar"><div><p>{windowLabel} · {provenance} · updated {updatedAt}</p></div><button className="secondary-button" disabled={loading} aria-label="Refresh DORA metrics" onClick={load}><RotateCcw size={15} />{loading ? 'Loading…' : 'Refresh'}</button></div><DoraCards metrics={metrics} /></>
}

function ConfigTab({ moduleId }: { moduleId: string }) {
  const { notify } = usePortalFeedback()
  const [revisions, setRevisions] = useState<ConfigRevision[]>([])
  const [configVersion, setConfigVersion] = useState<number>(1)
  const [driftReport, setDriftReport] = useState<ConfigDriftReport | null>(null)
  const [loadingDrift, setLoadingDrift] = useState(false)

  // Modals
  const [proposeModal, setProposeModal] = useState(false)
  const [diffModal, setDiffModal] = useState<{ from: number; to: number; diff: ConfigRevisionDiff | null } | null>(null)
  const [rejectModal, setRejectModal] = useState<ConfigRevision | null>(null)
  const [rejectReason, setRejectReason] = useState('')
  const [submitting, setSubmitting] = useState(false)

  // Fast apply decoupled lifecycle
  const [fastApplyModal, setFastApplyModal] = useState(false)
  const [fastApplyEnv, setFastApplyEnv] = useState<Environment>('staging')
  const [applyingConfig, setApplyingConfig] = useState(false)
  const [fastApplyResult, setFastApplyResult] = useState<ConfigApplyResponse | null>(null)

  // Propose form state
  const [changeSummary, setChangeSummary] = useState('')
  const [runner, setRunner] = useState('jenkins-primary')
  const [stagesText, setStagesText] = useState('checkout, unit-test, build, sbom, vulnerability-scan, sign, publish, deploy')
  const [devServers, setDevServers] = useState('srv-dev-01.internal')
  const [stagingServers, setStagingServers] = useState('srv-staging-01.internal')
  const [prodServers, setProdServers] = useState('srv-prod-01.internal')
  const [useRawJson, setUseRawJson] = useState(false)
  const [pipelineJsonText, setPipelineJsonText] = useState('{}')
  const [deploymentJsonText, setDeploymentJsonText] = useState('[]')
  const [formError, setFormError] = useState('')

  const loadRevisions = async () => {
    try {
      const data = await listConfigRevisions(moduleId)
      setRevisions(data.items)
      setConfigVersion(data.configVersion)
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Unable to load configuration revisions.', 'error')
    }
  }

  const checkDrift = async () => {
    setLoadingDrift(true)
    try {
      const report = await detectDrift(moduleId)
      setDriftReport(report)
      if (report.hasDrift) {
        notify('Configuration drift detected between active revision and running deployments or DCIM targets.', 'error')
      } else {
        notify('No configuration drift. Desired state matches running deployments and DCIM targets.')
      }
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Unable to run drift detection.', 'error')
    } finally {
      setLoadingDrift(false)
    }
  }

  useEffect(() => {
    void loadRevisions()
    void checkDrift()
  }, [moduleId])

  const openProposeModal = () => {
    const active = revisions.find((r) => r.active) ?? revisions[0]
    if (active) {
      setRunner(String(active.pipelineConfig.runner ?? 'jenkins-primary'))
      const stages = Array.isArray(active.pipelineConfig.stages) ? active.pipelineConfig.stages.join(', ') : 'checkout, unit-test, build'
      setStagesText(stages)
      const getServers = (env: string) => {
        const item = active.deploymentConfig.find((d) => d.environment === env)
        return Array.isArray(item?.servers) ? (item.servers as string[]).join(', ') : ''
      }
      setDevServers(getServers('dev') || 'srv-dev-01.internal')
      setStagingServers(getServers('staging') || 'srv-staging-01.internal')
      setProdServers(getServers('prod') || 'srv-prod-01.internal')
      setPipelineJsonText(JSON.stringify(active.pipelineConfig, null, 2))
      setDeploymentJsonText(JSON.stringify(active.deploymentConfig, null, 2))
    }
    setChangeSummary('')
    setFormError('')
    setProposeModal(true)
  }

  const handlePropose = async () => {
    if (!changeSummary.trim()) {
      setFormError('Please enter a summary of changes for the revision.')
      return
    }
    setSubmitting(true)
    setFormError('')
    try {
      let pipelineConfig: Record<string, unknown> = {}
      let deploymentConfig: Array<Record<string, unknown>> = []

      if (useRawJson) {
        try {
          pipelineConfig = JSON.parse(pipelineJsonText)
          deploymentConfig = JSON.parse(deploymentJsonText)
        } catch {
          setFormError('Invalid JSON format in pipelineConfig or deploymentConfig.')
          setSubmitting(false)
          return
        }
      } else {
        pipelineConfig = {
          runner: runner.trim(),
          stages: stagesText.split(',').map((s) => s.trim()).filter(Boolean),
        }
        deploymentConfig = [
          { environment: 'dev', servers: devServers.split(',').map((s) => s.trim()).filter(Boolean) },
          { environment: 'staging', servers: stagingServers.split(',').map((s) => s.trim()).filter(Boolean) },
          { environment: 'prod', servers: prodServers.split(',').map((s) => s.trim()).filter(Boolean) },
        ]
      }

      const res = await proposeConfigRevision(moduleId, {
        changeSummary: changeSummary.trim(),
        pipelineConfig,
        deploymentConfig,
      })

      setProposeModal(false)
      await loadRevisions()
      if (res.requiresApproval) {
        notify(`Revision #${res.revisionNumber} proposed. Modifies production and requires separate approval.`, 'info')
      } else {
        notify(`Revision #${res.revisionNumber} created and activated successfully!`)
      }
    } catch (error) {
      setFormError(error instanceof Error ? error.message : 'Unable to propose configuration revision.')
    } finally {
      setSubmitting(false)
    }
  }

  const handleApprove = async (revId: string, revNumber: number) => {
    try {
      await approveConfigRevision(moduleId, revId)
      notify(`Revision #${revNumber} approved and activated!`)
      await loadRevisions()
      await checkDrift()
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Unable to approve configuration revision.', 'error')
    }
  }

  const handleReject = async () => {
    if (!rejectModal) return
    if (!rejectReason.trim()) {
      notify('Please provide a reason for rejecting the revision.', 'error')
      return
    }
    try {
      await rejectConfigRevision(moduleId, rejectModal.id, rejectReason.trim())
      notify(`Revision #${rejectModal.revisionNumber} rejected.`)
      setRejectModal(null)
      setRejectReason('')
      await loadRevisions()
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Unable to reject revision.', 'error')
    }
  }

  const handleRollback = async (revNumber: number) => {
    if (!window.confirm(`Are you sure you want to rollback to Revision #${revNumber}? This will create a new immutable revision cloning its configuration.`)) {
      return
    }
    try {
      const res = await rollbackConfigRevision(moduleId, revNumber)
      notify(`Rolled back to revision #${revNumber}. Created revision #${res.revisionNumber}.`)
      await loadRevisions()
      await checkDrift()
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Unable to rollback revision.', 'error')
    }
  }

  const handleViewDiff = async (fromRev: number, toRev: number) => {
    try {
      const diff = await diffConfigRevisions(moduleId, fromRev, toRev)
      setDiffModal({ from: fromRev, to: toRev, diff })
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Unable to compute diff.', 'error')
    }
  }

  const handleFastApply = async () => {
    setApplyingConfig(true)
    try {
      const res = await applyModuleConfig(moduleId, { environment: fastApplyEnv })
      setFastApplyResult(res)
      notify(res.status === 'pending_approval'
        ? `Deployment ${res.deploymentId.slice(0, 8)} to ${fastApplyEnv} is waiting for a reviewer (digest ${res.artifactDigest.slice(0, 19)}…)`
        : `Deployment ${res.deploymentId.slice(0, 8)} to ${fastApplyEnv} started (digest ${res.artifactDigest.slice(0, 19)}…); the worker reports its health`)
      await checkDrift()
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Could not start the deployment', 'error')
    } finally {
      setApplyingConfig(false)
    }
  }

  const activeRev = revisions.find((r) => r.active)
  const pendingRev = revisions.find((r) => r.status === 'pending_approval')

  return (
    <div className="config-tab">
      {pendingRev && (
        <section className="panel" style={{ borderLeft: '4px solid #f59e0b', background: 'rgba(245, 158, 11, 0.08)', marginBottom: 20 }}>
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16 }}>
            <span style={{ color: '#f59e0b', marginTop: 2 }}><ShieldAlert size={24} /></span>
            <div style={{ flex: 1 }}>
              <h3 style={{ margin: '0 0 6px 0', fontSize: '1.1rem' }}>
                Revision #{pendingRev.revisionNumber} requires production change approval
              </h3>
              <p style={{ margin: '0 0 8px 0', color: 'var(--text-secondary)' }}>
                Proposed by <strong>{pendingRev.createdBy}</strong> at {new Date(pendingRev.createdAt).toLocaleString('vi-VN')}: <em>"{pendingRev.changeSummary}"</em>
              </p>
              <small style={{ display: 'block', color: 'var(--text-muted)', marginBottom: 12 }}>
                Separation of duties applies: the author of a production configuration cannot approve their own change.
              </small>
              <div style={{ display: 'flex', gap: 10 }}>
                <button className="primary-button" onClick={() => handleApprove(pendingRev.id, pendingRev.revisionNumber)}>
                  <Check size={15} /> Approve & Activate
                </button>
                <button className="secondary-button" onClick={() => setRejectModal(pendingRev)}>
                  <XCircle size={15} /> Reject
                </button>
                {activeRev && (
                  <button className="secondary-button" onClick={() => handleViewDiff(activeRev.revisionNumber, pendingRev.revisionNumber)}>
                    <ExternalLink size={15} /> View Changes Diff
                  </button>
                )}
              </div>
            </div>
          </div>
        </section>
      )}

      <div className="tab-toolbar" style={{ marginBottom: 16 }}>
        <div>
          <h2>Environment & Pipeline Configuration</h2>
          <p>
            Immutable, versioned domain configuration with compare-and-set pointer (CAS Version: {configVersion}) and DCIM lifecycle validation.
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button
            className="secondary-button"
            id="btn-fast-apply-config"
            style={{ borderColor: 'var(--accent)', color: 'var(--accent)' }}
            onClick={() => { setFastApplyResult(null); setFastApplyModal(true) }}
          >
            <Zap size={15} /> Fast Apply Config
          </button>
          <button className="secondary-button" disabled={loadingDrift} onClick={checkDrift}>
            <RotateCcw size={15} /> {loadingDrift ? 'Detecting…' : 'Check Drift'}
          </button>
          <button className="primary-button" onClick={openProposeModal}>
            <Plus size={16} /> Propose Revision
          </button>
        </div>
      </div>

      {driftReport && (
        <section className="panel" style={{ marginBottom: 20, borderLeft: driftReport.hasDrift ? '4px solid #ef4444' : '4px solid #10b981' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{ color: driftReport.hasDrift ? '#ef4444' : '#10b981' }}>
                {driftReport.hasDrift ? <XCircle size={20} /> : <CheckCircle2 size={20} />}
              </span>
              <strong>{driftReport.hasDrift ? 'Configuration Drift Detected' : 'Configuration In Sync'}</strong>
            </div>
            <small style={{ color: 'var(--text-muted)' }}>
              Targeting active revision #{activeRev?.revisionNumber ?? '—'}
            </small>
          </div>
          {driftReport.hasDrift ? (
            <div style={{ fontSize: '0.9rem', color: 'var(--text-secondary)' }}>
              {driftReport.deploymentDrift.filter(d => d.drifted).map((d, i) => (
                <div key={`dep-${i}`} style={{ marginBottom: 4 }}>
                  • <strong>{d.environment} deployment</strong>: running revision {d.runningConfigRevisionId?.slice(0, 8) ?? 'none'} does not match active desired revision {d.desiredConfigRevisionId?.slice(0, 8) ?? 'none'}. {d.reason}
                </div>
              ))}
              {driftReport.dcimDrift.filter(d => d.drifted).map((d, i) => (
                <div key={`dcim-${i}`} style={{ marginBottom: 4 }}>
                  • <strong>{d.environment} target host ({d.server})</strong>: DCIM status is <code>{d.dcimStatus}</code> ({d.message}).
                </div>
              ))}
            </div>
          ) : (
            <p style={{ margin: 0, fontSize: '0.88rem', color: 'var(--text-muted)' }}>
              All running deployment environments match the active desired configuration revision, and all DCIM target hosts are validated online.
            </p>
          )}
        </section>
      )}

      {activeRev && (
        <section className="panel" style={{ marginBottom: 24, padding: 18 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
                <h3 style={{ margin: 0 }}>Active: Revision #{activeRev.revisionNumber}</h3>
                <StatusPill status="active" />
                <span className="mono" style={{ fontSize: '0.8rem', opacity: 0.7 }}>({activeRev.id.slice(0, 8)})</span>
              </div>
              <p style={{ margin: '0 0 6px 0', color: 'var(--text-secondary)' }}>{activeRev.changeSummary}</p>
              <small style={{ color: 'var(--text-muted)' }}>
                Created by {activeRev.createdBy} on {new Date(activeRev.createdAt).toLocaleString('vi-VN')}
                {activeRev.approvedBy && ` · Approved by ${activeRev.approvedBy}`}
              </small>
            </div>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginTop: 16, paddingTop: 16, borderTop: '1px solid var(--border)' }}>
            <div>
              <strong style={{ fontSize: '0.85rem', textTransform: 'uppercase', letterSpacing: 0.5, opacity: 0.8 }}>Pipeline Settings</strong>
              <pre style={{ margin: '8px 0 0 0', padding: 10, borderRadius: 6, background: 'var(--bg-card)', fontSize: '0.8rem' }}>
                {JSON.stringify(activeRev.pipelineConfig, null, 2)}
              </pre>
            </div>
            <div>
              <strong style={{ fontSize: '0.85rem', textTransform: 'uppercase', letterSpacing: 0.5, opacity: 0.8 }}>Deployment Target Environments</strong>
              <pre style={{ margin: '8px 0 0 0', padding: 10, borderRadius: 6, background: 'var(--bg-card)', fontSize: '0.8rem' }}>
                {JSON.stringify(activeRev.deploymentConfig, null, 2)}
              </pre>
            </div>
          </div>
        </section>
      )}

      <div className="panel-heading" style={{ marginBottom: 12 }}>
        <div>
          <h3>Configuration Revision History</h3>
          <p>Complete immutable audit log of all proposed, approved, and rejected revisions.</p>
        </div>
      </div>

      <section className="panel table-panel">
        <div className="data-table">
          <div className="table-row table-head">
            <span>Rev</span>
            <span>Status</span>
            <span>Author</span>
            <span>Created</span>
            <span>Summary</span>
            <span style={{ textAlign: 'right' }}>Actions</span>
          </div>
          {revisions.map((rev) => (
            <div className="table-row" key={rev.id}>
              <span>
                <strong>#{rev.revisionNumber}</strong>
                {rev.active && <small style={{ color: '#10b981', display: 'block', fontWeight: 600 }}>Active</small>}
              </span>
              <StatusPill status={rev.status.replace('_', ' ')} />
              <span>{rev.createdBy}</span>
              <span style={{ fontSize: '0.85rem' }}>{new Date(rev.createdAt).toLocaleString('vi-VN')}</span>
              <span className="truncate" title={rev.changeSummary}>{rev.changeSummary}</span>
              <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
                {activeRev && rev.revisionNumber !== activeRev.revisionNumber && (
                  <button
                    className="secondary-button"
                    style={{ padding: '4px 8px', fontSize: '0.8rem' }}
                    onClick={() => handleViewDiff(rev.revisionNumber, activeRev.revisionNumber)}
                  >
                    Diff with Active
                  </button>
                )}
                {!rev.active && rev.status !== 'rejected' && (
                  <button
                    className="secondary-button"
                    style={{ padding: '4px 8px', fontSize: '0.8rem' }}
                    onClick={() => handleRollback(rev.revisionNumber)}
                  >
                    Rollback
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      </section>

      {proposeModal && (
        <Modal
          title="Propose Configuration Revision"
          description="Create a new immutable revision. Changes to production will require separate approval."
          onClose={() => setProposeModal(false)}
          footer={
            <>
              <button className="secondary-button" onClick={() => setProposeModal(false)}>Cancel</button>
              <button className="primary-button" disabled={submitting || !changeSummary.trim()} onClick={handlePropose}>
                {submitting ? 'Proposing…' : 'Propose Revision'}
              </button>
            </>
          }
        >
          <div className="form-grid">
            <label className="field full">
              <span>Change Summary *</span>
              <input
                value={changeSummary}
                onChange={(e) => setChangeSummary(e.target.value)}
                placeholder="Describe what changed and why (e.g., scale up prod workers, add staging host)"
              />
            </label>

            <div className="field full" style={{ display: 'flex', justifyContent: 'flex-end' }}>
              <button
                type="button"
                className="secondary-button"
                style={{ fontSize: '0.8rem', padding: '4px 8px' }}
                onClick={() => setUseRawJson(!useRawJson)}
              >
                {useRawJson ? 'Switch to Form Fields' : 'Switch to Raw JSON Editor'}
              </button>
            </div>

            {useRawJson ? (
              <>
                <label className="field full">
                  <span>pipelineConfig (JSON)</span>
                  <textarea
                    rows={6}
                    className="mono"
                    value={pipelineJsonText}
                    onChange={(e) => setPipelineJsonText(e.target.value)}
                  />
                </label>
                <label className="field full">
                  <span>deploymentConfig (JSON Array)</span>
                  <textarea
                    rows={6}
                    className="mono"
                    value={deploymentJsonText}
                    onChange={(e) => setDeploymentJsonText(e.target.value)}
                  />
                </label>
              </>
            ) : (
              <>
                <label className="field">
                  <span>Pipeline Runner</span>
                  <input value={runner} onChange={(e) => setRunner(e.target.value)} />
                </label>
                <label className="field full">
                  <span>Pipeline Stages (comma-separated)</span>
                  <input value={stagesText} onChange={(e) => setStagesText(e.target.value)} />
                </label>
                <label className="field full">
                  <span>Development Target Servers (comma-separated)</span>
                  <input value={devServers} onChange={(e) => setDevServers(e.target.value)} />
                </label>
                <label className="field full">
                  <span>Staging Target Servers (comma-separated)</span>
                  <input value={stagingServers} onChange={(e) => setStagingServers(e.target.value)} />
                </label>
                <label className="field full">
                  <span>Production Target Servers (comma-separated, triggers approval)</span>
                  <input value={prodServers} onChange={(e) => setProdServers(e.target.value)} />
                </label>
              </>
            )}

            {formError && <div className="inline-error full" role="alert">{formError}</div>}
          </div>
        </Modal>
      )}

      {diffModal && (
        <Modal
          title={`Diff: Revision #${diffModal.from} → Revision #${diffModal.to}`}
          description="Structural differences between configuration revisions."
          onClose={() => setDiffModal(null)}
          footer={<button className="secondary-button" onClick={() => setDiffModal(null)}>Close</button>}
        >
          {diffModal.diff && (
            <div>
              <p style={{ marginBottom: 12 }}>
                <strong>{diffModal.diff.changeCount}</strong> change{diffModal.diff.changeCount === 1 ? '' : 's'} recorded:
              </p>
              {diffModal.diff.changes.length === 0 ? (
                <div style={{ color: 'var(--text-muted)' }}>No differences found between these revisions.</div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  {diffModal.diff.changes.map((change, idx) => (
                    <div key={idx} style={{ padding: 10, borderRadius: 6, background: 'var(--bg-card)', border: '1px solid var(--border)' }}>
                      <div className="mono" style={{ fontWeight: 600, color: 'var(--accent-purple)', marginBottom: 6 }}>
                        {change.path}
                      </div>
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, fontSize: '0.85rem' }}>
                        <div style={{ background: 'rgba(239, 68, 68, 0.1)', padding: 8, borderRadius: 4, color: '#f87171' }}>
                          <small style={{ display: 'block', fontWeight: 600, marginBottom: 2 }}>FROM (Rev #{diffModal.from})</small>
                          <pre style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{JSON.stringify(change.from, null, 2)}</pre>
                        </div>
                        <div style={{ background: 'rgba(16, 185, 129, 0.1)', padding: 8, borderRadius: 4, color: '#34d399' }}>
                          <small style={{ display: 'block', fontWeight: 600, marginBottom: 2 }}>TO (Rev #{diffModal.to})</small>
                          <pre style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{JSON.stringify(change.to, null, 2)}</pre>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </Modal>
      )}

      {rejectModal && (
        <Modal
          title={`Reject Revision #${rejectModal.revisionNumber}`}
          description="Provide a justification for rejecting this configuration change."
          onClose={() => setRejectModal(null)}
          footer={
            <>
              <button className="secondary-button" onClick={() => setRejectModal(null)}>Cancel</button>
              <button className="primary-button" style={{ background: '#ef4444' }} onClick={handleReject}>
                Confirm Rejection
              </button>
            </>
          }
        >
          <div className="form-grid">
            <label className="field full">
              <span>Rejection Reason *</span>
              <textarea
                rows={4}
                value={rejectReason}
                onChange={(e) => setRejectReason(e.target.value)}
                placeholder="Explain why this change is rejected (e.g. invalid server host, security policy violation)"
              />
            </label>
          </div>
        </Modal>
      )}

      {fastApplyModal && (
        <Modal
          title="Redeploy with the active configuration"
          description="Starts a real deployment of the artifact this environment last built (digest-pinned, its evidence re-evaluated and its signature re-verified by the worker) under the active configuration revision, without rebuilding. Production still requires a reviewer's approval."
          onClose={() => setFastApplyModal(false)}
          footer={
            <>
              <button className="secondary-button" onClick={() => setFastApplyModal(false)}>Close</button>
              <button
                className="primary-button"
                id="btn-confirm-fast-apply"
                disabled={applyingConfig}
                onClick={handleFastApply}
                style={{ background: 'linear-gradient(135deg, #10b981 0%, #059669 100%)' }}
              >
                {applyingConfig ? 'Starting…' : 'Start deployment'}
              </button>
            </>
          }
        >
          <div className="form-grid">
            <div className="field full">
              <div style={{ padding: 12, borderRadius: 8, background: 'rgba(16, 185, 129, 0.08)', border: '1px solid rgba(16, 185, 129, 0.2)', marginBottom: 8 }}>
                <div style={{ fontWeight: 600, color: '#10b981', marginBottom: 4 }}>What this does</div>
                <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
                  A new deployment of the last artifact this environment built, with the active revision's targets and runtime settings. It takes a lease, goes through the same policy, approval and worker gates as any deployment, and is reported healthy only by the worker. If nothing was ever built for this environment it is refused.
                </div>
              </div>
            </div>

            <label className="field full">
              <span>Target Environment *</span>
              <select
                id="fast-apply-env-select"
                value={fastApplyEnv}
                onChange={(e) => setFastApplyEnv(e.target.value as Environment)}
              >
                <option value="dev">dev (Development)</option>
                <option value="staging">staging (Staging / Pre-prod)</option>
                <option value="prod">prod (Production)</option>
              </select>
            </label>

            <div className="field full" style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>
              Active Revision to Apply: <strong>Revision #{activeRev?.revisionNumber ?? 1}</strong> ({activeRev?.id.slice(0, 8) ?? 'latest'})
            </div>

            {fastApplyResult && (
              <div className="field full" id="fast-apply-result-box" style={{ padding: 12, borderRadius: 6, background: 'rgba(2, 132, 199, 0.08)', border: '1px solid #0284c7' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, color: '#0284c7', fontWeight: 600, marginBottom: 6 }}>
                  <CheckCircle2 size={18} /> {fastApplyResult.status === 'pending_approval' ? 'Deployment created — waiting for a reviewer' : 'Deployment started — waiting for the worker'}
                </div>
                <div style={{ fontSize: '0.85rem' }}>
                  <div>• <strong>Deployment ID:</strong> <code className="mono">{fastApplyResult.deploymentId}</code></div>
                  <div>• <strong>Artifact (reused, digest-pinned):</strong> <code className="mono">{fastApplyResult.artifactDigest}</code></div>
                  <div>• <strong>Built by run:</strong> <code className="mono">{fastApplyResult.sourcePipelineRunId}</code> · revision #{fastApplyResult.revisionNumber}</div>
                  <div>• <strong>Status:</strong> <span className="mono">{fastApplyResult.status}</span> — becomes <span className="mono">healthy</span> only when the worker reports it</div>
                  {fastApplyResult.riskLevel && <div>• <strong>Risk:</strong> {fastApplyResult.riskLevel}{fastApplyResult.riskReasons?.length ? ` (${fastApplyResult.riskReasons.join('; ')})` : ''}</div>}
                  <div style={{ color: 'var(--text-muted)', marginTop: 4 }}>{fastApplyResult.message}</div>
                </div>
              </div>
            )}
          </div>
        </Modal>
      )}
    </div>
  )
}

type ModuleView = { id: string; name: string; type: string; description: string; runtime: Runtime; versions: string[]; activityCount: number; pipelineConfig: Partial<ModulePipelineConfig>; deploymentEnvironments?: DeploymentEnvironmentConfig[] }

export function ModulePage({ moduleId, onSettings }: { moduleId: string; onSettings: () => void }) {
  const [module, setModule] = useState<ModuleView>(() => ({ id: moduleId, name: moduleId, type: 'Module', description: 'Loading module data from netCI.', runtime: 'docker', versions: [], activityCount: 0, pipelineConfig: {} }))
  const [tab, setTab] = useState<ModuleTab>('overview')
  useEffect(() => {
    getModule(moduleId).then((item) => setModule({ id: item.id, name: item.name, type: item.type, description: item.description, runtime: item.runtime, versions: item.versions, activityCount: item.pipelineRuns.length, pipelineConfig: item.pipelineConfig, deploymentEnvironments: item.deploymentEnvironments })).catch(() => undefined)
  }, [moduleId])
  const moduleCode = module.id.toUpperCase().replace(/-/g, '_')
  return <>
    <div className="module-heading"><div className="module-title"><span className="module-icon purple"><Box size={20} /></span><div><div className="title-status"><h1>{module.name}</h1><span className="type-badge purple">{module.type}</span></div><p>{module.description}</p><small>Module code: {moduleCode} · Runtime: {module.runtime}</small></div></div><button className="secondary-button" onClick={onSettings}><Settings size={16} />Settings</button></div>
    <nav className="tabs" role="tablist" aria-label="Module views">{([['overview', 'Overview'], ['pipeline', 'Pipeline'], ['version', 'Version'], ['config', 'Configuration'], ['dora', 'DORA Metrics']] as [ModuleTab, string][]).map(([id, label]) => <button role="tab" aria-selected={tab === id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)} key={id}>{label}</button>)}</nav>
    <div className="tab-content" role="tabpanel">{tab === 'overview' && <OverviewTab moduleId={moduleId} />}{tab === 'pipeline' && <PipelineTab moduleId={moduleId} pipelineConfig={module.pipelineConfig ?? {}} deploymentEnvironments={module.deploymentEnvironments} />}{tab === 'version' && <VersionsTab moduleId={moduleId} />}{tab === 'config' && <ConfigTab moduleId={moduleId} />}{tab === 'dora' && <DoraTab moduleId={moduleId} />}</div>
  </>
}
