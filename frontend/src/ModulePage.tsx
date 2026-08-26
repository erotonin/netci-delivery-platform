import { useState } from 'react'
import {
  ArrowLeft, Box, Check, CheckCircle2, Code2, Copy, ExternalLink, GitBranch,
  History, MoreHorizontal, Play, Plus, RotateCcw, Settings, TerminalSquare,
  ZoomIn, ZoomOut,
} from 'lucide-react'
import { DoraCards, Modal, StatusPill } from './PortalShell'
import { dora, modules, pipelineStages, pipelines, versions, type ModuleTab } from './portalData'

function OverviewTab() {
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

function PipelineRunView({ pipeline, onBack }: { pipeline: (typeof pipelines)[number]; onBack: () => void }) {
  const [stage, setStage] = useState('Checkout')
  const log = ['$ stage "' + stage + '"', '[netCI] Starting ' + stage.toLowerCase() + '...', '[netCI] workspace=/workspace/backend-api', '✓ Configuration loaded', '✓ Step completed successfully', '', 'Process exited with code 0'].join('\n')
  return <section className="run-view">
    <button className="back-button" onClick={onBack}><ArrowLeft size={16} />Back to build history</button>
    <div className="run-heading"><div><div className="title-status"><h2>{pipeline.name} {pipeline.number}</h2><StatusPill status="Success" /></div><p>{pipeline.branch} · {pipeline.sha} · triggered by {pipeline.actor}</p></div><div className="run-actions"><button className="secondary-button"><RotateCcw size={15} />Retry</button><button className="secondary-button"><ExternalLink size={15} />Open Jenkins</button></div></div>
    <section className="pipeline-canvas panel"><div className="canvas-toolbar"><span><strong>Pipeline graph</strong><small>13 stages · {pipeline.duration}</small></span><div><button aria-label="Thu nhỏ"><ZoomOut size={16} /></button><button aria-label="Phóng to"><ZoomIn size={16} /></button><button aria-label="Khôi phục"><RotateCcw size={15} /></button></div></div><div className="stage-graph">{pipelineStages.map(([name, category], index) => <button className={`stage-node category-${category} ${stage === name ? 'selected' : ''}`} onClick={() => setStage(name)} key={name}><span><Check size={13} /></span><strong>{name}</strong><small>{index < 2 ? '8s' : index < 8 ? '12s' : '19s'}</small></button>)}</div></section>
    <section className="log-panel"><div><span><TerminalSquare size={16} />{stage}</span><button aria-label="Sao chép log"><Copy size={15} /></button></div><pre>{log}</pre></section>
  </section>
}

function PipelineTab() {
  const [historyPipeline, setHistoryPipeline] = useState<(typeof pipelines)[number] | null>(null)
  const [run, setRun] = useState<(typeof pipelines)[number] | null>(null)
  const [triggered, setTriggered] = useState<string | null>(null)
  if (run) return <PipelineRunView pipeline={run} onBack={() => setRun(null)} />
  if (historyPipeline) return <section className="history-view"><button className="back-button" onClick={() => setHistoryPipeline(null)}><ArrowLeft size={16} />All pipelines</button><div className="run-heading"><div><h2>{historyPipeline.name} · Build history</h2><p>Recent pipeline runs from Jenkins.</p></div><button className="primary-button" onClick={() => setTriggered(historyPipeline.name)}><Play size={15} />Run pipeline</button></div><section className="panel table-panel"><div className="data-table history-table"><div className="table-row table-head"><span>Build</span><span>Commit</span><span>Branch</span><span>Triggered by</span><span>Duration</span><span>Status</span><span /></div>{[0, 1, 2, 3, 4].map((offset) => <button className="table-row table-button" onClick={() => setRun(historyPipeline)} key={offset}><span className="request-id">#{Number(historyPipeline.number.slice(1)) - offset}</span><span className="mono">{offset ? `b92e${offset}a1` : historyPipeline.sha}</span><span>{historyPipeline.branch}</span><span>{offset ? 'netCI' : historyPipeline.actor}</span><span>{historyPipeline.duration}</span><StatusPill status={offset === 3 ? 'Failed' : 'Success'} /><ExternalLink size={15} /></button>)}</div></section>{triggered && <div className="toast success-toast"><CheckCircle2 size={17} />{triggered} was queued successfully.</div>}</section>
  return <><div className="pipeline-card-grid">{pipelines.map((pipeline) => <article className="pipeline-card panel" key={pipeline.id}><div className="pipeline-card-title"><span className={`pipeline-icon pipeline-${pipeline.id}`}><GitBranch size={18} /></span><div><h3>{pipeline.name}</h3><p>Jenkins · {pipeline.branch}</p></div><button aria-label="Thêm tùy chọn"><MoreHorizontal size={18} /></button></div><div className="last-build"><span>Last build</span><strong>{pipeline.number}</strong><StatusPill status="Success" /></div><dl><div><dt>Commit</dt><dd className="mono">{pipeline.sha}</dd></div><div><dt>Triggered by</dt><dd>{pipeline.actor}</dd></div><div><dt>Started</dt><dd>{pipeline.time}</dd></div><div><dt>Duration</dt><dd>{pipeline.duration}</dd></div></dl><footer><button className="secondary-button" onClick={() => setHistoryPipeline(pipeline)}><History size={15} />History</button><button className="trigger-button" aria-label={`Run ${pipeline.name}`} onClick={() => setTriggered(pipeline.name)}><Play size={16} /></button></footer></article>)}</div>{triggered && <div className="toast success-toast"><CheckCircle2 size={17} />{triggered} was queued successfully.<button onClick={() => setTriggered(null)}>×</button></div>}</>
}

function VersionsTab() {
  const [modal, setModal] = useState(false)
  const [tag, setTag] = useState('')
  const example = ['{', '  "coverage": 87,', '  "autoTest": "passed",', '  "sast": "passed",', '  "sastIssues": 0,', '  "vulnerabilities": { "critical": 0, "high": 0, "medium": 2 },', '  "commit": "a1c4e2f"', '}'].join('\n')
  return <>
    <div className="tab-toolbar"><div><h2>Versions</h2><p>Register immutable release artifacts and receive CI quality reports.</p></div><button className="primary-button" onClick={() => setModal(true)}><Plus size={16} />New Version</button></div>
    <section className="panel table-panel"><div className="data-table versions-table"><div className="table-row table-head"><span>Version</span><span>Released</span><span>By</span><span>Coverage</span><span>Dev</span><span>Staging</span><span>Production</span><span>Auto Test</span><span /></div>{versions.map((version) => <div className="table-row" key={version.tag}><span><strong>{version.tag}</strong><small className="mono">{version.commit}</small></span><span>{version.date}</span><span>{version.user}</span><span className="coverage-cell"><i><b style={{ width: `${version.coverage}%` }} /></i>{version.coverage}%</span><StatusPill status={version.dev} /><StatusPill status={version.staging} /><StatusPill status={version.prod} /><StatusPill status={version.autoTest} /><button className="row-more" aria-label={`Tùy chọn ${version.tag}`}><MoreHorizontal size={17} /></button></div>)}</div></section>
    {modal && <Modal title="New Version" description="Register a version now; CI metrics can be published by the pipeline later." onClose={() => setModal(false)} footer={<><button className="secondary-button" onClick={() => setModal(false)}>Cancel</button><button className="primary-button" disabled={!/^v?\d+\.\d+\.\d+/.test(tag)} onClick={() => setModal(false)}>Create Version</button></>}><div className="form-grid"><label className="field full"><span>Version tag</span><input value={tag} onChange={(event) => setTag(event.target.value)} placeholder="v2.5.0" /></label><label className="field full"><span>Git tag link</span><input placeholder="https://git.example.net/.../tags/v2.5.0" /></label><label className="field full"><span>Artifact link</span><input placeholder="https://artifacts.example.net/.../v2.5.0" /></label></div><div className="api-contract"><div><Code2 size={17} /><strong>CI report integration</strong></div><p>Publish quality metrics after the CI pipeline completes:</p><code>POST /api/modules/backend-api/versions/{'{tag}'}/ci-report</code><small>Authorization: Bearer &lt;pipeline-api-key&gt;</small><pre>{example}</pre></div></Modal>}
  </>
}

function DoraTab() {
  const [range, setRange] = useState('Weekly')
  return <><div className="dora-toolbar"><div><h2>DORA Metrics</h2><p>Delivery performance for Backend API.</p></div><div><select value={range} onChange={(event) => setRange(event.target.value)}><option>Weekly</option><option>Monthly</option><option>Quarterly</option></select><input type="date" defaultValue="2025-04-01" /><span>to</span><input type="date" defaultValue="2025-04-28" /><button className="secondary-button"><RotateCcw size={15} />Reset</button></div></div><DoraCards /><div className="dora-charts">{dora.map((metric, index) => <section className="panel metric-chart" key={metric.key}><div className="panel-heading"><div><h2>{metric.label}</h2><p>{metric.hint}</p></div><strong>{metric.value}{metric.unit}</strong></div><div className="line-chart"><div className="line-grid"><i /><i /><i /><i /></div><svg viewBox="0 0 500 130" preserveAspectRatio="none" aria-label={`${metric.label} chart`}><polyline points={index % 2 ? '0,25 70,44 140,38 210,60 280,72 350,86 430,93 500,105' : '0,100 70,88 140,94 210,65 280,72 350,42 430,48 500,24'} fill="none" stroke={index === 2 ? '#f2053f' : index === 3 ? '#16a36a' : '#6366f1'} strokeWidth="3" /></svg><div className="chart-x"><span>01 Apr</span><span>08 Apr</span><span>15 Apr</span><span>22 Apr</span><span>28 Apr</span></div></div></section>)}</div></>
}

export function ModulePage({ moduleId, onSettings }: { moduleId: string; onSettings: () => void }) {
  const module = modules.find((item) => item.id === moduleId) ?? modules[0]
  const [tab, setTab] = useState<ModuleTab>('overview')
  return <>
    <div className="module-heading"><div className="module-title"><span className="module-icon purple"><Box size={20} /></span><div><div className="title-status"><h1>{module.name}</h1><span className="type-badge purple">{module.type}</span></div><p>{module.description}</p><small>Module code: NETCHAT_{module.id === 'backend-api' ? 'BE' : 'WEB'} · Runtime: {module.runtime}</small></div></div><button className="secondary-button" onClick={onSettings}><Settings size={16} />Settings</button></div>
    <nav className="tabs">{([['overview', 'Overview'], ['pipeline', 'Pipeline'], ['version', 'Version'], ['dora', 'DORA Metrics']] as [ModuleTab, string][]).map(([id, label]) => <button className={tab === id ? 'active' : ''} onClick={() => setTab(id)} key={id}>{label}</button>)}</nav>
    <div className="tab-content">{tab === 'overview' && <OverviewTab />}{tab === 'pipeline' && <PipelineTab />}{tab === 'version' && <VersionsTab />}{tab === 'dora' && <DoraTab />}</div>
  </>
}
