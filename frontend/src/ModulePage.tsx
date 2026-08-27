import { useEffect, useState } from 'react'
import {
  ArrowLeft, Box, Check, CheckCircle2, Code2, Copy, ExternalLink, GitBranch,
  History, MoreHorizontal, Play, Plus, RotateCcw, Settings, TerminalSquare,
  ZoomIn, ZoomOut,
} from 'lucide-react'
import { createModuleVersion, getDora, getModule, listModulePipelineRuns, startModulePipeline, type Environment, type ModulePipelineConfig, type PipelineRun, type Runtime } from './api/netciClient'
import { usePortalFeedback } from './PortalFeedback'
import { DoraCards, Modal, StatusPill } from './PortalShell'
import { dcimModules, moduleDora, modules, pipelineStages, pipelines, versions, type ModuleTab } from './portalData'

function EmptyModuleData({ title, description }: { title: string; description: string }) {
  return <section className="panel empty-tab-state"><Box size={29} /><h2>{title}</h2><p>{description}</p></section>
}

function OverviewTab({ hasActivity }: { hasActivity: boolean }) {
  if (!hasActivity) return <EmptyModuleData title="No delivery activity yet" description="The module is ready. Its CI checks, releases and quality metrics will appear after the first pipeline report." />
  return <>
    <div className="module-overview-grid">
      <section className="panel"><div className="panel-heading"><div><h2>CI · MERGE REQUESTS</h2><p>Latest pipeline checks</p></div><span className="all-passing"><i />All checks passing</span></div><div className="compact-list">{[['MR #239 · main', 'Running'], ['MR #241 · main', 'Success'], ['MR #240 · develop', 'Failed']].map(([label, status]) => <div key={label}><span><GitBranch size={15} />{label}</span><StatusPill status={status} /></div>)}</div><h3 className="subheading">CD · DEPLOYMENTS</h3><div className="compact-list"><div><span>Deploy Dev</span><StatusPill status="Deployed" /></div><div><span>Deploy Staging</span><StatusPill status="Deployed" /></div></div></section>
      <section className="panel"><div className="panel-heading"><div><h2>Recent Releases</h2><p>3 total</p></div></div><div className="release-list">{versions.map((version, index) => <div key={version.tag}><span className="release-avatar">{version.tag.slice(1, 3)}</span><span><strong>{version.tag}</strong><small>{version.date} · {version.user}</small></span><span className="release-tags"><StatusPill status={index === 0 ? 'Deployed' : index === 1 ? 'Pending' : 'Failed'} /><em className={version.autoTest === 'Passed' ? 'quality-pass' : 'quality-fail'}>Test · {version.autoTest}</em></span></div>)}</div></section>
    </div>
    <div className="quality-grid">
      <article className="panel quality-card"><div><span>Test coverage</span><strong>87%</strong><small>per released version</small><em>↗ +3% vs prev</em></div><div className="spark-bars blue">{[58, 71, 87].map((value) => <i style={{ height: `${value}%` }} key={value} />)}</div></article>
      <article className="panel quality-card"><div><span>Automation pass rate</span><strong>100%</strong><small>netAT test cases</small><em>↗ +14% vs prev</em></div><div className="spark-bars green">{[76, 86, 100].map((value) => <i style={{ height: `${value}%` }} key={value} />)}</div></article>
      <article className="panel quality-card"><div><span>Security findings</span><strong>10</strong><small>SAST + SCA by severity</small><em className="good-down">↘ -7 vs prev</em></div><div className="severity-bars"><i className="critical" /><i className="high" /><i className="medium" /><i className="low" /></div></article>
    </div>
  </>
}

