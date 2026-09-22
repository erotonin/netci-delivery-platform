import { useEffect, useState } from 'react'
import {
  ArrowLeft, ArrowRight, Box, Check, CheckCircle2, ChevronRight, Code2, Container,
  FileCode2, GitBranch, Globe2, HardDrive,
  Layers3, Plus, Server, Settings2, X, Zap,
} from 'lucide-react'
import { Modal } from './PortalShell'
import { usePortalFeedback } from './PortalFeedback'
import { getStageCatalog, listDcimModules, listDcimServers, autoProvisionDcimTargets, getGitInfo, listSampleApps, type DcimModule, type StageDefinition, type DeploymentEnvironmentConfig, type Environment, type GitInfo, type GitSample, type SampleApps, type ModulePipelineConfig, type ModulePipelineTabConfig, type Runtime, type RuntimeSettings } from './api/netciClient'


type DeploymentEnvironment = 'Dev' | 'Staging' | 'Production'
type DeploymentTarget = 'Systemd' | 'Docker' | 'Kubernetes'
type EnvironmentConfig = { name: string; environment: DeploymentEnvironment; target: DeploymentTarget; servers: string[]; kubeconfigRef?: string; namespace?: string; runtimeSettings?: RuntimeSettings }
type EnvironmentDraft = Pick<EnvironmentConfig, 'name' | 'environment' | 'target'>
type PortalInformation = { displayName: string; moduleType: string; description: string }
type ModuleWizardSubmission = PortalInformation & { runtime: Runtime; defaultEnvironment: Environment; deploymentEnvironments: DeploymentEnvironmentConfig[]; stages: string[]; pipelineConfig: ModulePipelineConfig; ownerTeam?: string }

const environmentNames: Record<DeploymentEnvironment, string> = {
  Dev: 'Development',
  Staging: 'Staging',
  Production: 'Production',
}

const deploymentTargets: { label: DeploymentTarget; icon: typeof Server; execution: string }[] = [
  { label: 'Systemd', icon: Server, execution: 'Executed via Ansible' },
  { label: 'Docker', icon: Container, execution: 'Executed via Ansible' },
  { label: 'Kubernetes', icon: Layers3, execution: 'Executed via kubeconfig' },
]

type TargetServer = { name: string; ip: string; environment: DeploymentEnvironment; status: string }

const defaultPipelineStages = ['checkout', 'unit-test', 'build', 'sbom', 'vulnerability-scan', 'sign', 'publish', 'deploy', 'health-check']
// One pipeline per environment: a netCI run builds the chosen commit and deploys it to
// that environment, so "CI" and "Automation Test" were not separate pipelines and the
// old `develop` / `tags/v*` defaults named branches most repositories do not have.
const defaultPipelineConfig: ModulePipelineConfig = {
  runner: 'docker-linux',
  strategy: 'Trunk-based',
  pipelines: Object.fromEntries(['CD Dev', 'CD Staging', 'CD Prod'].map((tab) => [tab, {
    branch: 'main',
    coverageReportPath: '',
    stages: [...defaultPipelineStages],
  }])),
}
const stageLabels: Record<string, string> = {
  checkout: 'Checkout',
  'unit-test': 'Unit test',
  build: 'Build',
  sbom: 'Generate SBOM',
  'vulnerability-scan': 'Vulnerability scan',
  sign: 'Sign artifact',
  publish: 'Publish artifact',
  deploy: 'Deploy',
  'health-check': 'Health check',
}

const kubernetesDefaults = (environment: DeploymentEnvironment) => ({
  kubeconfigRef: `netci-${environment.toLowerCase()}-kubeconfig`,
  namespace: environment.toLowerCase() === 'production' ? 'prod' : environment.toLowerCase(),
})

const runtimeForTarget: Record<DeploymentTarget, Runtime> = {
  Docker: 'docker',
  Kubernetes: 'kubernetes',
  Systemd: 'systemd',
}

const apiEnvironment: Record<DeploymentEnvironment, Environment> = {
  Dev: 'dev',
  Staging: 'staging',
  Production: 'prod',
}

function WizardSteps({ step }: { step: number }) {
  return <div className="wizard-steps">{[['1', 'General'], ['2', 'CI / CD'], ['3', 'Deployment']].map(([number, label], index) => <div className={step === index + 1 ? 'active' : step > index + 1 ? 'done' : ''} key={number}><span>{step > index + 1 ? <Check size={14} /> : number}</span><strong>{label}</strong>{index < 2 && <i />}</div>)}</div>
}

