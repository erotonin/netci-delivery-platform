import { useEffect, useState } from 'react'
import {
  ArrowLeft, Box, Check, CheckCircle2, Code2, Copy, ExternalLink, GitBranch,
  History, MoreHorizontal, Play, Plus, RotateCcw, Settings, ShieldAlert, Sliders,
  TerminalSquare, XCircle, Zap, ZoomIn, ZoomOut,
} from 'lucide-react'
import {
  approveConfigRevision, approvePipelineRun, cancelPipelineRun, createModuleVersion, detectDrift,
  diffConfigRevisions, getDora, getModule, getModuleGitCommits, getModuleGitRefs, getModuleInsights, getModuleOverview, getModuleScorecard, getPipelineLogs,
  getModuleDeliveryRules, getPipelineRun, getPipelineStages, listConfigRevisions, listModulePipelineRuns, listModuleVersions, promoteModuleArtifact,
  proposeConfigRevision, rejectConfigRevision, retryPipelineRun, rollbackConfigRevision,
  startModulePipeline, applyModuleConfig, whoami, NetciApiError, type ConfigApplyResponse, type ConfigDriftReport, type ConfigRevision, type ConfigRevisionDiff,
  listDcimServers, type DcimServer, type RuntimeSettings, type DeploymentEnvironmentConfig, type Environment, type GitCommits, type GitRefs, type ModuleInsights, type ModuleOverview, type ModulePipelineConfig, type ModuleScorecard, type ModuleVersion,
  type DeliveryRules, type PipelineRun, type PipelineStage, type Runtime,
} from './api/netciClient'
import { usePortalFeedback } from './PortalFeedback'
import { DoraCards, Modal, StatusPill } from './PortalShell'
import type { DoraCardMetric, ModuleTab } from './portalTypes'
import { PipelineStagesSettings } from './ModuleSettings'

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
        <div><span>Kiểm thử của build</span><strong>{q.ciReport?.autoTest
          ? `${q.ciReport.autoTest}${typeof q.ciReport.testsRun === 'number' ? ` · ${q.ciReport.testsRun} test` : ''}${q.ciReport.runner ? ` (${q.ciReport.runner})` : ''}${typeof (q.ciReport.coveragePercentage ?? q.ciReport.coverage) === 'number' ? ` · coverage ${q.ciReport.coveragePercentage ?? q.ciReport.coverage}%` : ''}`
          : 'pipeline không gửi kết quả test'}</strong></div>
      </div>}
    </section>
    <div className="module-overview-grid">
      <section className="panel"><div className="panel-heading"><div><h2>Build gần đây</h2><p>{overview.recentRuns.length} lượt chạy mới nhất</p></div></div>
        <div className="data-table history-table compact"><div className="table-row table-head"><span>Trạng thái</span><span>Môi trường</span><span>Commit</span><span>Artifact</span><span>Bởi</span><span>Khi nào</span></div>
          {overview.recentRuns.map((run) => <button className="table-row table-button" key={run.id} onClick={() => onOpenRun?.(run)} title={run.id}><RunStatus run={run} /><span>{run.environment}</span><span className="mono">{shortSha(run.commitSha)}</span><span className="mono">{shortDigest(run.artifactDigest)}</span><span title={run.startedBy ?? ''}>{person(run.startedBy)}</span><span title={run.createdAt}>{timeAgo(run.createdAt)}</span></button>)}
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

// "jenkins-a:netci-<uuid>#3" is the controller's name for the run; the user reads
// "#3" and finds the rest in the subtitle.
function buildLabel(run: PipelineRun): string {
  const jenkins = run.jenkinsRunId ?? ''
  const match = /#(\d+)$/.exec(jenkins)
  return match ? `#${match[1]}` : `#${run.id.slice(0, 8)}`
}

/** What started a run, in a few words: the server's record, not a guess from the branch. */
export function triggerText(run: PipelineRun): string {
  const t = run.trigger ?? {}
  switch (t.event) {
    case 'push': return `push ${t.branch ?? run.branch}`
    case 'tag': return `tag ${t.tag ?? ''}`
    case 'pull_request': return `PR #${t.pullRequest ?? '?'} → ${t.baseBranch ?? 'main'}${t.fromFork ? ' (fork)' : ''}`
    case 'manual': return 'manual'
    default: return 'manual'
  }
}

/** Whether the run deploys, only builds, or only verifies (ADR-043). */
export function runIntent(run: PipelineRun): 'deploys' | 'build only' | 'verify only' {
  if (run.publishArtifact === false) return 'verify only'
  if (run.deployAfterBuild === false) return 'build only'
  return 'deploys'
}

/** A run whose artifact can be moved onward: it built and published something. */
export function isPromotable(run: PipelineRun | null): boolean {
  return !!run && run.publishArtifact !== false && !!run.artifactDigest
    && (run.status === 'succeeded' || run.status === 'rolled_back')
}

/** A queued run the server has not admitted to CI: its quota scope is full (ADR-050). Only
 * an explicit null says so -- a response without the field is not read as waiting. */
export function isWaitingForAdmission(run: PipelineRun): boolean {
  return run.status === 'queued' && run.admittedAt === null
}

const shortRunId = (id: string) => `#${id.slice(0, 8)}`

/** A run's state as the server recorded it. A run waiting for admission has not reached
 * Jenkins, so it must never read as dispatched or running; a superseded run names the run
 * that replaced it, so a cancellation nobody asked for is not mistaken for a failure. */
export function RunStatus({ run }: { run: PipelineRun }) {
  if (isWaitingForAdmission(run)) {
    return <span className="status status-waiting-admission" title="Waiting for admission: quota của phạm vi này đã đủ build đồng thời; run chưa được gửi tới Jenkins (ADR-050)."><i />chờ tới lượt</span>
  }
  if (run.status === 'cancelled' && run.supersededBy) {
    return <span className="status status-cancelled" title={`Cancelled: superseded by run ${run.supersededBy}`}><i />superseded by {shortRunId(run.supersededBy)}</span>
  }
  return <StatusPill status={run.status.replace('_', ' ')} />
}

function describeTrigger(rule: DeliveryRules['triggers'][number]): string {
  const patterns = (rule.on === 'tag' ? rule.tags : rule.branches) ?? []
  const what = rule.deployTo ? `build → deploy ${rule.deployTo}` : 'build only'
  // Stated only when the rule sets them: the server serialises what the module declared.
  const options = [
    rule.cancelInProgress === undefined ? '' : ` · cancelInProgress: ${rule.cancelInProgress}`,
    rule.paths?.length ? ` · paths: ${rule.paths.join(', ')}` : '',
    rule.pathsIgnore?.length ? ` · pathsIgnore: ${rule.pathsIgnore.join(', ')}` : '',
  ].join('')
  return `${rule.on} ${patterns.join(', ')} → ${what}${rule.registerVersion ? ' + register version' : ''}${options}`
}

