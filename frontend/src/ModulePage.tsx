import { useEffect, useState } from 'react'
import {
  ArrowLeft, Box, CheckCircle2, Code2, Copy, ExternalLink, GitBranch,
  History, MoreHorizontal, Play, Plus, RotateCcw, Settings, TerminalSquare,
  ZoomIn, ZoomOut,
} from 'lucide-react'
import { createModuleVersion, getDora, getModule, getModuleOverview, getPipelineLogs, listModulePipelineRuns, listModuleVersions, startModulePipeline, type Environment, type ModuleOverview, type ModulePipelineConfig, type ModuleVersion, type PipelineRun, type Runtime } from './api/netciClient'
import { usePortalFeedback } from './PortalFeedback'
import { DoraCards, Modal, StatusPill } from './PortalShell'
import type { DoraCardMetric, ModuleTab } from './portalTypes'

const pipelineStages = ['checkout', 'unit-test', 'build', 'sbom', 'vulnerability-scan', 'sign', 'publish', 'deploy', 'health-check']
const pipelines = [
  { id: 'ci', name: 'CI Pipeline', number: '', sha: '', actor: '', time: '', duration: '', status: 'not_started', branch: 'main' },
  { id: 'cd-dev', name: 'CD Dev', number: '', sha: '', actor: '', time: '', duration: '', status: 'not_started', branch: 'main' },
  { id: 'cd-staging', name: 'CD Staging', number: '', sha: '', actor: '', time: '', duration: '', status: 'not_started', branch: 'main' },
  { id: 'cd-prod', name: 'CD Prod', number: '', sha: '', actor: '', time: '', duration: '', status: 'not_started', branch: 'main' },
] as const
function EmptyModuleData({ title, description }: { title: string; description: string }) {
  return <section className="panel empty-tab-state"><Box size={29} /><h2>{title}</h2><p>{description}</p></section>
}

function OverviewTab({ moduleId }: { moduleId: string }) {
  const [overview, setOverview] = useState<ModuleOverview | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    getModuleOverview(moduleId).then((result) => { if (active) { setOverview(result); setError('') } }).catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : 'Unable to load module overview.') })
    return () => { active = false }
  }, [moduleId])
  if (error) return <EmptyModuleData title="Overview unavailable" description={error} />
  if (!overview) return <EmptyModuleData title="Loading delivery evidence" description="Reading deployments, releases and quality reports from netCI." />
  const hasData = overview.deployments.length > 0 || overview.recentReleases.length > 0 || Object.values(overview.trends).some((value) => value !== null)
  if (!hasData) return <EmptyModuleData title="No delivery activity yet" description="Deployments, releases and quality evidence will appear after real pipeline callbacks are recorded." />
  const trends = [
    ['Test coverage', overview.trends.testCoverage, '%'],
    ['Automation pass rate', overview.trends.automationPassRate, '%'],
    ['Security findings', overview.trends.securityFindings, ''],
  ] as const
  return <><div className="module-overview-grid"><section className="panel"><div className="panel-heading"><div><h2>Deployments</h2><p>Recorded deployment state by environment</p></div></div><div className="compact-list">{overview.deployments.map((item, index) => <div key={`${item.environment}-${index}`}><span>{item.environment}</span><StatusPill status={item.status.replace('_', ' ')} /></div>)}</div></section><section className="panel"><div className="panel-heading"><div><h2>Recent releases</h2><p>{overview.recentReleases.length} recorded</p></div></div><div className="compact-list">{overview.recentReleases.map((release) => <div key={release.version}><span><strong>{release.version}</strong><small>Test: {release.testStatus}</small></span><StatusPill status={release.status.replace('_', ' ')} /></div>)}</div></section></div><div className="quality-grid">{trends.map(([label, value, unit]) => <article className="panel quality-card" key={label}><div><span>{label}</span><strong>{value === null ? 'Not reported' : `${value}${unit}`}</strong><small>Latest CI quality report</small></div></article>)}</div></>
}

type PipelineDefinition = Omit<(typeof pipelines)[number], 'id' | 'branch'> & { id: string; branch: string; stages: string[] }

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