function GeneralStep({
  systemId,
  modules = [],
  integrationStatus = 'loading',
  selected,
  onSelect,
  information,
  onInformationChange,
  source,
  onSourceChange,
  customModule,
  onCustomModuleChange,
  gitInfo,
  sampleApps,
  onSelectSample,
}: {
  systemId: string
  modules?: DcimModule[]
  integrationStatus?: string
  selected: string
  onSelect: (id: string) => void
  information: PortalInformation
  onInformationChange: (value: PortalInformation) => void
  source: 'local' | 'dcim'
  onSourceChange: (s: 'local' | 'dcim') => void
  customModule: DcimModule
  onCustomModuleChange: (mod: DcimModule) => void
  gitInfo?: GitInfo | null
  sampleApps?: SampleApps | null
  onSelectSample?: (sample: GitSample) => void
}) {
  const module = source === 'local' ? customModule : modules.find((item) => item.id === selected)
  return <div className="wizard-content">
    <div className="wizard-section-title">
      <span><Layers3 size={18} /></span>
      <div>
        <h2>Select or define a module</h2>
        <p>Chọn phân hệ từ danh mục DCIM hoặc tạo cấu hình phân hệ Local trực tiếp.</p>
      </div>
    </div>
    <div className="segmented compact" style={{ marginBottom: '16px' }}>
      <button className={source === 'local' ? 'active' : ''} onClick={() => onSourceChange('local')}>Tạo Module Local (Tùy chọn runtime)</button>
      <button className={source === 'dcim' ? 'active' : ''} onClick={() => onSourceChange('dcim')}>Chọn từ DCIM ({modules.length})</button>
    </div>
    {source === 'dcim' ? (
      <>
        {integrationStatus === 'not_configured' && <div className="inline-error" role="status">DCIM chưa được cấu hình (NETCI_DCIM_BASE_URL). Bạn có thể chuyển sang tab "Tạo Module Local" để tiếp tục ngay.</div>}
        <div className="dcim-module-list">
          {modules.map((item) => <button disabled={item.registered} className={selected === item.id ? 'selected' : ''} onClick={() => onSelect(item.id)} key={item.id}>
            <span className="radio">{selected === item.id && <i />}</span>
            <span className="module-symbol"><Box size={17} /></span>
            <span><strong>{item.name}</strong><small>{item.id}{item.description ? ` · ${item.description}` : ''} · nguồn: {item.source ?? 'DCIM'}</small></span>
            {item.registered ? <em>Already added</em> : <ChevronRight size={17} />}
          </button>)}
        </div>
        {integrationStatus === 'ready' && !modules.length && <div className="inline-empty"><Box size={23} /><span>NetBox có tenant này nhưng chưa có device nào — nên chưa có device role để chọn làm module.</span></div>}
        {integrationStatus === 'not_registered' && <div className="inline-empty"><Box size={23} /><span>NetBox chưa có tenant "{systemId}". Tạo tenant và device (role = module) trong NetBox, hoặc dùng tab "Tạo Module Local".</span></div>}
        {integrationStatus === 'error' && <div className="inline-error" role="status">Không truy vấn được DCIM — kiểm tra NetBox. netCI không tự bịa danh sách module.</div>}
      </>
    ) : (
      <>
        {sampleApps && (
          <div className="panel" style={{ marginBottom: '16px', padding: '14px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '10px' }}>
              <div>
                <strong style={{ fontSize: '0.92rem' }}>Sample applications ({sampleApps.items.length} shipped in this checkout)</strong>
                <p style={{ margin: '2px 0 0', fontSize: '0.78rem', color: 'var(--text-muted)' }}>
                  {sampleApps.repositoryBaseConfigured
                    ? 'Select one to prefill runtime and pipeline template.'
                    : 'Repository base is not configured (NETCI_SAMPLE_APPS_REPOSITORY_BASE); selecting a sample prefills runtime only — enter the repository URL yourself.'}
                </p>
              </div>
            </div>
            {sampleApps.items.length === 0 ? (
              <div className="inline-empty"><Box size={23} /><span>No sample applications found in this checkout.</span></div>
            ) : (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '10px' }}>
              {sampleApps.items.map((sample) => {
                const isSelected = customModule.code === sample.id
                return (
                  <button
                    key={sample.id}
                    type="button"
                    onClick={() => onSelectSample?.(sample)}
                    style={{
                      textAlign: 'left',
                      padding: '10px 12px',
                      borderRadius: '8px',
                      border: isSelected ? '2px solid #3b82f6' : '1px solid var(--border-color, rgba(255,255,255,0.1))',
                      background: isSelected ? 'rgba(59, 130, 246, 0.12)' : 'var(--bg-subtle, rgba(255,255,255,0.02))',
                      cursor: 'pointer',
                      display: 'flex',
                      flexDirection: 'column',
                      gap: '4px',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontWeight: 600, color: isSelected ? '#60a5fa' : 'inherit' }}>
                      {sample.runtime === 'docker' ? <Container size={15} /> : sample.runtime === 'kubernetes' ? <Layers3 size={15} /> : <Server size={15} />}
                      <span style={{ fontSize: '0.85rem' }}>{sample.name}</span>
                    </div>
                    <span className="mono" style={{ color: 'var(--text-muted)', fontSize: '0.72rem' }}>{sample.path}</span>
                    <div style={{ marginTop: '4px', display: 'flex', gap: '6px', fontSize: '0.72rem', flexWrap: 'wrap' }}>
                      <span style={{ background: 'rgba(255,255,255,0.06)', padding: '1px 5px', borderRadius: '3px' }}>Runtime: <b>{sample.runtime}</b></span>
                      <span style={{ background: 'rgba(255,255,255,0.06)', padding: '1px 5px', borderRadius: '3px' }}>{sample.hasTests ? 'has tests' : 'no tests'}</span>
                      {!sample.repositoryUrl && <span style={{ background: 'rgba(245,158,11,0.15)', color: '#f59e0b', padding: '1px 5px', borderRadius: '3px' }}>repository not configured</span>}
                    </div>
                  </button>
                )
              })}
            </div>
            )}
          </div>
        )}
        <div className="panel wizard-form" style={{ marginBottom: '20px' }}>
        <div className="form-grid">
          <label className="field">
            <span>Module code / ID (chữ thường, gạch nối) *</span>
            <input
              value={customModule.code}
              onChange={(e) => {
                const code = e.target.value.toLowerCase().replace(/[^a-z0-9-_]/g, '-')
                onCustomModuleChange({ ...customModule, id: code, code })
                onSelect(code)
              }}
              placeholder="e.g. core-api, web-portal, auth-worker"
            />
          </label>
          <label className="field">
            <span>Display name *</span>
            <input
              value={information.displayName}
              onChange={(e) => {
                onInformationChange({ ...information, displayName: e.target.value })
                onCustomModuleChange({ ...customModule, name: e.target.value })
              }}
              placeholder="e.g. Core Banking API"
            />
          </label>
          <label className="field">
            <span>Type *</span>
            <select
              value={information.moduleType}
              onChange={(e) => {
                onInformationChange({ ...information, moduleType: e.target.value })
                onCustomModuleChange({ ...customModule, type: e.target.value })
              }}
            >
              <option>Backend</option>
              <option>Frontend</option>
              <option>Worker</option>
              <option>Gateway</option>
            </select>
          </label>
          <div className="field full">
            <span style={{ fontWeight: 600, display: 'block', marginBottom: '8px' }}>Repository URL *</span>
              <input
                value={customModule.repositoryUrl}
                onChange={(e) => onCustomModuleChange({ ...customModule, repositoryUrl: e.target.value })}
                placeholder="https://github.com/my-org/core-api hoặc file:///path/to/local/git"
              />

            <div style={{ marginTop: '10px', padding: '10px 14px', background: 'rgba(59, 130, 246, 0.08)', borderRadius: '8px', border: '1px solid rgba(59, 130, 246, 0.2)' }}>
              <div style={{ fontWeight: 600, color: '#60a5fa', fontSize: '0.84rem', marginBottom: '3px' }}>
                💡 Phân biệt giữa Mã Nguồn (Code) và Cấu hình Pipeline (netci.yaml):
              </div>
              <p style={{ margin: 0, fontSize: '0.78rem', color: '#cbd5e1', lineHeight: 1.5 }}>
                • <strong>Mã nguồn (Code)</strong>: Chứa logic nghiệp vụ (.py, .ts, .go, Dockerfile...). Liên kết Git URL mà Jenkins có thể checkout được.<br />
                • <strong>netci.yaml</strong>: Chỉ là file cấu hình các bước CI/CD (Test, Build, Scan, Deploy), được xem trước và tuỳ biến ở Bước 2.
              </p>
            </div>
          </div>
          <label className="field full">
            <span>Description</span>
            <textarea
              value={information.description}
              onChange={(e) => {
                onInformationChange({ ...information, description: e.target.value })
              }}
              placeholder="Mô tả chức năng nghiệp vụ của module..."
            />
          </label>
        </div>
      </div>
      </>
    )}
    {module && source === 'dcim' && <>
      <div className="wizard-section-title spaced"><span><Settings2 size={18} /></span><div><h2>Portal information</h2><p>Review display fields before continuing.</p></div></div>
      <div className="panel wizard-form">
        <div className="form-grid">
          <label className="field full"><span>Display name</span><input value={information.displayName} onChange={(event) => onInformationChange({ ...information, displayName: event.target.value })} /></label>
          <label className="field"><span>Module code (device role trong NetBox)</span><input value={module.id} readOnly /></label>
          <label className="field"><span>Type *</span><select value={information.moduleType} onChange={(event) => onInformationChange({ ...information, moduleType: event.target.value })}><option value="">— chọn —</option><option>Backend</option><option>Frontend</option><option>Worker</option><option>Gateway</option></select></label>
          <label className="field full"><span>Repository URL *</span><input value={customModule.repositoryUrl} onChange={(event) => onCustomModuleChange({ ...customModule, repositoryUrl: event.target.value })} placeholder="URL git mà Jenkins checkout được" /><small>NetBox chỉ biết máy chủ; nguồn mã của module là do bạn khai.</small></label>
          <label className="field full"><span>Description</span><textarea value={information.description} onChange={(event) => onInformationChange({ ...information, description: event.target.value })} /></label>
        </div>
      </div>
    </>}
    <div className="info-banner">
      <CheckCircle2 size={18} />
      <div>
        <strong>Bước tiếp theo: Thiết lập CI/CD & Runtime</strong>
        <p>Ở bước sau bạn sẽ cấu hình Pipeline runner và chọn Target triển khai (Kubernetes, Docker container, hoặc Linux Systemd).</p>
      </div>
    </div>
  </div>
}