type PipelineDefinition = (typeof pipelines)[number] & { stages: string[] }

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
  const log = ['$ stage "' + selectedStageLabel + '"', '[netCI] Starting ' + selectedStageLabel.toLowerCase() + '...', '[netCI] workspace=/workspace/backend-api', '✓ Configuration loaded', '✓ Step completed successfully', '', 'Process exited with code 0'].join('\n')
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
    <div className="run-heading"><div><div className="title-status"><h2>{pipeline.name} {liveRun ? `#${liveRun.jenkinsRunId ?? liveRun.id.slice(0, 8)}` : pipeline.number}</h2><StatusPill status={liveRun?.status.replace('_', ' ') ?? 'Success'} /></div><p>{liveRun?.branch ?? pipeline.branch} · {liveRun?.commitSha ?? pipeline.sha} · triggered by {liveRun ? 'Release Portal' : pipeline.actor}</p></div><div className="run-actions"><button className="secondary-button" disabled={retrying} onClick={onRetry}><RotateCcw size={15} />{retrying ? 'Queuing…' : 'Retry'}</button><button className="secondary-button" disabled title="Jenkins base URL is not configured for this Windows preview"><ExternalLink size={15} />Open Jenkins</button></div></div>
    <section className="pipeline-canvas panel"><div className="canvas-toolbar"><span><strong>Pipeline graph</strong><small>{pipeline.stages.length} stages · {pipeline.duration} · {Math.round(zoom * 100)}%</small></span><div><button aria-label="Thu nhỏ" disabled={zoom <= .75} onClick={() => setZoom((value) => Math.max(.75, value - .25))}><ZoomOut size={16} /></button><button aria-label="Phóng to" disabled={zoom >= 1.5} onClick={() => setZoom((value) => Math.min(1.5, value + .25))}><ZoomIn size={16} /></button><button aria-label="Khôi phục" disabled={zoom === 1} onClick={() => setZoom(1)}><RotateCcw size={15} /></button></div></div><div className="stage-graph" style={{ transform: `scale(${zoom})`, transformOrigin: 'left top' }}>{pipeline.stages.map((stageId, index) => { const name = stageLabels[stageId] ?? stageId; return <button className={`stage-node category-${stageCategory(stageId)} ${stage === stageId ? 'selected' : ''}`} onClick={() => setStage(stageId)} key={`${stageId}-${index}`}><span><Check size={13} /></span><strong>{name}</strong><small>{index < 2 ? '8s' : index < 8 ? '12s' : '19s'}</small></button> })}</div></section>
    <section className="log-panel"><div><span><TerminalSquare size={16} />{selectedStageLabel}</span><button aria-label="Sao chép log" onClick={copyLog}><Copy size={15} /></button></div><pre>{log}</pre></section>
  </section>
}

function pipelineEnvironment(pipelineId: string): Environment {
  if (pipelineId === 'cd-staging') return 'staging'
  if (pipelineId === 'cd-prod') return 'prod'
  return 'dev'
}

const pipelineConfigKey: Record<(typeof pipelines)[number]['id'], string> = { ci: 'CI', 'cd-dev': 'CD Dev', 'cd-staging': 'CD Staging', 'cd-prod': 'CD Prod', 'auto-test': 'Automation Test' }