function PipelineRunView({ pipeline, liveRun, onBack, onRetry, retrying }: { pipeline: PipelineDefinition; liveRun: PipelineRun | null; onBack: () => void; onRetry: () => void; retrying: boolean }) {
  const { notify } = usePortalFeedback()
  const [stage, setStage] = useState(pipeline.stages[0] ?? 'Checkout')
  const [zoom, setZoom] = useState(1)
  const selectedStageLabel = stageLabels[stage] ?? stage
  const [log, setLog] = useState('No log lines have been reported for this run.')
  useEffect(() => {
    if (!liveRun) { setLog('No pipeline run selected.'); return }
    getPipelineLogs(liveRun.id)
      .then((result) => setLog(result.lines.length ? result.lines.join('\n') : 'No log lines have been reported for this run.'))
      .catch((error) => setLog(error instanceof Error ? error.message : 'Unable to load pipeline logs.'))
  }, [liveRun?.id])
  const copyLog = async () => {
    try {
      await navigator.clipboard.writeText(log)
      notify(`Đã sao chép log của stage ${selectedStageLabel}.`)
    } catch {
      notify('Trình duyệt không cho phép truy cập clipboard.', 'error')
    }
  }
  return <section className="run-view">
    <button className="back-button" onClick={onBack}><ArrowLeft size={16} />Back to build history</button>
    <div className="run-heading"><div><div className="title-status"><h2>{pipeline.name} {liveRun ? `#${liveRun.jenkinsRunId ?? liveRun.id.slice(0, 8)}` : ''}</h2><StatusPill status={liveRun?.status.replace('_', ' ') ?? 'Not started'} /></div><p>{liveRun ? `${liveRun.branch} · ${liveRun.commitSha} · triggered by ${liveRun.startedBy ?? 'unknown actor'}` : 'No pipeline run selected.'}</p></div><div className="run-actions"><button className="secondary-button" disabled={retrying || !liveRun} onClick={onRetry}><RotateCcw size={15} />{retrying ? 'Queuing…' : 'Retry'}</button><button className="secondary-button" disabled={!liveRun?.consoleUrl} title={liveRun?.consoleUrl ? 'Open Jenkins console' : 'Jenkins run URL is not part of the current API response'} onClick={() => { if (liveRun?.consoleUrl) window.open(liveRun.consoleUrl, '_blank', 'noopener,noreferrer') }}><ExternalLink size={15} />Open Jenkins</button></div></div>
    <section className="pipeline-canvas panel"><div className="canvas-toolbar"><span><strong>Configured pipeline stages</strong><small>{pipeline.stages.length} stages · per-stage results not reported · {Math.round(zoom * 100)}%</small></span><div><button aria-label="Thu nhỏ" disabled={zoom <= .75} onClick={() => setZoom((value) => Math.max(.75, value - .25))}><ZoomOut size={16} /></button><button aria-label="Phóng to" disabled={zoom >= 1.5} onClick={() => setZoom((value) => Math.min(1.5, value + .25))}><ZoomIn size={16} /></button><button aria-label="Khôi phục" disabled={zoom === 1} onClick={() => setZoom(1)}><RotateCcw size={15} /></button></div></div><div className="stage-graph" style={{ transform: `scale(${zoom})`, transformOrigin: 'left top' }}>{pipeline.stages.map((stageId, index) => { const name = stageLabels[stageId] ?? stageId; return <button className={`stage-node category-${stageCategory(stageId)} ${stage === stageId ? 'selected' : ''}`} onClick={() => setStage(stageId)} key={`${stageId}-${index}`}><span>{index + 1}</span><strong>{name}</strong><small>Status unavailable</small></button> })}</div></section>
    <section className="log-panel"><div><span><TerminalSquare size={16} />{selectedStageLabel}</span><button aria-label="Sao chép log" onClick={copyLog}><Copy size={15} /></button></div><pre>{log}</pre></section>
  </section>
}

function pipelineEnvironment(pipelineId: string): Environment {
  if (pipelineId === 'cd-staging') return 'staging'
  if (pipelineId === 'cd-prod') return 'prod'
  return 'dev'
}

const pipelineConfigKey: Record<string, string> = { ci: 'CI', 'cd-dev': 'CD Dev', 'cd-staging': 'CD Staging', 'cd-prod': 'CD Prod' }

function configuredPipelines(config: Partial<ModulePipelineConfig> = {}): PipelineDefinition[] {
  return pipelines.map((pipeline) => {
    const configured = config?.pipelines?.[pipelineConfigKey[pipeline.id]]
    return { ...pipeline, branch: configured?.branch || pipeline.branch, stages: configured?.stages?.length ? configured.stages : [...pipelineStages] }
  })
}