function PipelineDesigner({ initialConfig, onCancel, onSave }: { initialConfig: ModulePipelineConfig; onCancel: () => void; onSave: (config: ModulePipelineConfig) => void }) {
  const [tab, setTab] = useState(Object.keys(initialConfig.pipelines)[0] ?? 'CD Dev')
  const [mode, setMode] = useState<'visual' | 'code'>('visual')
  const [config, setConfig] = useState<ModulePipelineConfig>(() => ({ ...initialConfig, pipelines: Object.fromEntries(Object.entries(initialConfig.pipelines).map(([key, value]) => [key, { ...value, stages: [...value.stages] }])) }))
  const [validated, setValidated] = useState(false)
  // The catalog is the source of what a pipeline may contain: names, which stages are
  // required, and the custom stages an administrator has approved.
  const [catalog, setCatalog] = useState<StageDefinition[]>([])
  const [picking, setPicking] = useState(false)
  useEffect(() => { getStageCatalog().then((c) => setCatalog(c.stages)).catch(() => setCatalog([])) }, [])
  const catalogById = Object.fromEntries(catalog.map((s) => [s.id, s]))
  const current = config.pipelines[tab]
  const updateCurrent = (changes: Partial<ModulePipelineTabConfig>) => setConfig({ ...config, pipelines: { ...config.pipelines, [tab]: { ...current, ...changes } } })
  const addable = catalog.filter((s) => s.kind === 'custom' && s.status === 'active' && !current.stages.includes(s.id) && current.stages.includes(s.afterStage ?? ''))
  const addCustom = (stage: StageDefinition) => {
    const anchorIndex = current.stages.indexOf(stage.afterStage ?? '')
    const next = [...current.stages]
    next.splice(anchorIndex + 1, 0, stage.id)
    updateCurrent({ stages: next })
    setPicking(false)
  }
  const yaml = ['pipeline:', `  name: ${tab.toLowerCase().replace(/\s+/g, '-')}`, `  runner: ${config.runner}`, `  strategy: ${config.strategy.toLowerCase()}`, '  branches:', `    - ${current.branch}`, ...(current.coverageReportPath ? [`  coverageReportPath: ${current.coverageReportPath}`] : []), '  stages:', ...current.stages.map((stage) => `    - ${stage}`)].join('\n')
  const valid = Boolean(config.runner.trim() && current.branch.trim() && current.stages.length)
  return <div className="pipeline-designer">
    <div className="designer-top">
      <button className="back-button" onClick={onCancel}><ArrowLeft size={16} />Back to CI / CD</button>
      <div className="segmented compact"><button className={mode === 'visual' ? 'active' : ''} onClick={() => setMode('visual')}>Visual</button><button className={mode === 'code' ? 'active' : ''} onClick={() => setMode('code')}>As code</button></div>
    </div>
    <div className="settings-pipeline-tabs">{Object.keys(config.pipelines).map((item) => <button className={tab === item ? 'active' : ''} onClick={() => { setTab(item); setValidated(false) }} key={item}>{item}</button>)}</div>
    {mode === 'visual' ? <>
      <div className="form-grid designer-form">
        <label className="field"><span>Runner routing label</span><input value={config.runner} onChange={(event) => setConfig({ ...config, runner: event.target.value })} placeholder="docker-linux" /><small>The configured CI engine resolves this label.</small></label>
        <label className="field"><span>Nhánh mặc định cho {tab}</span><input value={current.branch} onChange={(event) => updateCurrent({ branch: event.target.value })} placeholder="main" /><small>Nhánh được đề xuất khi bấm "Chạy"; commit thực tế được chọn lúc chạy.</small></label>
        <label className="field full"><span>Coverage report path <em className="muted">(tuỳ chọn)</em></span><input value={current.coverageReportPath ?? ''} onChange={(event) => updateCurrent({ coverageReportPath: event.target.value })} placeholder="coverage/lcov.info" /></label>
      </div>
      <div className="stage-list">
        <div className="section-heading"><h3>Các bước của pipeline (chung cho mọi môi trường)</h3><button className="secondary-button" disabled={!addable.length} title={addable.length ? 'Add an approved custom stage from the catalog' : 'No approved custom stage fits this pipeline (register one in Module Settings → Pipeline stages)'} onClick={() => setPicking(true)}><Plus size={15} />Add custom stage</button></div>
        {picking && addable.length > 0 && <div className="panel" style={{ padding: 8, marginBottom: 8, display: 'grid', gap: 6 }}>{addable.map((stage) => <button key={stage.id} className="secondary-button" onClick={() => addCustom(stage)}>{stage.name} <code className="mono">{stage.script}</code> · after {stage.afterStage}</button>)}</div>}
        {current.stages.map((stage, index) => {
          const def = catalogById[stage]
          const label = def?.name ?? stageLabels[stage] ?? stage
          const required = Boolean(def?.required)
          return <div key={`${stage}-${index}`}><span className="drag-handle">⠿</span><span className="stage-number">{index + 1}</span><strong>{label}</strong><small>{def?.kind === 'custom' ? `custom · ${def.script}` : required ? 'required by policy' : 'Catalog stage'}</small><button aria-label={`Configure ${label}`} disabled title="Parameters are set per module in Module Settings → Pipeline stages"><Settings2 size={15} /></button><button aria-label={`Remove ${label}`} disabled={required} title={required ? 'Required: an artifact without this stage cannot be deployed' : undefined} onClick={() => updateCurrent({ stages: current.stages.filter((_, itemIndex) => itemIndex !== index && catalogById[current.stages[itemIndex]]?.afterStage !== stage) })}><X size={15} /></button></div>
        })}
      </div>
    </> : <div className="code-editor"><div><span>pipeline.yml · read-only projection</span><button onClick={() => setValidated(valid)}><Code2 size={15} />Validate</button>{validated && <small role="status">Required catalog fields are present</small>}</div><pre>{yaml}</pre></div>}
    <div className="designer-footer"><button className="primary-button" disabled={!valid} onClick={() => onSave({ ...config, pipelines: Object.fromEntries(Object.entries(config.pipelines).map(([name, item]) => [name, { ...item, stages: [...current.stages] }])) })}><Check size={16} />Save configuration</button></div>
  </div>
}