function configuredPipelines(config: Partial<ModulePipelineConfig> = {}): PipelineDefinition[] {
  return pipelines.map((pipeline) => {
    const configured = config?.pipelines?.[pipelineConfigKey[pipeline.id]]
    return { ...pipeline, branch: configured?.branch || pipeline.branch, stages: configured?.stages?.length ? configured.stages : pipelineStages.map(([name]) => name) }
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
  useEffect(() => {
    let active = true
    listModulePipelineRuns(moduleId).then((result) => { if (active) setLiveRuns(result.items) }).catch((error) => { if (active) notify(error instanceof Error ? error.message : 'Không tải được pipeline history.', 'error') })
    return () => { active = false }
  }, [moduleId])
  const trigger = async (pipeline: PipelineDefinition) => {
    setBusyPipeline(pipeline.id)
    try {
      const next = await startModulePipeline(moduleId, { commitSha: pipeline.sha, branch: pipeline.branch, environment: pipelineEnvironment(pipeline.id), parameters: { portalPipeline: pipeline.id } })
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
  if (run) return <PipelineRunView pipeline={run.pipeline} liveRun={run.liveRun} onBack={() => setRun(null)} onRetry={() => trigger(run.pipeline)} retrying={busyPipeline === run.pipeline.id} />
  if (historyPipeline) { const historyRuns = runsForPipeline(historyPipeline); return <section className="history-view"><button className="back-button" onClick={() => setHistoryPipeline(null)}><ArrowLeft size={16} />All pipelines</button><div className="run-heading"><div><h2>{historyPipeline.name} · Build history</h2><p>Recent pipeline runs from netCI API and Jenkins callbacks.</p></div><button className="primary-button" disabled={busyPipeline === historyPipeline.id} onClick={() => trigger(historyPipeline)}><Play size={15} />{busyPipeline === historyPipeline.id ? 'Queuing…' : 'Run pipeline'}</button></div><section className="panel table-panel"><div className="data-table history-table"><div className="table-row table-head"><span>Build</span><span>Commit</span><span>Branch</span><span>Triggered by</span><span>Duration</span><span>Status</span><span /></div>{historyRuns.map((item) => <button className="table-row table-button" onClick={() => setRun({ pipeline: historyPipeline, liveRun: item })} key={item.id}><span className="request-id">#{item.jenkinsRunId ?? item.id.slice(0, 8)}</span><span className="mono">{item.commitSha}</span><span>{item.branch}</span><span>Release Portal</span><span>{new Date(item.createdAt).toLocaleString('vi-VN')}</span><StatusPill status={item.status.replace('_', ' ')} /><ExternalLink size={15} /></button>)}</div>{!historyRuns.length && <div className="empty-table"><History size={22} /><strong>No runs for this pipeline</strong><span>Trigger the pipeline to create the first API-backed run.</span></div>}</section>{triggered && <div className="toast success-toast"><CheckCircle2 size={17} />{triggered} was queued successfully.</div>}</section> }
  return <><div className="pipeline-card-grid">{definitions.map((pipeline) => {
    const live = runsForPipeline(pipeline)[0]
    return <article className="pipeline-card panel" key={pipeline.id}><div className="pipeline-card-title"><span className={`pipeline-icon pipeline-${pipeline.id}`}><GitBranch size={18} /></span><div><h3>{pipeline.name}</h3><p>netCI API → Jenkins · {pipeline.branch}</p></div><button aria-label={`Mở lịch sử ${pipeline.name}`} onClick={() => setHistoryPipeline(pipeline)}><MoreHorizontal size={18} /></button></div><div className="last-build"><span>Last build</span><strong>{live ? `#${live.jenkinsRunId ?? live.id.slice(0, 8)}` : '—'}</strong><StatusPill status={live?.status.replace('_', ' ') ?? 'Not started'} /></div><dl><div><dt>Commit</dt><dd className="mono">{live?.commitSha ?? pipeline.sha}</dd></div><div><dt>Triggered by</dt><dd>{live ? 'Release Portal' : '—'}</dd></div><div><dt>Started</dt><dd>{live ? new Date(live.createdAt).toLocaleString('vi-VN') : 'No run yet'}</dd></div><div><dt>Environment</dt><dd>{pipelineEnvironment(pipeline.id)}</dd></div></dl><footer><button className="secondary-button" onClick={() => setHistoryPipeline(pipeline)}><History size={15} />History</button><button className="trigger-button" disabled={busyPipeline === pipeline.id} aria-label={`Run ${pipeline.name}`} onClick={() => trigger(pipeline)}><Play size={16} /></button></footer></article>
  })}</div>{triggered && <div className="toast success-toast"><CheckCircle2 size={17} />{triggered} was queued successfully.<button aria-label="Đóng thông báo" onClick={() => setTriggered(null)}>×</button></div>}</>
}

function versionRows(moduleId: string, tags: string[]) {
  if (moduleId === 'backend-api') return versions
  return tags.map((tag) => ({ tag, date: 'Synced from delivery API', user: 'netCI', commit: 'Report unavailable', coverage: 0, dev: 'Not deployed', staging: 'Not deployed', prod: 'Not deployed', autoTest: 'Pending' }))
}

function VersionsTab({ moduleId, tags }: { moduleId: string; tags: string[] }) {
  const { notify } = usePortalFeedback()
  const [modal, setModal] = useState(false)
  const [tag, setTag] = useState('')
  const [gitTagUrl, setGitTagUrl] = useState('')
  const [artifactUrl, setArtifactUrl] = useState('')
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')
  const [selectedTag, setSelectedTag] = useState<string | null>(null)
  const [versionItems, setVersionItems] = useState(() => versionRows(moduleId, tags))
  useEffect(() => setVersionItems(versionRows(moduleId, tags)), [moduleId, tags.join('|')])
  const example = ['{', '  "coverage": 87,', '  "autoTest": "passed",', '  "sast": "passed",', '  "sastIssues": 0,', '  "vulnerabilities": { "critical": 0, "high": 0, "medium": 2 },', '  "commit": "a1c4e2f"', '}'].join('\n')
  const saveVersion = async () => {
    setSaving(true)
    setFormError('')
    try {
      await createModuleVersion(moduleId, { tag, gitTagUrl, artifactUrl })
      setVersionItems((current) => [{ tag, date: 'Vừa xong', user: 'Admin', commit: 'Waiting for CI', coverage: 0, dev: 'Not deployed', staging: 'Not deployed', prod: 'Not deployed', autoTest: 'Pending' }, ...current])
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
    <section className="panel table-panel"><div className="data-table versions-table"><div className="table-row table-head"><span>Version</span><span>Released</span><span>By</span><span>Coverage</span><span>Dev</span><span>Staging</span><span>Production</span><span>Auto Test</span><span /></div>{versionItems.map((version) => <div className="table-row" key={version.tag}><span><strong>{version.tag}</strong><small className="mono">{version.commit}</small></span><span>{version.date}</span><span>{version.user}</span><span className="coverage-cell"><i><b style={{ width: `${version.coverage}%` }} /></i>{version.coverage ? `${version.coverage}%` : 'Pending'}</span><StatusPill status={version.dev} /><StatusPill status={version.staging} /><StatusPill status={version.prod} /><StatusPill status={version.autoTest} /><button className="row-more" aria-label={`Chi tiết ${version.tag}`} onClick={() => setSelectedTag(version.tag)}><MoreHorizontal size={17} /></button></div>)}</div></section>
    {modal && <Modal title="New Version" description="Register a version now; CI metrics can be published by the pipeline later." onClose={() => setModal(false)} footer={<><button className="secondary-button" onClick={() => setModal(false)}>Cancel</button><button className="primary-button" disabled={!/^v?\d+\.\d+\.\d+/.test(tag) || !/^https?:\/\//.test(gitTagUrl) || !/^https?:\/\//.test(artifactUrl) || saving} onClick={saveVersion}>{saving ? 'Creating…' : 'Create Version'}</button></>}><div className="form-grid"><label className="field full"><span>Version tag</span><input value={tag} onChange={(event) => setTag(event.target.value)} placeholder="v2.5.0" /></label><label className="field full"><span>Git tag link</span><input value={gitTagUrl} onChange={(event) => setGitTagUrl(event.target.value)} placeholder="https://git.example.net/.../tags/v2.5.0" /></label><label className="field full"><span>Artifact link</span><input value={artifactUrl} onChange={(event) => setArtifactUrl(event.target.value)} placeholder="https://artifacts.example.net/.../v2.5.0" /></label>{formError && <div className="inline-error full" role="alert">{formError}</div>}</div><div className="api-contract"><div><Code2 size={17} /><strong>CI report integration</strong></div><p>Publish quality metrics after the CI pipeline completes:</p><code>POST /api/modules/{moduleId}/versions/{'{tag}'}/ci-report</code><small>Authorization: Bearer &lt;pipeline-api-key&gt;</small><pre>{example}</pre></div></Modal>}
    {selectedTag && (() => { const version = versionItems.find((item) => item.tag === selectedTag); return version ? <Modal title={version.tag} description="Immutable release version details." onClose={() => setSelectedTag(null)} footer={<button className="secondary-button" onClick={() => setSelectedTag(null)}>Close</button>}><div className="review-grid"><div><span>Commit / report</span><strong className="mono">{version.commit}</strong></div><div><span>Released</span><strong>{version.date}</strong></div><div><span>Coverage</span><strong>{version.coverage ? `${version.coverage}%` : 'Pending CI report'}</strong></div><div><span>Automation</span><strong>{version.autoTest}</strong></div></div></Modal> : null })()}
  </>
}

function DoraTab({ moduleId }: { moduleId: string }) {
  const { notify } = usePortalFeedback()
  const [range, setRange] = useState('Quarterly')
  const [from, setFrom] = useState('2024-07-01')
  const [to, setTo] = useState('2025-06-30')
  const [metrics, setMetrics] = useState<(typeof moduleDora)[number][]>(moduleDora)
  const [loading, setLoading] = useState(false)
  const [updatedAt, setUpdatedAt] = useState('preview baseline')

  const load = async () => {
    setLoading(true)
    try {
      const result = await getDora('modules', moduleId)
      const apiKeys: Record<string, string> = { frequency: 'deploymentFrequency', lead: 'leadTime', failure: 'changeFailureRate', recovery: 'timeToRestoreService' }
      const next = moduleDora.map((fallback) => {
        const metric = result.metrics.find((item) => item.key === fallback.key || item.key === apiKeys[fallback.key])
        return metric ? { ...fallback, label: metric.label, value: String(metric.value), unit: metric.unit, hint: metric.hint } : fallback
      })
      setMetrics(next)
      setUpdatedAt(new Date().toLocaleTimeString('vi-VN'))
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Không tải được DORA metrics.', 'error')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [moduleId])

  return <><div className="dora-toolbar"><div><p>4 DORA metrics · updated {updatedAt}</p></div><div><select value={range} onChange={(event) => setRange(event.target.value)}><option>Weekly</option><option>Monthly</option><option>Quarterly</option></select><span>From</span><input type="date" value={from} max={to} onChange={(event) => setFrom(event.target.value)} /><span>To</span><input type="date" value={to} min={from} onChange={(event) => setTo(event.target.value)} /><button className="secondary-button" disabled={loading} aria-label="Refresh DORA metrics" onClick={load}><RotateCcw size={15} />{loading ? 'Loading…' : 'Refresh'}</button></div></div><DoraCards metrics={metrics} /><div className="dora-charts">{metrics.map((metric, index) => <section className="panel metric-chart" key={metric.key}><div className="panel-heading"><div><h2>{metric.label}</h2><p>{metric.hint} · {range.toLowerCase()} view</p></div></div><div className="line-chart"><div className="line-grid"><i /><i /><i /><i /></div><svg viewBox="0 0 500 130" preserveAspectRatio="none" aria-label={`${metric.label} chart`}><polyline points={index % 2 ? '0,25 165,52 330,78 500,105' : '0,100 165,78 330,52 500,24'} fill="none" stroke={index === 2 ? '#f2053f' : index === 3 ? '#16a36a' : index === 1 ? '#2da9d6' : '#8b5cf6'} strokeWidth="3" /></svg><div className="chart-x"><span>{from}</span><span>{to}</span></div></div></section>)}</div></>
}

type ModuleView = { id: string; name: string; type: string; description: string; runtime: Runtime; versions: string[]; activityCount: number; pipelineConfig: Partial<ModulePipelineConfig> }

export function ModulePage({ moduleId, onSettings }: { moduleId: string; onSettings: () => void }) {
  const seed = modules.find((item) => item.id === moduleId)
  const [module, setModule] = useState<ModuleView>(() => ({ ...(seed ?? { id: moduleId, name: dcimModules.find((item) => item.id === moduleId)?.name ?? moduleId, type: dcimModules.find((item) => item.id === moduleId)?.type ?? 'Module', description: 'Module delivery configuration is loading.', runtime: 'docker' as const, versions: [] }), activityCount: seed ? pipelines.length : 0, pipelineConfig: {} }))
  const [tab, setTab] = useState<ModuleTab>('overview')
  useEffect(() => {
    const fallback = modules.find((item) => item.id === moduleId)
    if (fallback) setModule({ ...fallback, activityCount: pipelines.length, pipelineConfig: {} })
    getModule(moduleId).then((item) => setModule({ id: item.id, name: item.name, type: item.type, description: item.description, runtime: item.runtime, versions: item.versions, activityCount: item.pipelineRuns.length, pipelineConfig: item.pipelineConfig })).catch(() => undefined)
  }, [moduleId])
  const moduleCode = dcimModules.find((item) => item.id === module.id)?.code ?? module.id.toUpperCase().replace(/-/g, '_')
  const hasActivity = module.activityCount > 0
  return <>
    <div className="module-heading"><div className="module-title"><span className="module-icon purple"><Box size={20} /></span><div><div className="title-status"><h1>{module.name}</h1><span className="type-badge purple">{module.type}</span></div><p>{module.description}</p><small>Module code: {moduleCode} · Runtime: {module.runtime}</small></div></div><button className="secondary-button" onClick={onSettings}><Settings size={16} />Settings</button></div>
    <nav className="tabs" role="tablist" aria-label="Module views">{([['overview', 'Overview'], ['pipeline', 'Pipeline'], ['version', 'Version'], ['dora', 'DORA Metrics']] as [ModuleTab, string][]).map(([id, label]) => <button role="tab" aria-selected={tab === id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)} key={id}>{label}</button>)}</nav>
    <div className="tab-content" role="tabpanel">{tab === 'overview' && <OverviewTab hasActivity={hasActivity} />}{tab === 'pipeline' && <PipelineTab moduleId={moduleId} pipelineConfig={module.pipelineConfig ?? {}} />}{tab === 'version' && <VersionsTab moduleId={moduleId} tags={module.versions} />}{tab === 'dora' && <DoraTab moduleId={moduleId} />}</div>
  </>
}