/** The rules in force, stated as the server evaluates them -- first match wins. */
export function DeliveryFlowPanel({ moduleId }: { moduleId: string }) {
  const [rules, setRules] = useState<DeliveryRules | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    getModuleDeliveryRules(moduleId)
      .then((result) => { if (active) setRules(result) })
      .catch((cause) => { if (active) setError(cause instanceof Error ? cause.message : String(cause)) })
    return () => { active = false }
  }, [moduleId])
  if (error) return <section className="panel delivery-flow" role="alert">Could not read delivery rules: {error}</section>
  if (!rules) return <section className="panel delivery-flow">Reading delivery rules…</section>
  return <section className="panel delivery-flow" data-testid="delivery-flow">
    <h3>Delivery flow{rules.defaulted ? ' (defaults)' : ''}</h3>
    <ol>{rules.triggers.map((rule, i) => <li key={i}>{describeTrigger(rule)}</li>)}</ol>
    {/* A path filter reads as "this change was skipped" unless it says when it is not applied (ADR-051). */}
    {rules.triggers.some((rule) => rule.paths?.length || rule.pathsIgnore?.length) && <p>Path filters skip a run only when the SCM reported every changed file; otherwise the rule matches and the run says so. Pull requests always build in full.</p>}
    <p>Fork pull requests: {rules.forkPullRequests === 'ignore' ? 'ignored' : 'verified only — never signed or published'}.</p>
    <p>{Object.entries(rules.promotion).map(([env, rule]) => rule.requireHealthyIn
      ? `${env} needs the artifact healthy in ${rule.requireHealthyIn}${rule.minSoakMinutes ? ` for ${rule.minSoakMinutes} min` : ''}`
      : `${env}: no prior environment required`).join(' · ') || 'No promotion rules.'} Production is reached only through a production request.</p>
  </section>
}