function CicdStep({ config, onConfigChange }: { config: ModulePipelineConfig; onConfigChange: (config: ModulePipelineConfig) => void }) {
  const [designer, setDesigner] = useState(false)
  if (designer) return <PipelineDesigner initialConfig={config} onCancel={() => setDesigner(false)} onSave={(nextConfig) => { onConfigChange(nextConfig); setDesigner(false) }} />
  return <div className="wizard-content">
    <div className="wizard-section-title"><span><Server size={18} /></span><div><h2>Runner routing label</h2><p>This label is sent to the configured CI adapter. Availability is verified by the CI engine when the run starts.</p></div></div>
    <label className="field runner-label"><span>Runner label</span><input value={config.runner} onChange={(event) => onConfigChange({ ...config, runner: event.target.value })} placeholder="docker-linux" /></label>
    <div className="wizard-section-title spaced"><div><h2>Pipelines theo môi trường</h2><p>Mỗi môi trường một pipeline: build commit đã chọn → ký & quét → triển khai. Các bước là chung; nhánh mặc định có thể khác nhau.</p></div><button className="secondary-button" onClick={() => setDesigner(true)}><Settings2 size={15} />Open pipeline designer</button></div>
    <div className="pipeline-card-grid">{Object.entries(config.pipelines).map(([name, item]) => <article className="panel pipeline-card" key={name}><div className="pipeline-card-header"><div><h3>{name}</h3><small>Branch: {item.branch}</small></div><span className="stage-count">{item.stages.length} stages</span></div><ol className="pipeline-stage-pills">{item.stages.map((stage) => <li key={stage}>{stageLabels[stage] ?? stage}</li>)}</ol></article>)}</div>
  </div>
}

function TargetConfiguration({ current, targetServers = [], updateCurrent, onSelectServers }: { current: EnvironmentConfig; targetServers?: TargetServer[]; updateCurrent: (changes: Partial<EnvironmentConfig>) => void; onSelectServers: () => void }) {
  if (current.target === 'Kubernetes') {
    return <section className="deployment-section panel"><div className="section-heading"><div><h3>Cluster access</h3><p>Reference stored cluster credentials; never paste kubeconfig contents into the module.</p></div></div><div className="form-grid target-connection-form"><label className="field"><span>Kubeconfig secret reference</span><input value={current.kubeconfigRef ?? ''} onChange={(event) => updateCurrent({ kubeconfigRef: event.target.value })} placeholder="netci-staging-kubeconfig" /><small>Secret reference resolved by the runtime adapter.</small></label><label className="field"><span>Target namespace</span><input value={current.namespace ?? ''} onChange={(event) => updateCurrent({ namespace: event.target.value })} placeholder="staging" /><small>The namespace must already exist and be authorized.</small></label></div></section>
  }
  const rs = current.runtimeSettings ?? {}
  const setRs = (changes: RuntimeSettings) => updateCurrent({ runtimeSettings: { ...rs, ...changes } })
  const num = (value: string) => (value.trim() === '' ? null : Number(value))
  const layout = current.target === 'Docker'
    ? <section className="deployment-section panel"><div className="section-heading"><div><h3>Server Layout & Runtime Settings (Docker)</h3><p>Playbook `deploy-docker.yml` uses these parameters from the approved configuration; leave blank for playbook defaults.</p></div></div><div className="form-grid">
        <label className="field"><span>App Root Directory (appRoot)</span><input value={rs.appRoot ?? ''} placeholder="/opt/netci-docker-demo" onChange={(e) => setRs({ appRoot: e.target.value || null })} /><small>When become is disabled, this path must be writable by the runner user.</small></label>
        <label className="field"><span>Host Port (hostPort)</span><input type="number" value={rs.hostPort ?? ''} placeholder="18081" onChange={(e) => setRs({ hostPort: num(e.target.value) })} /></label>
        <label className="field"><span>Network Mode</span><select value={rs.networkMode ?? ''} onChange={(e) => setRs({ networkMode: (e.target.value || null) as RuntimeSettings['networkMode'] })}><option value="">bridge (default)</option><option value="bridge">bridge</option><option value="host">host</option></select></label>
        <label className="field"><span>Container Port (containerPort)</span><input type="number" value={rs.containerPort ?? ''} placeholder="8080" onChange={(e) => setRs({ containerPort: num(e.target.value) })} /></label>
        <label className="field"><span>Registry Host (imagePullHost)</span><input value={rs.imagePullHost ?? ''} placeholder="e.g. 172.17.0.1:55000" onChange={(e) => setRs({ imagePullHost: e.target.value || null })} /><small>Overrides registry host name; image digest remains immutable.</small></label>
        <label className="field checkbox-field"><input type="checkbox" checked={rs.become ?? true} onChange={(e) => setRs({ become: e.target.checked })} /><span>Ansible become (sudo) on target server — enabled by default</span></label>
      </div></section>
    : <section className="deployment-section panel"><div className="section-heading"><div><h3>Server Layout & Runtime Settings (systemd)</h3><p>Playbook `deploy-systemd.yml` uses these parameters; leave blank for playbook defaults.</p></div></div><div className="form-grid">
        <label className="field"><span>App Root Directory (appRoot)</span><input value={rs.appRoot ?? ''} placeholder="/opt/netci-demo" onChange={(e) => setRs({ appRoot: e.target.value || null })} /></label>
        <label className="field"><span>Service Port (appPort)</span><input type="number" value={rs.appPort ?? ''} placeholder="8080" onChange={(e) => setRs({ appPort: num(e.target.value) })} /></label>
        <label className="field"><span>Systemd Unit Scope</span><select value={rs.systemdScope ?? ''} onChange={(e) => setRs({ systemdScope: (e.target.value || null) as RuntimeSettings['systemdScope'] })}><option value="">system (default)</option><option value="system">system</option><option value="user">user</option></select></label>
        <label className="field checkbox-field"><input type="checkbox" checked={rs.become ?? true} onChange={(e) => setRs({ become: e.target.checked })} /><span>Ansible become (sudo) on target server — enabled by default</span></label>
      </div></section>
  return <>{layout}<section className="deployment-section panel"><div className="section-heading"><div><h3>Target Servers</h3><p>Deployment servers for runtime {current.target}.</p></div><button className="secondary-button" onClick={onSelectServers}><Plus size={15} />Select servers</button></div>{current.servers.length ? <div className="target-server-list">{current.servers.map((server) => { const target = targetServers.find((item) => item.name === server); return <div key={server}><HardDrive size={17} /><span><strong>{server}</strong><small>{target ? `${target.ip || 'No IP in NetBox'} · NetBox: ${target.status}` : 'Not registered in NetBox — DCIM validation'}</small></span><span className={`status status-${target ? (target.status === 'active' ? 'online' : 'degraded') : 'offline'}`}><i />{target ? target.status : 'custom'}</span><button aria-label={`Remove ${server}`} onClick={() => updateCurrent({ servers: current.servers.filter((item) => item !== server) })}><X size={15} /></button></div> })}</div> : <div className="inline-empty"><Server size={23} /><span>No target servers selected</span></div>}</section></>

}