function PipelineTab({ moduleId, pipelineConfig }: { moduleId: string; pipelineConfig: Partial<ModulePipelineConfig> }) {
  const { notify } = usePortalFeedback()
  const definitions = configuredPipelines(pipelineConfig)
  const [historyPipeline, setHistoryPipeline] = useState<PipelineDefinition | null>(null)
  const [run, setRun] = useState<{ pipeline: PipelineDefinition; liveRun: PipelineRun | null } | null>(null)
  const [liveRuns, setLiveRuns] = useState<PipelineRun[]>([])
  const [busyPipeline, setBusyPipeline] = useState<string | null>(null)
  const [triggered, setTriggered] = useState<string | null>(null)
  const [sourceRevision, setSourceRevision] = useState('')
  useEffect(() => {
    let active = true
    listModulePipelineRuns(moduleId).then((result) => { if (active) setLiveRuns(result.items) }).catch((error) => { if (active) notify(error instanceof Error ? error.message : 'Không tải được pipeline history.', 'error') })
    return () => { active = false }
  }, [moduleId])
  const trigger = async (pipeline: PipelineDefinition, revision = sourceRevision) => {
    if (!/^[0-9a-f]{7,64}$/i.test(revision.trim())) {
      notify('Enter a valid 7–64 character Git commit SHA before starting a pipeline.', 'error')
      return
    }
    setBusyPipeline(pipeline.id)
    try {
      const next = await startModulePipeline(moduleId, { commitSha: revision.trim(), branch: pipeline.branch, environment: pipelineEnvironment(pipeline.id), parameters: { portalPipeline: pipeline.id } })
      setLiveRuns((current) => [next, ...current.filter((item) => item.id !== next.id)])
      setTriggered(pipeline.name)
      notify(`${pipeline.name} đã được đưa vào hàng đợi.`)
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Không thể trigger pipeline.', 'error')
    } finally {
      setBusyPipeline(null)
    }
  }
  const runsForPipeline = (pipeline: PipelineDefinition) => liveRuns.filter((item) => item.parameters?.portalPipeline === pipeline.id || (!item.parameters?.portalPipeline && pipeline.id === 'ci' && item.environment === 'dev'))
  if (run) return <PipelineRunView pipeline={run.pipeline} liveRun={run.liveRun} onBack={() => setRun(null)} onRetry={() => trigger(run.pipeline, run.liveRun?.commitSha ?? '')} retrying={busyPipeline === run.pipeline.id} />
  if (historyPipeline) { const historyRuns = runsForPipeline(historyPipeline); return <section className="history-view"><button className="back-button" onClick={() => setHistoryPipeline(null)}><ArrowLeft size={16} />All pipelines</button><div className="run-heading"><div><h2>{historyPipeline.name} · Build history</h2><p>Recent pipeline runs from netCI API and Jenkins callbacks.</p></div><button className="primary-button" disabled={busyPipeline === historyPipeline.id || !/^[0-9a-f]{7,64}$/i.test(sourceRevision.trim())} onClick={() => trigger(historyPipeline)}><Play size={15} />{busyPipeline === historyPipeline.id ? 'Queuing…' : 'Run pipeline'}</button></div><section className="panel table-panel"><div className="data-table history-table"><div className="table-row table-head"><span>Build</span><span>Commit</span><span>Branch</span><span>Triggered by</span><span>Started</span><span>Status</span><span /></div>{historyRuns.map((item) => <button className="table-row table-button" onClick={() => setRun({ pipeline: historyPipeline, liveRun: item })} key={item.id}><span className="request-id">#{item.jenkinsRunId ?? item.id.slice(0, 8)}</span><span className="mono">{item.commitSha}</span><span>{item.branch}</span><span>{item.startedBy ?? 'unknown'}</span><span>{new Date(item.createdAt).toLocaleString('vi-VN')}</span><StatusPill status={item.status.replace('_', ' ')} /><ExternalLink size={15} /></button>)}</div>{!historyRuns.length && <div className="empty-table"><History size={22} /><strong>No runs for this pipeline</strong><span>Enter a source commit and trigger the first API-backed run.</span></div>}</section>{triggered && <div className="toast success-toast"><CheckCircle2 size={17} />{triggered} was queued successfully.</div>}</section> }
  return <><section className="panel form-grid"><label className="field full"><span>Source Git commit SHA</span><input className="mono" value={sourceRevision} onChange={(event) => setSourceRevision(event.target.value)} placeholder="7–64 hexadecimal characters" /><small>netCI records and sends this exact immutable revision to the configured CI engine.</small></label></section><div className="pipeline-card-grid">{definitions.map((pipeline) => {
    const live = runsForPipeline(pipeline)[0]
    return <article className="pipeline-card panel" key={pipeline.id}><div className="pipeline-card-title"><span className={`pipeline-icon pipeline-${pipeline.id}`}><GitBranch size={18} /></span><div><h3>{pipeline.name}</h3><p>{pipelineConfig.runner ? `${pipelineConfig.runner} · ` : 'netCI API → configured CI adapter · '}{pipeline.branch}</p></div><button aria-label={`Mở lịch sử ${pipeline.name}`} onClick={() => setHistoryPipeline(pipeline)}><MoreHorizontal size={18} /></button></div><div className="last-build"><span>Last build</span><strong>{live ? `#${live.jenkinsRunId ?? live.id.slice(0, 8)}` : '—'}</strong><StatusPill status={live?.status.replace('_', ' ') ?? 'Not started'} /></div><dl><div><dt>Commit</dt><dd className="mono">{live?.commitSha ?? '—'}</dd></div><div><dt>Triggered by</dt><dd>{live?.startedBy ?? '—'}</dd></div><div><dt>Started</dt><dd>{live ? new Date(live.createdAt).toLocaleString('vi-VN') : 'No run yet'}</dd></div><div><dt>Environment</dt><dd>{pipelineEnvironment(pipeline.id)}</dd></div></dl><footer><button className="secondary-button" onClick={() => setHistoryPipeline(pipeline)}><History size={15} />History</button><button className="trigger-button" disabled={busyPipeline === pipeline.id || !/^[0-9a-f]{7,64}$/i.test(sourceRevision.trim())} title={/^[0-9a-f]{7,64}$/i.test(sourceRevision.trim()) ? undefined : 'Enter a valid source commit SHA'} aria-label={`Run ${pipeline.name}`} onClick={() => trigger(pipeline)}><Play size={16} /></button></footer></article>
  })}</div>{triggered && <div className="toast success-toast"><CheckCircle2 size={17} />{triggered} was queued successfully.<button aria-label="Đóng thông báo" onClick={() => setTriggered(null)}>×</button></div>}</>
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

type ModuleView = { id: string; name: string; type: string; description: string; runtime: Runtime; versions: string[]; activityCount: number; pipelineConfig: Partial<ModulePipelineConfig> }

export function ModulePage({ moduleId, onSettings }: { moduleId: string; onSettings: () => void }) {
  const [module, setModule] = useState<ModuleView>(() => ({ id: moduleId, name: moduleId, type: 'Module', description: 'Loading module data from netCI.', runtime: 'docker', versions: [], activityCount: 0, pipelineConfig: {} }))
  const [tab, setTab] = useState<ModuleTab>('overview')
  useEffect(() => {
    getModule(moduleId).then((item) => setModule({ id: item.id, name: item.name, type: item.type, description: item.description, runtime: item.runtime, versions: item.versions, activityCount: item.pipelineRuns.length, pipelineConfig: item.pipelineConfig })).catch(() => undefined)
  }, [moduleId])
  const moduleCode = module.id.toUpperCase().replace(/-/g, '_')
  return <>
    <div className="module-heading"><div className="module-title"><span className="module-icon purple"><Box size={20} /></span><div><div className="title-status"><h1>{module.name}</h1><span className="type-badge purple">{module.type}</span></div><p>{module.description}</p><small>Module code: {moduleCode} · Runtime: {module.runtime}</small></div></div><button className="secondary-button" onClick={onSettings}><Settings size={16} />Settings</button></div>
    <nav className="tabs" role="tablist" aria-label="Module views">{([['overview', 'Overview'], ['pipeline', 'Pipeline'], ['version', 'Version'], ['dora', 'DORA Metrics']] as [ModuleTab, string][]).map(([id, label]) => <button role="tab" aria-selected={tab === id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)} key={id}>{label}</button>)}</nav>
    <div className="tab-content" role="tabpanel">{tab === 'overview' && <OverviewTab moduleId={moduleId} />}{tab === 'pipeline' && <PipelineTab moduleId={moduleId} pipelineConfig={module.pipelineConfig ?? {}} />}{tab === 'version' && <VersionsTab moduleId={moduleId} />}{tab === 'dora' && <DoraTab moduleId={moduleId} />}</div>
  </>
}