function PipelineRunView({
  pipeline,
  liveRun,
  moduleId,
  onBack,
  onBackToEnvironments,
  onRunUpdated,
  onOpenRun,
}: {
  pipeline: PipelineDefinition
  liveRun: PipelineRun | null
  moduleId?: string
  onBack: () => void
  /** Straight back to the environment list. Without it, leaving a run took two hops. */
  onBackToEnvironments: () => void
  onRunUpdated: (updated: PipelineRun) => void
  /** Open another run by id -- the one that superseded this run. */
  onOpenRun?: (runId: string) => void
}) {
  const { notify } = usePortalFeedback()
  const [stage, setStage] = useState(pipeline.stages[0] ?? 'Checkout')
  const [zoom, setZoom] = useState(1)
  const selectedStageLabel = stageLabels[stage] ?? stage
  const [log, setLog] = useState('No log lines have been reported for this run.')
  const [stages, setStages] = useState<PipelineStage[]>([])
  const [cancelling, setCancelling] = useState(false)
  const [retrying, setRetrying] = useState(false)
  const [promoting, setPromoting] = useState<Environment | null>(null)
  const promote = async (environment: Environment) => {
    if (!liveRun || !moduleId) return
    setPromoting(environment)
    try {
      const result = await promoteModuleArtifact(moduleId, { pipelineRunId: liveRun.id, environment })
      notify(`Promoting ${result.artifactDigest.slice(0, 19)} to ${environment}: deployment ${result.deploymentId.slice(0, 8)} is ${result.status}.`)
    } catch (cause) {
      // The server's refusal is the useful part: which environment it wanted evidence from.
      notify(cause instanceof Error ? cause.message : String(cause), 'error')
    } finally {
      setPromoting(null)
    }
  }

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
            // Admission changes no status (queued stays queued), so it is compared on its own:
            // without it a run admitted while open kept saying it was waiting.
            if (matched && (matched.status !== liveRun.status || matched.jenkinsRunId !== liveRun.jenkinsRunId
              || matched.admittedAt !== liveRun.admittedAt || matched.supersededBy !== liveRun.supersededBy)) {
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

  const [approving, setApproving] = useState(false)

  const handleApprove = async () => {
    if (!liveRun) return
    setApproving(true)
    try {
      await approvePipelineRun(liveRun.id)
      notify('Đã phê duyệt triển khai Production! CD workflow đã bắt đầu.')
      if (moduleId) {
        listModulePipelineRuns(moduleId)
          .then((res) => {
            const matched = res.items.find((r) => r.id === liveRun.id)
            if (matched) onRunUpdated(matched)
          })
          .catch(() => {})
      }
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Không thể phê duyệt triển khai.', 'error')
    } finally {
      setApproving(false)
    }
  }

  const canCancel = !!liveRun && (liveRun.status === 'queued' || liveRun.status === 'running' || liveRun.status === 'waiting_approval')
  const canRetry = !!liveRun && (liveRun.status === 'failed' || liveRun.status === 'cancelled' || liveRun.status === 'rolled_back' || liveRun.status === 'succeeded')

  return <section className="run-view">
    {/* Leaving a run used to mean two hops: back to build history, then back to the
        environment list. Each step is its own control now, and the one people want most
        -- all the way out -- is first. */}
    <nav className="run-breadcrumb" aria-label="Breadcrumb">
      <button className="back-button" onClick={onBackToEnvironments}><ArrowLeft size={16} />All environments</button>
      <span aria-hidden="true">/</span>
      <button className="back-button" onClick={onBack}>{pipeline.name}</button>
      <span aria-hidden="true">/</span>
      <span className="run-breadcrumb-current">{liveRun ? `build ${buildLabel(liveRun)}` : 'new run'}</span>
    </nav>
    <div className="run-heading">
      <div>
        <div className="title-status">
          <h2>{pipeline.name} {liveRun ? `· build ${buildLabel(liveRun)}` : ''}</h2>
          {liveRun ? <RunStatus run={liveRun} /> : <StatusPill status="Not started" />}
          {liveRun?.retryOf && <span className="mono" style={{ marginLeft: 8, fontSize: '0.85em', opacity: 0.8 }}>(retry of #{liveRun.retryOf.slice(0, 8)})</span>}
        </div>
        <p>{liveRun ? `${liveRun.branch} · ${liveRun.commitSha} · ${triggerText(liveRun)} · ${runIntent(liveRun)} · triggered by ${liveRun.startedBy ?? 'unknown actor'}` : 'No pipeline run selected.'}{liveRun?.releaseTag && <> · version <b>{liveRun.releaseTag}</b></>}{liveRun?.jenkinsRunId && <><br /><span className="mono" style={{ opacity: 0.7 }}>Jenkins: {liveRun.jenkinsRunId}</span></>}{liveRun?.trigger?.reason && <><br /><small>{liveRun.trigger.reason}{typeof liveRun.trigger.changedFiles === 'number' ? ` · ${liveRun.trigger.changedFiles} changed file${liveRun.trigger.changedFiles === 1 ? '' : 's'}` : ''}</small></>}</p>
        {liveRun && isWaitingForAdmission(liveRun) && <p className="run-notice" role="status" data-testid="run-admission-notice">
          Chờ tới lượt: quota của phạm vi này đã đủ build đồng thời, nên run chưa được gửi tới Jenkins. netCI admit run khi một build khác kết thúc (ADR-050).
          {liveRun.concurrencyGroup ? <> Một commit mới hơn của <span className="mono">{liveRun.concurrencyGroup}</span> sẽ thay thế run này.</> : null}
        </p>}
        {liveRun?.status === 'cancelled' && liveRun.supersededBy && <p className="run-notice" data-testid="run-superseded-notice">
          Superseded by {onOpenRun
            ? <button className="link-button mono" onClick={() => onOpenRun(liveRun.supersededBy!)}>{shortRunId(liveRun.supersededBy)}</button>
            : <span className="mono" title={liveRun.supersededBy}>{shortRunId(liveRun.supersededBy)}</span>}
          {liveRun.concurrencyGroup ? <>: a newer run of <span className="mono">{liveRun.concurrencyGroup}</span> cancelled this one.</> : '.'}
        </p>}
      </div>
      <div className="run-actions">
        {liveRun?.status === 'waiting_approval' && (
          <button
            className="primary-button"
            style={{ backgroundColor: '#10b981', color: '#fff' }}
            disabled={approving}
            onClick={handleApprove}
          >
            <Check size={15} />{approving ? 'Approving…' : 'Approve & Deploy'}
          </button>
        )}
        {canCancel && (
          <button className="secondary-button" disabled={cancelling} onClick={handleCancel}>
            <XCircle size={15} />{cancelling ? 'Cancelling…' : 'Cancel'}
          </button>
        )}
        {moduleId && isPromotable(liveRun) && (['dev', 'staging'] as Environment[]).map((environment) => (
          <button key={environment} className="secondary-button" data-testid={`promote-${environment}`}
            disabled={promoting !== null} onClick={() => promote(environment)}
            title={`Deploy this exact artifact to ${environment} without rebuilding it`}>
            <Zap size={15} />{promoting === environment ? 'Promoting…' : `Promote to ${environment}`}
          </button>
        ))}
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
          const observedStatus = observed ? observed.status.toLowerCase() : 'pending'
          return (
            <button
              className={`stage-node category-${stageCategory(stageId)} status-${observedStatus} ${stage === stageId ? 'selected' : ''}`}
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
  const [showStagesEditor, setShowStagesEditor] = useState(false)
  const [refs, setRefs] = useState<GitRefs | null>(null)
  // 'loading' until the request settles. A failed request used to leave `refs`
  // null and the hint still promising that netCI would fill the commit in, which
  // it then could not do -- an empty field and a sentence that was not true.
  const [refsState, setRefsState] = useState<'loading' | 'ready' | 'unreachable'>('loading')

  useEffect(() => {
    let active = true
    listModulePipelineRuns(moduleId).then((result) => { if (active) setLiveRuns([...result.items].sort((a, b) => b.createdAt.localeCompare(a.createdAt))) }).catch((error) => { if (active) notify(error instanceof Error ? error.message : 'Không tải được pipeline history.', 'error') })
    // Branches and tags come from the module's own repository, read by the server. The
    // Portal used to offer netCI's *own* build commit here, which does not exist in the
    // module's repository, so the default "Run" failed at checkout.
    getModuleGitRefs(moduleId)
      .then((result) => { if (active) { setRefs(result); setRefsState('ready') } })
      .catch(() => { if (active) { setRefs(null); setRefsState('unreachable') } })
    return () => { active = false }
  }, [moduleId])
  const [runModalPipeline, setRunModalPipeline] = useState<PipelineDefinition | null>(null)
  const [modalBranch, setModalBranch] = useState('')
  const [modalRevision, setModalRevision] = useState('')
  const [modalEnv, setModalEnv] = useState<Environment>('dev')
  const [modalDeploy, setModalDeploy] = useState(true)
  const [modalError, setModalError] = useState('')
  // The commits on the chosen branch, so a build can be picked by its message instead of
  // by pasting hex. `null` while loading; an error is shown, not an empty list that reads
  // as "this branch has no commits".
  const [branchCommits, setBranchCommits] = useState<GitCommits | null>(null)
  useEffect(() => {
    if (!runModalPipeline || !modalBranch.trim()) return
    let active = true
    setBranchCommits(null)
    getModuleGitCommits(moduleId, modalBranch.trim())
      .then((result) => {
        if (active) {
          setBranchCommits(result)
          if (result && result.items && result.items.length > 0) {
            setModalRevision((prev) => (prev.trim() ? prev : result.items[0].sha))
          }
        }
      })
      .catch((cause) => { if (active) setBranchCommits({ moduleId, ref: modalBranch, items: [], error: cause instanceof Error ? cause.message : String(cause) }) })
    return () => { active = false }
  }, [moduleId, modalBranch, runModalPipeline])
  const branchSha = (name: string) => refs?.branches.find((b) => b.name === name)?.sha ?? refs?.tags.find((t) => t.name === name)?.sha ?? ''

  const openRunModal = (pipeline: PipelineDefinition) => {
    setRunModalPipeline(pipeline)
    const wanted = cleanBranchName(pipeline.branch)
    const branch = refs?.branches.some((b) => b.name === wanted) ? wanted : (refs?.branches[0]?.name ?? wanted)
    setModalBranch(branch)
    setModalRevision(branchSha(branch))
    setModalEnv((pipeline.id.replace('cd-', '') as Environment) || 'dev')
    setModalDeploy(true)
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
        deploy: modalDeploy,
      })
      setLiveRuns((current) => [next, ...current.filter((item) => item.id !== next.id)])
      setTriggered(runModalPipeline.name)
      const waiting = isWaitingForAdmission(next) ? ' Chờ tới lượt: quota đang đầy, chưa gửi tới Jenkins.' : ''
      notify((modalDeploy ? `Queued build ${rev.slice(0, 8)} → ${modalEnv}.` : `Queued build ${rev.slice(0, 8)} (build only, not deployed).`) + waiting)
      setRunModalPipeline(null)
      setRun({ pipeline: runModalPipeline, liveRun: next })
    } catch (error) {
      setModalError(error instanceof Error ? error.message : 'Unable to trigger pipeline.')
    } finally {
      setBusyPipeline(null)
    }
  }

  const runsForPipeline = (pipeline: PipelineDefinition) => liveRuns.filter((item) => item.environment === pipeline.id.replace('cd-', ''))
  // The superseding run may be newer than the list this tab loaded, so it is fetched when unknown.
  const openRunById = (runId: string) => {
    const open = (target: PipelineRun) => {
      const pipeline = definitions.find((d) => d.id === `cd-${target.environment}`) ?? definitions[0]
      if (pipeline) setRun({ pipeline, liveRun: target })
    }
    const known = liveRuns.find((item) => item.id === runId)
    if (known) { open(known); return }
    getPipelineRun(runId).then(open).catch((error) => notify(error instanceof Error ? error.message : 'Không tải được pipeline run.', 'error'))
  }
  if (run) {
    return (
      <PipelineRunView
        key={run.liveRun?.id ?? 'new'}
        pipeline={run.pipeline}
        liveRun={run.liveRun}
        moduleId={moduleId}
        onBack={() => { setRun(null); setHistoryPipeline(run.pipeline) }}
        onBackToEnvironments={() => { setRun(null); setHistoryPipeline(null) }}
        onRunUpdated={(updated) => {
          setRun({ pipeline: run.pipeline, liveRun: updated })
          setLiveRuns((current) => [updated, ...current.filter((item) => item.id !== updated.id)])
        }}
        onOpenRun={openRunById}
      />
    )
  }
  if (historyPipeline) { const historyRuns = runsForPipeline(historyPipeline); return <section className="history-view"><button className="back-button" onClick={() => setHistoryPipeline(null)}><ArrowLeft size={16} />All Environments</button><div className="run-heading"><div><h2>{historyPipeline.name} · Build History</h2><p>Runs targeting this environment, newest first; status reported by Jenkins and worker.</p></div><button className="primary-button" disabled={busyPipeline === historyPipeline.id} onClick={() => openRunModal(historyPipeline)}><Play size={15} />{busyPipeline === historyPipeline.id ? 'Queuing…' : 'Run Pipeline'}</button></div><section className="panel table-panel"><div className="data-table history-table"><div className="table-row table-head"><span>Build</span><span>Commit</span><span>Branch</span><span>Triggered By</span><span>Started</span><span>Status</span><span /></div>{historyRuns.map((item) => <button className="table-row table-button" onClick={() => setRun({ pipeline: historyPipeline, liveRun: item })} key={item.id}><span className="request-id" title={item.jenkinsRunId ?? item.id}>#{item.jenkinsRunId ? item.jenkinsRunId.split('#').pop() : item.id.slice(0, 8)}</span><span className="mono" title={item.commitSha}>{shortSha(item.commitSha)}</span><span title={item.trigger?.reason ?? ''}>{item.branch}<small className="run-intent"> · {triggerText(item)}{runIntent(item) !== 'deploys' ? ` · ${runIntent(item)}` : ''}</small></span><span title={item.startedBy ?? ''}>{person(item.startedBy)}</span><span title={item.createdAt}>{new Date(item.createdAt).toLocaleString('en-US')}</span><RunStatus run={item} /><ExternalLink size={15} /></button>)}</div>{!historyRuns.length && <div className="empty-table"><History size={22} /><strong>No pipeline runs for this environment yet</strong><span>Click "Run Pipeline" to build a commit and deploy.</span></div>}</section>{triggered && <div className="toast success-toast"><CheckCircle2 size={17} />{triggered} has been queued.</div>}</section> }
  return <>
    <section className="panel repo-strip"><div><span>Repository</span><strong className="mono">{refs?.repositoryUrl ?? '…'}</strong></div>{refs?.error ? <div className="repo-error"><ShieldAlert size={14} />Failed to read branches from repository: {refs.error}</div> : <div><span>Branch</span><strong>{refs ? refs.branches.map((b) => `${b.name} @ ${b.sha.slice(0, 7)}`).join(' · ') || 'no branches' : 'loading…'}</strong></div>}{refs && refs.tags.length > 0 && <div><span>Latest Tags</span><strong>{refs.tags.slice(0, 3).map((t) => t.name).join(' · ')}</strong></div>}</section>
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', margin: '14px 0 10px' }}>
      <div>
        <h3 style={{ margin: 0, fontSize: '15px', fontWeight: 600 }}>Cấu hình & Môi trường Pipeline</h3>
        <p style={{ margin: '3px 0 0', color: 'var(--muted)', fontSize: '12px' }}>Chỉnh sửa trực tiếp stages trong shared pipeline, tham số và trigger pipeline cho từng môi trường.</p>
      </div>
      <button
        className="secondary-button"
        style={{ display: 'flex', alignItems: 'center', gap: '6px' }}
        onClick={() => setShowStagesEditor(!showStagesEditor)}
      >
        <Sliders size={15} />
        {showStagesEditor ? 'Ẩn cấu hình Stages' : 'Chỉnh sửa Pipeline Stages'}
      </button>
    </div>
    {showStagesEditor && (
      <div style={{ marginBottom: '20px' }}>
        <PipelineStagesSettings moduleId={moduleId} />
      </div>
    )}
    <DeliveryFlowPanel moduleId={moduleId} />
    <div className="pipeline-card-grid">{definitions.map((pipeline) => {
    const live = runsForPipeline(pipeline)[0]
    const env = pipeline.id.replace('cd-', '')
    return <article className="pipeline-card panel" key={pipeline.id}><div className="pipeline-card-title"><span className={`pipeline-icon pipeline-${pipeline.id}`}><GitBranch size={18} /></span><div><h3>{pipeline.name}</h3><p>build → publish → deploy to <b>{env}</b> · branch {pipeline.branch}{env === 'prod' ? ' · approval required' : ''}</p></div><button aria-label={`Open history ${pipeline.name}`} onClick={() => setHistoryPipeline(pipeline)}><MoreHorizontal size={18} /></button></div><div className="last-build" style={live ? { cursor: 'pointer' } : undefined} title={live ? "Click để mở chi tiết Pipeline Run này" : undefined} onClick={() => { if (live) setRun({ pipeline, liveRun: live }) }}><span>Latest Run</span><strong title={live?.jenkinsRunId ?? live?.id}>{live ? `#${live.jenkinsRunId ? live.jenkinsRunId.split('#').pop() : live.id.slice(0, 8)}` : '—'}</strong>{live ? <RunStatus run={live} /> : <StatusPill status="idle" />}</div>{live?.status === 'waiting_approval' && <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', background: '#fff4df', border: '1px solid #dda11d', borderRadius: '6px', padding: '6px 10px', margin: '8px 0', fontSize: '0.82rem', color: '#9d6b0c', cursor: 'pointer' }} onClick={() => setRun({ pipeline, liveRun: live })}><span>⏳ <strong>Chờ phê duyệt</strong> để deploy Prod</span><span style={{ textDecoration: 'underline', fontWeight: 600 }}>Duyệt ngay →</span></div>}<dl><div><dt>Commit</dt><dd className="mono" title={live?.commitSha}>{shortSha(live?.commitSha)}{live?.branch ? ` (${live.branch})` : ''}</dd></div><div><dt>Artifact</dt><dd className="mono" title={live?.artifactDigest ?? ''}>{shortDigest(live?.artifactDigest)}</dd></div><div><dt>Triggered By</dt><dd title={live?.startedBy ?? ''}>{person(live?.startedBy)}</dd></div><div><dt>Started</dt><dd title={live?.createdAt}>{live ? timeAgo(live.createdAt) : 'none'}</dd></div></dl><footer><button className="secondary-button" onClick={() => setHistoryPipeline(pipeline)}><History size={15} />History</button><button className="primary-button" style={{ height: '32px', padding: '0 12px', fontSize: '0.82rem', display: 'flex', alignItems: 'center', gap: '6px' }} disabled={busyPipeline === pipeline.id} aria-label={`Run ${pipeline.name}`} onClick={() => openRunModal(pipeline)}><Play size={14} />Run Pipeline</button></footer></article>
  })}</div>
  {runModalPipeline && (
    <Modal
      title={`Run Pipeline → ${runModalPipeline.name}`}
      description={modalDeploy
        ? `Build selected commit on Jenkins (ephemeral pod), sign & scan artifact, then deploy to ${modalEnv}${modalEnv === 'prod' ? ' after approval' : ''}.`
        : 'Build, test, sign and publish the selected commit without deploying it. Promote the artifact afterwards from the run page.'}
      onClose={() => setRunModalPipeline(null)}
      footer={
        <>
          <button className="secondary-button" onClick={() => setRunModalPipeline(null)}>Cancel</button>
          <button className="primary-button" disabled={busyPipeline === runModalPipeline.id || !modalRevision.trim()} onClick={executeModalRun}>
            <Play size={15} />{busyPipeline === runModalPipeline.id ? 'Queuing…' : 'Run Pipeline'}
          </button>
        </>
      }
    >
      <div className="form-grid" style={{ gap: '14px' }}>
        <label className="field full">
          <span>Branch</span>
          {refs && refs.branches.length > 0 ? <select value={modalBranch} onChange={(e) => { setModalBranch(e.target.value); setModalRevision(branchSha(e.target.value)) }}>
            {refs.branches.map((b) => <option key={b.name} value={b.name}>{b.name} — {b.sha.slice(0, 7)}</option>)}
            {refs.tags.slice(0, 20).map((t) => <option key={`tag:${t.name}`} value={t.name}>tag {t.name} — {t.sha.slice(0, 7)}</option>)}
          </select> : <input value={modalBranch} onChange={(e) => setModalBranch(e.target.value)} placeholder="main" />}
          <small>{refs?.error
            ? `Unable to read repository (${refs.error}); enter commit manually.`
            : refsState === 'loading'
              ? 'Reading branches from the module\u2019s repository\u2026'
              : refsState === 'unreachable'
                ? 'Could not reach netCI to list branches; enter the commit SHA manually.'
                : 'Select branch or tag: netCI auto-fills head commit SHA.'}</small>
        </label>
        <label className="field full">
          <span>Commit</span>
          {branchCommits && branchCommits.items.length > 0 ? (
            <select data-testid="run-commit-picker" value={branchCommits.items.some((c) => c.sha === modalRevision) ? modalRevision : ''} onChange={(e) => { if (e.target.value) setModalRevision(e.target.value) }}>
              <option value="">— chọn commit —</option>
              {branchCommits.items.map((c) => <option key={c.sha} value={c.sha}>{c.sha.slice(0, 7)} · {c.subject} · {c.author} · {timeAgo(c.committedAt)}</option>)}
            </select>
          ) : (
            <small>{branchCommits === null
              ? 'Reading recent commits…'
              : branchCommits.error
                ? `Could not list commits (${branchCommits.error}); enter the SHA below.`
                : 'No commits found on this branch.'}</small>
          )}
        </label>
        <label className="field full">
          <span>Commit SHA</span>
          <input className="mono" value={modalRevision} onChange={(e) => setModalRevision(e.target.value)} placeholder="7–64 hex characters" />
          <small>Immutable commit recorded for this run and sent to Jenkins; can be overridden to build an earlier commit.</small>
        </label>
        <label className="field full">
          <span>Environment</span>
          <select value={modalEnv} onChange={(e) => setModalEnv(e.target.value as Environment)}>
            {definitions.map((d) => { const env = d.id.replace('cd-', '') as Environment; return <option key={env} value={env}>{environmentLabel[env] ?? env}{env === 'prod' ? ' (approval required)' : ''}</option> })}
          </select>
        </label>
        <label className="field full checkbox-field">
          <span><input type="checkbox" data-testid="run-build-only" checked={!modalDeploy} onChange={(e) => setModalDeploy(!e.target.checked)} /> Build only — do not deploy</span>
          <small>The artifact is still signed and published; deploy it later with “Promote”.</small>
        </label>
        {modalError && <div className="login-error" role="alert">{modalError}</div>}
      </div>
    </Modal>
  )}
  {triggered && <div className="toast success-toast"><CheckCircle2 size={17} />{triggered} was queued successfully.<button aria-label="Close notification" onClick={() => setTriggered(null)}>×</button></div>}</>

}

type VersionRow = { tag: string; date: string; user: string; commit: string; coverage: number | null; dev: string; staging: string; prod: string; autoTest: string; signed: boolean; sbom: string; scan: string; promotable: boolean }

function versionRow(item: ModuleVersion): VersionRow {
  return {
    tag: item.version,
    date: item.createdAt ? new Date(item.createdAt).toLocaleString('vi-VN') : 'Not reported',
    user: item.createdBy || 'unknown',
    commit: item.ciReport?.commit || item.artifactDigest || 'Not reported',
    coverage: typeof item.ciReport?.coveragePercentage === 'number' ? item.ciReport.coveragePercentage : typeof item.ciReport?.coverage === 'number' ? item.ciReport.coverage : null,
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
  const [builds, setBuilds] = useState<PipelineRun[]>([])
  const loadVersions = () => listModuleVersions(moduleId).then((result) => setVersionItems(result.items.map(versionRow))).catch((error) => notify(error instanceof Error ? error.message : 'Unable to load versions.', 'error'))
  useEffect(() => {
    void loadVersions()
    // A version is a name for an artifact a pipeline actually built: offer those builds
    // instead of asking for a run UUID and a 64-character digest by hand.
    listModulePipelineRuns(moduleId).then((result) => setBuilds(result.items.filter((r) => r.status === 'succeeded' && r.artifactDigest).sort((a, b) => b.createdAt.localeCompare(a.createdAt)))).catch(() => setBuilds([]))
  }, [moduleId])
  const chooseBuild = (runId: string) => {
    setPipelineRunId(runId)
    setArtifactDigest(builds.find((b) => b.id === runId)?.artifactDigest ?? '')
  }
  const example = ['{', '  "coverage": 87,', '  "autoTest": "passed",', '  "sast": "passed",', '  "sastIssues": 0,', '  "vulnerabilities": { "critical": 0, "high": 0, "medium": 2 },', '  "commit": "a1c4e2f"', '}'].join('\n')
  const saveVersion = async () => {
    if (!pipelineRunId.trim() || !/^sha256:[0-9a-f]{64}$/.test(artifactDigest)) {
      setFormError('A successful pipeline run ID and its sha256 artifact digest are required.')
      return
    }
    setSaving(true)
    setFormError('')
    try {
      await createModuleVersion(moduleId, { tag, gitTagUrl: gitTagUrl.trim() || undefined, artifactUrl: artifactUrl.trim() || undefined, pipelineRunId, artifactDigest })
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
    <section className="panel table-panel"><div className="data-table versions-table"><div className="table-row table-head"><span>Version</span><span>Released</span><span>By</span><span>Coverage</span><span>Dev</span><span>Staging</span><span>Production</span><span>Auto Test</span><span /></div>{versionItems.map((version) => <div className="table-row" key={version.tag}><span><strong>{version.tag}</strong><small className="mono" title={version.commit}>{version.commit.replace(/^sha256:/, '').slice(0, 12)}</small></span><span>{version.date}</span><span>{version.user}</span><span className="coverage-cell"><i><b style={{ width: `${version.coverage ?? 0}%` }} /></i>{version.coverage === null ? 'Not reported' : `${version.coverage}%`}</span><StatusPill status={version.dev.replace('_', ' ')} /><StatusPill status={version.staging.replace('_', ' ')} /><StatusPill status={version.prod.replace('_', ' ')} /><StatusPill status={version.autoTest} /><button className="row-more" aria-label={`Chi tiết ${version.tag}`} onClick={() => setSelectedTag(version.tag)}><MoreHorizontal size={17} /></button></div>)}</div>{!versionItems.length && <div className="empty-table"><strong>No release versions</strong><span>Register a verified artifact from a successful pipeline run.</span></div>}</section>
    {modal && <Modal title="Register verified version" description="Link a semantic version to the exact artifact and successful pipeline that produced it." onClose={() => setModal(false)} footer={<><button className="secondary-button" onClick={() => setModal(false)}>Cancel</button><button className="primary-button" disabled={!/^v?\d+\.\d+\.\d+/.test(tag) || (gitTagUrl.trim() !== '' && !/^https?:\/\//.test(gitTagUrl)) || (artifactUrl.trim() !== '' && !/^https?:\/\//.test(artifactUrl)) || !pipelineRunId.trim() || !/^sha256:[0-9a-f]{64}$/.test(artifactDigest) || saving} onClick={saveVersion}>{saving ? 'Creating…' : 'Register Version'}</button></>}><div className="form-grid"><label className="field full"><span>Version tag</span><input value={tag} onChange={(event) => setTag(event.target.value)} placeholder="v2.5.0" /><small>Semantic version (v1.2.3). Tên gọi cho một artifact đã build; không tạo ra build mới.</small></label><label className="field full"><span>Build thành công</span>{builds.length ? <select value={pipelineRunId} onChange={(event) => chooseBuild(event.target.value)}><option value="">— chọn build —</option>{builds.map((b) => <option key={b.id} value={b.id}>#{b.jenkinsRunId ? b.jenkinsRunId.split('#').pop() : b.id.slice(0, 8)} · {b.environment} · {b.commitSha.slice(0, 8)} · {(b.artifactDigest ?? '').replace('sha256:', '').slice(0, 12)} · {new Date(b.createdAt).toLocaleString('vi-VN')}</option>)}</select> : <input value={pipelineRunId} onChange={(event) => setPipelineRunId(event.target.value)} placeholder="UUID của lượt chạy thành công" />}<small>{builds.length ? 'Chỉ các lượt chạy thành công có artifact; netCI kiểm tra digest và bằng chứng của lượt chạy khi đăng ký.' : 'Chưa có build thành công nào; chạy pipeline trước.'}</small></label><label className="field full"><span>Artifact digest</span><input className="mono" value={artifactDigest} onChange={(event) => setArtifactDigest(event.target.value)} placeholder="sha256:…" readOnly={builds.length > 0} /></label><label className="field full"><span>Git tag link <em className="muted">(tuỳ chọn)</em></span><input value={gitTagUrl} onChange={(event) => setGitTagUrl(event.target.value)} placeholder="https://git…/tags/v2.5.0" /></label><label className="field full"><span>Artifact link <em className="muted">(tuỳ chọn)</em></span><input value={artifactUrl} onChange={(event) => setArtifactUrl(event.target.value)} placeholder="https://registry…" /></label>{formError && <div className="inline-error full" role="alert">{formError}</div>}</div><div className="api-contract"><div><Code2 size={17} /><strong>CI report integration</strong></div><p>Publish quality metrics after the CI pipeline completes:</p><code>POST /api/modules/{moduleId}/versions/{'{tag}'}/ci-report</code><small>Authorization: Bearer &lt;pipeline-api-key&gt;</small><pre>{example}</pre></div></Modal>}
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
  // Who the server says we are: the approve button is hidden from the author, the way
  // the server will refuse them, instead of inviting a click that ends in 403.
  const [me, setMe] = useState<string>('')
  useEffect(() => { whoami().then((identity) => setMe(identity.principal.subject)).catch(() => setMe('')) }, [])
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

  // Propose form state. The form edits a copy of the *active* revision, target by
  // target; it never starts from placeholders, and what it does not show (pipeline
  // settings, tasks) it carries over unchanged. Only the raw editor touches those.
  const [changeSummary, setChangeSummary] = useState('')
  const [envDrafts, setEnvDrafts] = useState<DeploymentEnvironmentConfig[]>([])
  const [dcimHosts, setDcimHosts] = useState<DcimServer[]>([])
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

  const checkDrift = async (announce = false) => {
    setLoadingDrift(true)
    try {
      const report = await detectDrift(moduleId)
      setDriftReport(report)
      // The banner below says it; a toast on every visit was noise.
      if (announce) notify(report.hasDrift ? 'Có sai lệch giữa revision đang hoạt động và trạng thái đang chạy / NetBox.' : 'Không có sai lệch cấu hình.', report.hasDrift ? 'error' : 'success')
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
      setEnvDrafts((active.deploymentConfig as unknown as DeploymentEnvironmentConfig[]).map((item) => ({ ...item, servers: [...(item.servers ?? [])], runtimeSettings: { ...(item.runtimeSettings ?? {}) } })))
      setPipelineJsonText(JSON.stringify(active.pipelineConfig, null, 2))
      setDeploymentJsonText(JSON.stringify(active.deploymentConfig, null, 2))
    }
    setChangeSummary('')
    setFormError('')
    setUseRawJson(false)
    setProposeModal(true)
    // Hosts NetBox knows for this module, offered as suggestions; anything else is
    // refused by the server at dispatch, so a typo is caught before a release.
    getModule(moduleId).then((m) => listDcimServers(m.systemId, moduleId)).then((page) => setDcimHosts(page.items)).catch(() => setDcimHosts([]))
  }
  const updateDraft = (index: number, patch: Partial<DeploymentEnvironmentConfig>) => setEnvDrafts((current) => current.map((item, i) => (i === index ? { ...item, ...patch } : item)))
  const updateDraftSettings = (index: number, patch: Partial<RuntimeSettings>) => setEnvDrafts((current) => current.map((item, i) => (i === index ? { ...item, runtimeSettings: { ...(item.runtimeSettings ?? {}), ...patch } } : item)))

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
        const active = revisions.find((r) => r.active) ?? revisions[0]
        pipelineConfig = active ? { ...active.pipelineConfig } : {}
        deploymentConfig = envDrafts.map((item) => {
          const settings = Object.fromEntries(Object.entries(item.runtimeSettings ?? {}).filter(([, v]) => v !== '' && v !== null && v !== undefined))
          return { ...item, servers: item.servers.map((h) => h.trim()).filter(Boolean), runtimeSettings: Object.keys(settings).length ? settings : null } as unknown as Record<string, unknown>
        })
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
                {me && me === pendingRev.createdBy
                  ? 'Bạn là tác giả của thay đổi này: một reviewer khác phải phê duyệt (separation of duties).'
                  : 'Separation of duties applies: the author of a production configuration cannot approve their own change.'}
              </small>
              <div style={{ display: 'flex', gap: 10 }}>
                {me !== pendingRev.createdBy && <button className="primary-button" onClick={() => handleApprove(pendingRev.id, pendingRev.revisionNumber)}>
                  <Check size={15} /> Approve & Activate
                </button>}
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
            Cấu hình triển khai theo revision bất biến (phiên bản {configVersion}); thay đổi prod cần một reviewer khác phê duyệt; mỗi máy chủ đích được đối chiếu với NetBox.
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
          <button className="secondary-button" disabled={loadingDrift} onClick={() => checkDrift(true)}>
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
              Không có deployment nào đã tới máy chủ lệch khỏi revision đang hoạt động, và DCIM chấp nhận mọi máy chủ đích đã cấu hình (trạng thái inventory, không phải kiểm tra "online").
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
                Created by <span title={activeRev.createdBy}>{person(activeRev.createdBy)}</span> on {new Date(activeRev.createdAt).toLocaleString('vi-VN')}
                {activeRev.approvedBy && <> · Approved by <span title={activeRev.approvedBy}>{person(activeRev.approvedBy)}</span></>}
              </small>
            </div>
          </div>
          <div className="env-grid" style={{ marginTop: 16, paddingTop: 16, borderTop: '1px solid var(--border)' }}>
            {(activeRev.deploymentConfig as unknown as DeploymentEnvironmentConfig[]).map((env) => <article className="env-card" key={env.environment} style={{ border: '1px solid var(--border)', borderRadius: 8 }}>
              <header><span className={`env-badge env-${env.environment}`}>{env.displayName || env.environment}</span><small className="muted">{env.runtime}</small></header>
              <dl>
                <div><dt>Máy chủ</dt><dd className="mono">{env.servers?.length ? env.servers.join(', ') : (env.runtime === 'kubernetes' ? `namespace ${env.namespace ?? '—'}` : '—')}</dd></div>
                {env.runtime === 'kubernetes' && <div><dt>Kubeconfig</dt><dd className="mono">{env.kubeconfigRef ?? '—'}</dd></div>}
                {Object.entries(env.runtimeSettings ?? {}).filter(([, v]) => v !== null && v !== undefined && v !== '').map(([k, v]) => <div key={k}><dt>{k}</dt><dd className="mono">{String(v)}</dd></div>)}
                {env.runtime !== 'kubernetes' && (env.runtimeSettings?.become === null || env.runtimeSettings?.become === undefined) && <div><dt>become</dt><dd className="mono" title="Không khai trong revision; playbook mặc định leo thang quyền (sudo)">mặc định (bật)</dd></div>}
              </dl>
            </article>)}
          </div>
          {Object.keys(activeRev.pipelineConfig ?? {}).length > 0 && <details style={{ marginTop: 12 }}><summary className="muted">Pipeline settings (JSON)</summary><pre style={{ margin: '8px 0 0 0', padding: 10, borderRadius: 6, background: 'var(--bg-card)', fontSize: '0.8rem' }}>{JSON.stringify(activeRev.pipelineConfig, null, 2)}</pre></details>}
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
              <span title={rev.createdBy}>{person(rev.createdBy)}</span>
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
                {!rev.active && rev.status === 'superseded' && (
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
                {envDrafts.map((env, index) => <fieldset className="field full env-draft" key={env.environment}>
                  <legend><span className={`env-badge env-${env.environment}`}>{env.displayName || env.environment}</span> <small className="muted">runtime {env.runtime}{env.environment === 'prod' ? ' · thay đổi prod cần một reviewer khác phê duyệt' : ''}</small></legend>
                  <div className="form-grid">
                    <label className="field full"><span>Máy chủ đích (tên trong NetBox, phân cách bằng dấu phẩy)</span>
                      <input list={`dcim-${env.environment}`} value={env.servers.join(', ')} onChange={(e) => updateDraft(index, { servers: e.target.value.split(',').map((h) => h.trim()) })} placeholder={env.runtime === 'kubernetes' ? '(Kubernetes: không cần máy chủ)' : 'ví dụ: netci-prod-01'} />
                      <datalist id={`dcim-${env.environment}`}>{dcimHosts.filter((h) => !h.environment || h.environment === env.environment).map((h) => <option key={h.id} value={h.hostname}>{h.ipAddress ? `${h.hostname} (${h.ipAddress})` : h.hostname}</option>)}</datalist>
                      <small>netCI kiểm tra tên máy chủ với NetBox (tenant/role/site) và trạng thái của nó trước mỗi lần triển khai.</small>
                    </label>
                    {env.runtime === 'kubernetes' && <>
                      <label className="field"><span>Namespace</span><input value={env.namespace ?? ''} onChange={(e) => updateDraft(index, { namespace: e.target.value })} /></label>
                      <label className="field"><span>Kubeconfig secret ref</span><input value={env.kubeconfigRef ?? ''} onChange={(e) => updateDraft(index, { kubeconfigRef: e.target.value })} /></label>
                    </>}
                    {env.runtime === 'docker' && <>
                      <label className="field"><span>Thư mục ứng dụng (appRoot)</span><input value={env.runtimeSettings?.appRoot ?? ''} onChange={(e) => updateDraftSettings(index, { appRoot: e.target.value })} /></label>
                      <label className="field"><span>Host port</span><input type="number" value={env.runtimeSettings?.hostPort ?? ''} onChange={(e) => updateDraftSettings(index, { hostPort: e.target.value === '' ? null : Number(e.target.value) })} /></label>
                      <label className="field"><span>Container port</span><input type="number" value={env.runtimeSettings?.containerPort ?? ''} onChange={(e) => updateDraftSettings(index, { containerPort: e.target.value === '' ? null : Number(e.target.value) })} /></label>
                      <label className="field"><span>Network mode</span><select value={env.runtimeSettings?.networkMode ?? 'bridge'} onChange={(e) => updateDraftSettings(index, { networkMode: e.target.value as 'bridge' | 'host' })}><option value="bridge">bridge</option><option value="host">host</option></select></label>
                      <label className="field"><span>Registry host nhìn từ máy chủ (imagePullHost)</span><input value={env.runtimeSettings?.imagePullHost ?? ''} onChange={(e) => updateDraftSettings(index, { imagePullHost: e.target.value })} placeholder="registry.example:5000" /></label>
                    </>}
                    {env.runtime === 'systemd' && <>
                      <label className="field"><span>Cổng ứng dụng (appPort)</span><input type="number" value={env.runtimeSettings?.appPort ?? ''} onChange={(e) => updateDraftSettings(index, { appPort: e.target.value === '' ? null : Number(e.target.value) })} /></label>
                      <label className="field"><span>Thư mục cài đặt (appRoot)</span><input value={env.runtimeSettings?.appRoot ?? ''} onChange={(e) => updateDraftSettings(index, { appRoot: e.target.value })} /></label>
                      <label className="field"><span>Phạm vi systemd</span><select value={env.runtimeSettings?.systemdScope ?? 'user'} onChange={(e) => updateDraftSettings(index, { systemdScope: e.target.value as 'system' | 'user' })}><option value="user">user</option><option value="system">system</option></select></label>
                    </>}
                    {env.runtime !== 'kubernetes' && <label className="field checkbox-field"><input type="checkbox" checked={env.runtimeSettings?.become ?? true} onChange={(e) => updateDraftSettings(index, { become: e.target.checked })} /><span>Leo thang quyền (become/sudo) trên máy chủ — playbook mặc định bật khi không khai</span></label>}
                  </div>
                </fieldset>)}
                {!envDrafts.length && <p className="muted full">Revision hiện tại không có môi trường nào; dùng Raw JSON.</p>}
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

type ModuleView = { id: string; name: string; type: string; description: string; runtime: Runtime; versions: string[]; activityCount: number; pipelineConfig: Partial<ModulePipelineConfig>; deploymentEnvironments?: DeploymentEnvironmentConfig[]; repositoryUrl?: string }

export function ModulePage({ moduleId, onSettings }: { moduleId: string; onSettings: () => void }) {
  const [module, setModule] = useState<ModuleView>(() => ({ id: moduleId, name: moduleId, type: 'Module', description: 'Loading module data from netCI.', runtime: 'docker', versions: [], activityCount: 0, pipelineConfig: {} }))
  const [tab, setTab] = useState<ModuleTab>('overview')
  // A refusal is a page, not a spinner that never ends: the server says whose module
  // this is and why the caller may not see it.
  const [loadError, setLoadError] = useState<{ status: number; message: string } | null>(null)
  useEffect(() => {
    setLoadError(null)
    getModule(moduleId).then((item) => setModule({ id: item.id, name: item.name, type: item.type, description: item.description, runtime: item.runtime, versions: item.versions, activityCount: item.pipelineRuns.length, pipelineConfig: item.pipelineConfig, repositoryUrl: item.repositoryUrl ?? undefined, deploymentEnvironments: item.deploymentEnvironments })).catch((error) => setLoadError({ status: error instanceof NetciApiError ? error.status : 0, message: error instanceof Error ? error.message : String(error) }))
  }, [moduleId])
  const moduleCode = module.id
  if (loadError) {
    return <section className="panel" style={{ padding: 24 }}>
      <h2 style={{ marginTop: 0 }}>{loadError.status === 403 ? 'Bạn không có quyền xem module này' : loadError.status === 404 ? 'Không tìm thấy module' : 'Không tải được module'}</h2>
      <p className="muted">{loadError.message}</p>
      <p className="muted" style={{ fontSize: '0.85rem' }}>Module <code>{moduleId}</code>. {loadError.status === 403 ? 'Quyền truy cập do team sở hữu module quyết định; hãy hỏi platform-admin hoặc team đó.' : ''}</p>
    </section>
  }
  return <>
    <div className="module-heading"><div className="module-title"><span className="module-icon purple"><Box size={20} /></span><div><div className="title-status"><h1>{module.name}</h1><span className="type-badge purple">{module.type}</span></div><p>{module.description}</p><small>Module: {moduleCode} · Runtime: {module.runtime}{module.repositoryUrl ? <> · <a href={module.repositoryUrl} target="_blank" rel="noreferrer">{module.repositoryUrl}</a></> : null}</small></div></div><button className="secondary-button" onClick={onSettings}><Settings size={16} />Settings</button></div>
    <nav className="tabs" role="tablist" aria-label="Module views">{([['overview', 'Overview'], ['pipeline', 'Pipeline'], ['version', 'Version'], ['config', 'Configuration'], ['dora', 'DORA Metrics']] as [ModuleTab, string][]).map(([id, label]) => <button role="tab" aria-selected={tab === id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)} key={id}>{label}</button>)}</nav>
    <div className="tab-content" role="tabpanel">{tab === 'overview' && <OverviewTab moduleId={moduleId} />}{tab === 'pipeline' && <PipelineTab moduleId={moduleId} pipelineConfig={module.pipelineConfig ?? {}} deploymentEnvironments={module.deploymentEnvironments} />}{tab === 'version' && <VersionsTab moduleId={moduleId} />}{tab === 'config' && <ConfigTab moduleId={moduleId} />}{tab === 'dora' && <DoraTab moduleId={moduleId} />}</div>
  </>
}