function DeploymentStep({ systemId, moduleId, initialTarget = 'Docker', targetServers = [], onValidityChange, onConfigurationChange }: { systemId: string; moduleId: string; initialTarget?: DeploymentTarget; targetServers?: TargetServer[]; onValidityChange: (valid: boolean) => void; onConfigurationChange: (config: DeploymentEnvironmentConfig[]) => void }) {
  const [liveTargetServers, setLiveTargetServers] = useState<TargetServer[]>(targetServers)
  const [inventoryStatus, setInventoryStatus] = useState('loading')
  const [provisioning, setProvisioning] = useState(false)
  const { notify } = usePortalFeedback()

  const handleAutoProvision = async () => {
    setProvisioning(true)
    try {
      const res = await autoProvisionDcimTargets(systemId, moduleId, initialTarget.toLowerCase())
      notify(`Đã auto-provision thành công ${res.provisioned} NetBox targets cho tenant "${systemId}"!`)
      const result = await listDcimServers(systemId, moduleId)
      setInventoryStatus(result.status)
      const mapped: TargetServer[] = result.items.map((item) => ({
        name: item.hostname || item.id,
        ip: item.ipAddress ?? '',
        environment: (item.environment === 'prod' ? 'Production' : item.environment === 'staging' ? 'Staging' : 'Dev') as DeploymentEnvironment,
        status: item.status ?? 'unknown',
      }))
      setLiveTargetServers(mapped)
      setEnvironments((prev) => prev.map((env) => {
        if (env.servers.length > 0 || env.target === 'Kubernetes') return env
        const candidate = mapped.find((s) => s.environment === env.environment)
        return candidate ? { ...env, servers: [candidate.name] } : env
      }))
    } catch (err) {
      notify(err instanceof Error ? err.message : 'Không thể auto-provision NetBox targets', 'error')
    } finally {
      setProvisioning(false)
    }
  }

  useEffect(() => {
    listDcimServers(systemId, moduleId).then((result) => {
      setInventoryStatus(result.status)
      const mapped: TargetServer[] = result.items.map((item) => ({
        name: item.hostname || item.id,
        ip: item.ipAddress ?? '',
        environment: (item.environment === 'prod' ? 'Production' : item.environment === 'staging' ? 'Staging' : 'Dev') as DeploymentEnvironment,
        status: item.status ?? 'unknown',
      }))
      // Only what NetBox actually holds. A made-up "localhost · online" here would be
      // refused by DCIM at deploy time anyway, after the user believed it was set up.
      setLiveTargetServers(mapped)
    }).catch(() => {
      setInventoryStatus('error')
      setLiveTargetServers([])
    })
  }, [systemId, moduleId])
  targetServers = liveTargetServers.length > 0 ? liveTargetServers : targetServers
  const [environments, setEnvironments] = useState<EnvironmentConfig[]>(() => [

    {
      name: 'Development',
      environment: 'Dev',
      target: initialTarget,
      servers: [],
      ...(initialTarget === 'Kubernetes' ? kubernetesDefaults('Dev') : {})
    },
    {
      name: 'Staging',
      environment: 'Staging',
      target: initialTarget,
      servers: [],
      ...(initialTarget === 'Kubernetes' ? kubernetesDefaults('Staging') : {})
    },
    {
      name: 'Production',
      environment: 'Production',
      target: initialTarget,
      servers: [],
      ...(initialTarget === 'Kubernetes' ? kubernetesDefaults('Production') : {})
    }
  ])
  const [active, setActive] = useState(0)

  useEffect(() => {
    setEnvironments((prev) => {
      if (!prev.length) {
        return [
          {
            name: 'Development',
            environment: 'Dev',
            target: initialTarget,
            servers: [],
            ...(initialTarget === 'Kubernetes' ? kubernetesDefaults('Dev') : {})
          },
          {
            name: 'Staging',
            environment: 'Staging',
            target: initialTarget,
            servers: [],
            ...(initialTarget === 'Kubernetes' ? kubernetesDefaults('Staging') : {})
          },
          {
            name: 'Production',
            environment: 'Production',
            target: initialTarget,
            servers: [],
            ...(initialTarget === 'Kubernetes' ? kubernetesDefaults('Production') : {})
          }
        ]
      }
      return prev.map((env) => ({
        ...env,
        target: initialTarget,
        servers: initialTarget === 'Kubernetes' ? [] : env.servers,
        ...(initialTarget === 'Kubernetes' ? kubernetesDefaults(env.environment) : { kubeconfigRef: undefined, namespace: undefined })
      }))
    })
  }, [initialTarget])
  const [addEnv, setAddEnv] = useState(false)
  const [editingEnvironment, setEditingEnvironment] = useState<number | null>(null)
  const [environmentDraft, setEnvironmentDraft] = useState<EnvironmentDraft>({ name: 'Staging', environment: 'Staging', target: 'Docker' })
  const [selectServers, setSelectServers] = useState(false)
  const [serverDraft, setServerDraft] = useState<string[]>([])
  const current = environments[active]
  const availableEnvironment = (['Dev', 'Staging', 'Production'] as DeploymentEnvironment[]).find((item) => !environments.some((environment) => environment.environment === item))
  const duplicateEnvironment = environments.some((environment, index) => environment.environment === environmentDraft.environment && index !== editingEnvironment)
  const lockedTarget = environments.find((_, index) => index !== editingEnvironment)?.target
  const environmentsReady = environments.length > 0 && environments.every((environment) => environment.target === 'Kubernetes'
    ? Boolean(environment.kubeconfigRef?.trim() && environment.namespace?.trim())
    : environment.servers.length > 0)
  useEffect(() => onValidityChange(environmentsReady), [environmentsReady, onValidityChange])
  useEffect(() => onConfigurationChange(environments.map((environment) => ({
    displayName: environment.name,
    environment: apiEnvironment[environment.environment],
    runtime: runtimeForTarget[environment.target],
    servers: environment.target === 'Kubernetes' ? [] : environment.servers,
    tasks: [],
    taskSettings: {},
    kubeconfigRef: environment.kubeconfigRef ?? null,
    namespace: environment.namespace ?? null,
    runtimeSettings: environment.target === 'Kubernetes' || !environment.runtimeSettings || !Object.values(environment.runtimeSettings).some((value) => value !== null && value !== undefined)
      ? null
      : Object.fromEntries(Object.entries(environment.runtimeSettings).filter(([, value]) => value !== null && value !== undefined)),
  }))), [environments, onConfigurationChange])
  const openAddEnvironment = () => {
    if (!availableEnvironment) return
    setEditingEnvironment(null)
    setEnvironmentDraft({ name: environmentNames[availableEnvironment], environment: availableEnvironment, target: environments[0]?.target ?? 'Docker' })
    setAddEnv(true)
  }
  const openEditEnvironment = () => {
    if (!current) return
    setEditingEnvironment(active)
    setEnvironmentDraft({ name: current.name, environment: current.environment, target: current.target })
    setAddEnv(true)
  }
  const closeEnvironmentModal = () => { setAddEnv(false); setEditingEnvironment(null) }
  const saveEnvironment = () => {
    const normalizedDraft = { ...environmentDraft, name: environmentDraft.name.trim() }
    if (!normalizedDraft.name || duplicateEnvironment) return
    const defaultServers: string[] = []
    if (editingEnvironment === null) {
      setEnvironments([...environments, { ...normalizedDraft, servers: defaultServers, ...(normalizedDraft.target === 'Kubernetes' ? kubernetesDefaults(normalizedDraft.environment) : {}) }])
      setActive(environments.length)
    } else {
      setEnvironments((items) => items.map((item, index) => {
        if (index !== editingEnvironment) return item
        const keepKubernetesConfig = item.target === 'Kubernetes' && item.environment === normalizedDraft.environment && Boolean(item.kubeconfigRef?.trim() && item.namespace?.trim())
        return {
          ...item,
          ...normalizedDraft,
          servers: normalizedDraft.target === 'Kubernetes' ? [] : (item.servers.length ? item.servers : defaultServers),
          ...(normalizedDraft.target === 'Kubernetes'
            ? keepKubernetesConfig
              ? { kubeconfigRef: item.kubeconfigRef, namespace: item.namespace }
              : kubernetesDefaults(normalizedDraft.environment)
            : { kubeconfigRef: undefined, namespace: undefined }),
        }
      }))
    }
    closeEnvironmentModal()
  }
  const updateCurrent = (changes: Partial<EnvironmentConfig>) => setEnvironments((items) => items.map((item, index) => index === active ? { ...item, ...changes } : item))
  const [manualServer, setManualServer] = useState('')
  const openServerPicker = () => {
    setServerDraft(current.servers)
    setManualServer('')
    setSelectServers(true)
  }
  const pickerCandidates = current ? targetServers.filter((server) => server.environment === current.environment) : []
  const siteSlug = current ? ({ Dev: 'dev', Staging: 'staging', Production: 'prod' } as const)[current.environment] : 'dev'
  const inventoryNotice = inventoryStatus === 'not_configured'
    ? 'DCIM is not configured (NETCI_DCIM_BASE_URL). Manual hostnames will not be validated by NetBox before deployment.'
    : inventoryStatus === 'not_registered'
      ? `NetBox does not have tenant "${systemId}"${moduleId ? ` with device role "${moduleId}"` : ''}. You can register devices in NetBox (tenant = system, device role = module, site = dev/staging/prod) or add manual hostnames below.`
      : inventoryStatus === 'error'
        ? 'Cannot query DCIM. Check NetBox service status.'
        : null
  return <div className="deployment-builder"><aside><div><h3>Environments</h3><button aria-label="Add environment" disabled={!availableEnvironment} title={availableEnvironment ? undefined : 'Dev, Staging and Production are already configured'} onClick={openAddEnvironment}><Plus size={16} /></button></div>{environments.map((environment, index) => <button className={active === index ? 'active' : ''} onClick={() => setActive(index)} key={`${environment.name}-${index}`}><span className={`env-dot env-${environment.environment.toLowerCase()}`} /><span><strong>{environment.name}</strong><small>{environment.target}</small></span><ChevronRight size={16} /></button>)}{!environments.length && <div className="empty-environments"><Globe2 size={24} /><p>No environments yet</p><button onClick={openAddEnvironment}>Add environment</button></div>}</aside><main>{inventoryNotice && <div className="inline-error" role="status" style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}><div>{inventoryNotice}</div>{inventoryStatus === 'not_registered' && <div><button type="button" className="primary-button" style={{ padding: '5px 12px', fontSize: '0.85rem' }} disabled={provisioning} onClick={handleAutoProvision}><Zap size={14} style={{ marginRight: '6px' }} />{provisioning ? 'Đang tạo NetBox Targets…' : '⚡ Auto-provision NetBox Tenant & Targets'}</button></div>}</div>}{current ? <><div className="panel" style={{ marginBottom: '16px', padding: '12px 16px' }}><div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px' }}><div><h3 style={{ margin: 0, fontSize: '0.95rem' }}>Target Runtime Environment</h3><p style={{ margin: '2px 0 0', fontSize: '0.8rem', color: 'var(--text-muted)' }}>Select deployment runtime target for ({current.name}):</p></div><div className="segmented compact">{deploymentTargets.map(({ label, icon: TargetIcon }) => <button key={label} type="button" className={current.target === label ? 'active' : ''} onClick={() => setEnvironments((items) => items.map((item) => ({ ...item, target: label, servers: label === 'Kubernetes' ? [] : item.servers, ...(label === 'Kubernetes' ? kubernetesDefaults(item.environment) : { kubeconfigRef: undefined, namespace: undefined }) })))}><TargetIcon size={14} style={{ marginRight: '6px' }} />{label}</button>)}</div></div></div><div className="environment-title"><div><h2>{current.name}</h2><p>{current.environment} · {current.target} deployment target</p></div><button className="secondary-button" onClick={openEditEnvironment}><Settings2 size={15} />Edit</button></div><TargetConfiguration current={current} targetServers={targetServers} updateCurrent={updateCurrent} onSelectServers={openServerPicker} /><section className="deployment-section panel"><div className="section-heading"><div><h3>Managed Deployment Adapter</h3><p>The checked-in {current.target} playbook manages the task order, immutable artifact deployment, health gate, and rollback behavior. Target selection above is the runtime routing control exposed here.</p></div></div></section></> : <div className="deployment-empty"><Globe2 size={34} /><h2>Configure deployment environments</h2><p>Add Dev, Staging or Production and bind its runtime target.</p><button className="primary-button" onClick={openAddEnvironment}><Plus size={16} />Add environment</button></div>}</main>

    {addEnv && <Modal title={editingEnvironment === null ? 'Add environment' : 'Edit environment'} description="Create a deployment target for this module." onClose={closeEnvironmentModal} footer={<><button className="secondary-button" onClick={closeEnvironmentModal}>Cancel</button><button className="primary-button" disabled={!environmentDraft.name.trim() || duplicateEnvironment} onClick={saveEnvironment}>{editingEnvironment === null ? 'Add environment' : 'Save changes'}</button></>}><div className="form-grid"><label className="field full"><span>Name</span><input value={environmentDraft.name} onChange={(event) => setEnvironmentDraft({ ...environmentDraft, name: event.target.value })} /></label><label className="field full"><span>Environment</span><select value={environmentDraft.environment} onChange={(event) => setEnvironmentDraft({ ...environmentDraft, environment: event.target.value as DeploymentEnvironment, name: environmentNames[event.target.value as DeploymentEnvironment] })}>{(['Dev', 'Staging', 'Production'] as DeploymentEnvironment[]).map((environment) => <option disabled={environments.some((item, index) => item.environment === environment && index !== editingEnvironment)} key={environment}>{environment}</option>)}</select></label>{duplicateEnvironment && <div className="inline-error full" role="alert">This environment is already configured.</div>}<div className="field full"><span>Deployment target</span><div className="target-options">{deploymentTargets.map(({ label, icon: TargetIcon, execution }) => <button className={environmentDraft.target === label ? 'selected' : ''} disabled={Boolean(lockedTarget && label !== lockedTarget)} title={lockedTarget && label !== lockedTarget ? `Application runtime is already ${lockedTarget}` : undefined} onClick={() => setEnvironmentDraft({ ...environmentDraft, target: label })} key={label}><TargetIcon size={18} /><span><strong>{label}</strong><small>{execution}</small></span></button>)}</div>{lockedTarget && <small className="target-policy-hint">Application runtime is shared by all deployment environments.</small>}</div></div></Modal>}
    {selectServers && <Modal title="Select target servers" description={`DCIM servers assigned to ${current.name}.`} onClose={() => setSelectServers(false)} footer={<><button className="secondary-button" onClick={() => setSelectServers(false)}>Cancel</button><button className="primary-button" disabled={!serverDraft.length} onClick={() => { updateCurrent({ servers: serverDraft }); setSelectServers(false) }}>Add selected servers</button></>}><div className="server-picker">{pickerCandidates.map(({ name, ip, status }) => <label key={name}><input type="checkbox" checked={serverDraft.includes(name)} onChange={(event) => setServerDraft((items) => event.target.checked ? [...items, name] : items.filter((item) => item !== name))} /><Server size={17} /><span><strong>{name}</strong><small>{ip || 'No IP in NetBox'} · NetBox: {status}</small></span></label>)}{!pickerCandidates.length && <div className="inline-empty"><Server size={23} /><span>{inventoryStatus === 'ready' ? `NetBox has no devices for tenant "${systemId}" at site "${siteSlug}".` : inventoryNotice}</span></div>}</div>{(inventoryStatus !== 'ready' || !pickerCandidates.length) && <div className="form-grid"><label className="field full"><span>Hostname (Manual Target Server)</span><input value={manualServer} placeholder="e.g. banking-core-docker-dev or localhost" onChange={(event) => setManualServer(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && manualServer.trim()) { setServerDraft((items) => items.includes(manualServer.trim()) ? items : [...items, manualServer.trim()]); setManualServer('') } }} /><small>Press Enter to add. Configured: {serverDraft.join(', ') || 'None'}</small></label></div>}</Modal>}

  </div>
}

export function NewModuleWizard({ systemId, ownerTeams = [], canOwnAnyTeam = false, onCancel, onCreate }: { systemId: string; ownerTeams?: string[]; canOwnAnyTeam?: boolean; onCancel: () => void; onCreate: (module: DcimModule, configuration: ModuleWizardSubmission) => Promise<void> | void }) {
  const { notify } = usePortalFeedback()
  const [step, setStep] = useState(1)
  const [selected, setSelected] = useState('')
  const [dcimModules, setDcimModules] = useState<DcimModule[]>([])
  const [dcimStatus, setDcimStatus] = useState('loading')
  const [moduleSource, setModuleSource] = useState<'local' | 'dcim'>('local')
  const [gitInfo, setGitInfo] = useState<GitInfo | null>(null)
  const [sampleApps, setSampleApps] = useState<SampleApps | null>(null)
  const [selectedTarget, setSelectedTarget] = useState<DeploymentTarget>('Docker')
  // Empty, not example values: a form submitted unchanged used to register a module
  // pointing at a repository that does not exist.
  const [customModule, setCustomModule] = useState<DcimModule>({
    id: '',
    name: '',
    code: '',
    type: 'Backend',
    repositoryUrl: '',
    registered: false,
  })
  const [ownerTeam, setOwnerTeam] = useState(ownerTeams[0] ?? '')
  const [portalInformation, setPortalInformation] = useState<PortalInformation>({ displayName: '', moduleType: 'Backend', description: '' })
  const [pipelineConfig, setPipelineConfig] = useState<ModulePipelineConfig>(defaultPipelineConfig)
  const [deploymentReady, setDeploymentReady] = useState(false)
  const [deploymentConfig, setDeploymentConfig] = useState<DeploymentEnvironmentConfig[]>([])
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    getGitInfo().then((info) => setGitInfo(info)).catch(() => setGitInfo(null))
    listSampleApps().then((result) => setSampleApps(result)).catch(() => setSampleApps(null))
  }, [])

  const handleSelectSample = (sample: GitSample) => {
    const targetMap: Record<Runtime, DeploymentTarget> = {
      docker: 'Docker',
      kubernetes: 'Kubernetes',
      systemd: 'Systemd',
    }
    const chosenTarget = targetMap[sample.runtime] || 'Docker'
    setSelectedTarget(chosenTarget)
    setCustomModule({
      id: sample.id,
      code: sample.id,
      name: sample.name,
      type: 'Backend',
      repositoryUrl: sample.repositoryUrl || `https://github.com/netci-delivery-platform/${sample.id}.git`,
      registered: false,
    })

    setSelected(sample.id)
    setPortalInformation({
      displayName: sample.name,
      moduleType: 'Backend',
      description: `Sample application from ${sample.path}`,
    })
  }

  useEffect(() => {
    let active = true
    listDcimModules(systemId)
      .then((modulesResult) => {
        if (!active) return
        // Only record what DCIM said. Switching tabs or pre-selecting here would
        // overwrite whatever the user had already typed by the time the answer came.
        setDcimModules(modulesResult.items)
        setDcimStatus(modulesResult.status)
      })
      .catch(() => {
        if (!active) return
        setDcimStatus('error')
      })
    return () => { active = false }
  }, [systemId])
  const selectModule = (moduleId: string) => {
    const module = dcimModules.find((item) => item.id === moduleId)
    if (!module) {
      // A locally defined module has no DCIM entry: the typed code *is* its id. Ignoring
      // it here left "Next" disabled forever unless a sample had been clicked first.
      setSelected(moduleSource === 'local' ? moduleId : '')
      return
    }
    setSelected(moduleId)
    setPortalInformation({
      displayName: module.name,
      moduleType: portalInformation.moduleType,
      description: module.description ?? '',
    })
  }
  // Switching source must not carry a DCIM selection into local mode or vice versa.
  const changeSource = (next: 'local' | 'dcim') => {
    setModuleSource(next)
    setSelected(next === 'local' ? customModule.code ?? '' : '')
  }
  const finish = async () => {
    const defaultConfig = deploymentConfig[0]
    if (!defaultConfig) {
      notify('Configure at least one deployment environment.', 'error')
      return
    }
    setCreating(true)
    try {
      const dcimModule = dcimModules.find((item) => item.id === selected)
      const selectedModule = moduleSource === 'local'
        ? customModule
        : dcimModule && { ...dcimModule, code: dcimModule.id, type: portalInformation.moduleType, repositoryUrl: customModule.repositoryUrl }
      if (!selectedModule) throw new Error('Module definition not found.')
      await onCreate(selectedModule, {
        ...portalInformation,
        displayName: portalInformation.displayName || selectedModule.name,
        moduleType: portalInformation.moduleType || selectedModule.type || 'Backend',
        runtime: defaultConfig.runtime,
        defaultEnvironment: defaultConfig.environment,
        deploymentEnvironments: deploymentConfig,
        stages: pipelineConfig.pipelines['CD Dev']?.stages || defaultPipelineStages,
        pipelineConfig,
        ownerTeam: ownerTeam || undefined,
      })
      notify('Module đã được tạo thành công và sẵn sàng nhận pipeline run.')
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Không thể tạo module.', 'error')
    } finally {
      setCreating(false)
    }
  }
  return <div className="new-module-page"><div className="wizard-header"><button className="back-button" onClick={onCancel}><ArrowLeft size={16} />Back to System</button><div><h1>New Module</h1><p>Add a module and configure its delivery lifecycle.</p></div><WizardSteps step={step} /></div><section className="wizard-shell">{step === 1 && <><GeneralStep systemId={systemId} modules={dcimModules} integrationStatus={dcimStatus} selected={selected} onSelect={selectModule} information={portalInformation} onInformationChange={setPortalInformation} source={moduleSource} onSourceChange={changeSource} customModule={customModule} onCustomModuleChange={setCustomModule} gitInfo={gitInfo} sampleApps={sampleApps} onSelectSample={handleSelectSample} />{(ownerTeams.length > 0 || canOwnAnyTeam) && <div className="wizard-content"><label className="field"><span>Owning team</span>{canOwnAnyTeam
  ? <><input list="owner-teams" value={ownerTeam} onChange={(event) => setOwnerTeam(event.target.value)} placeholder="vd. team-payments" /><datalist id="owner-teams">{ownerTeams.map((team) => <option key={team} value={team} />)}</datalist><small>Platform-admin có thể giao module cho bất kỳ team nào (tên group trong identity provider). Chỉ thành viên team đó xem được module; thay đổi prod cần một thành viên khác của team phê duyệt.</small></>
  : <><select value={ownerTeam} onChange={(event) => setOwnerTeam(event.target.value)}>{ownerTeams.map((team) => <option key={team}>{team}</option>)}</select><small>Only members of this verified identity team can access the module.</small></>}</label></div>}</>}{step === 2 && <CicdStep config={pipelineConfig} onConfigChange={setPipelineConfig} />}{step === 3 && <DeploymentStep systemId={systemId} moduleId={selected} initialTarget={selectedTarget} onValidityChange={setDeploymentReady} onConfigurationChange={setDeploymentConfig} />}</section><footer className="wizard-footer"><button className="secondary-button" disabled={creating} onClick={step === 1 ? onCancel : () => setStep(step - 1)}>{step === 1 ? 'Cancel' : 'Back'}</button>{step < 3 ? <button className="primary-button" disabled={step === 1 && (!selected || !portalInformation.displayName.trim() || !portalInformation.moduleType || !(customModule.repositoryUrl ?? '').trim() || ownerTeams.length > 0 && !ownerTeam) || step === 2 && !pipelineConfig.runner.trim()} onClick={() => setStep(step + 1)}>Next <ArrowRight size={16} /></button> : <button className="primary-button" disabled={!deploymentReady || creating} title={deploymentReady ? undefined : 'Complete an environment and its target connection'} onClick={finish}><Check size={16} />{creating ? 'Creating…' : 'Create Module'}</button>}</footer></div>
}
