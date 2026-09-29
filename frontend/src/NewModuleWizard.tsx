import { useEffect, useState } from 'react'
import {
  ArrowLeft, ArrowRight, Box, Check, CheckCircle2, ChevronRight, Container,
  GitBranch, Globe2, HardDrive,
  Layers3, Plus, Server, Settings2, X, Zap,
} from 'lucide-react'
import { Modal } from './PortalShell'
import { usePortalFeedback } from './PortalFeedback'
import {
  listDcimModules,
  listDcimServers,
  autoProvisionDcimTargets,
  getGitInfo,
  listSampleApps,
  listSharedPipelines,
  type DcimModule,
  type DeploymentEnvironmentConfig,
  type Environment,
  type GitInfo,
  type GitSample,
  type SampleApps,
  type ModulePipelineConfig,
  type Runtime,
  type RuntimeSettings,
  type SharedPipeline,
} from './api/netciClient'


type DeploymentEnvironment = 'Dev' | 'Staging' | 'Production'
type DeploymentTarget = 'Systemd' | 'Docker' | 'Kubernetes'
type EnvironmentConfig = { name: string; environment: DeploymentEnvironment; target: DeploymentTarget; servers: string[]; kubeconfigRef?: string; namespace?: string; runtimeSettings?: RuntimeSettings }
type EnvironmentDraft = Pick<EnvironmentConfig, 'name' | 'environment' | 'target'>
type PortalInformation = { displayName: string; moduleType: string; description: string }
export type ModuleWizardSubmission = PortalInformation & {
  runtime: Runtime
  defaultEnvironment: Environment
  deploymentEnvironments: DeploymentEnvironmentConfig[]
  stages: string[]
  pipelineConfig: ModulePipelineConfig
  ownerTeam?: string
  pipeline: string
}

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
  return <div className="wizard-steps">{[['1', 'General'], ['2', 'Pipeline (CI)'], ['3', 'Deployment (CD)']].map(([number, label], index) => <div className={step === index + 1 ? 'active' : step > index + 1 ? 'done' : ''} key={number}><span>{step > index + 1 ? <Check size={14} /> : number}</span><strong>{label}</strong>{index < 2 && <i />}</div>)}</div>
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
        <p>Select a module from DCIM catalog or create a local module configuration directly.</p>
      </div>
    </div>
    <div className="segmented compact" style={{ marginBottom: '16px' }}>
      <button className={source === 'local' ? 'active' : ''} onClick={() => onSourceChange('local')}>Create Local Module</button>
      <button className={source === 'dcim' ? 'active' : ''} onClick={() => onSourceChange('dcim')}>Select from DCIM ({modules.length})</button>
    </div>
    {source === 'dcim' ? (
      <>
        {integrationStatus === 'not_configured' && <div className="inline-error" role="status">DCIM is not configured (NETCI_DCIM_BASE_URL). You can switch to "Create Local Module" tab to continue.</div>}
        <div className="dcim-module-list">
          {modules.map((item) => <button disabled={item.registered} className={selected === item.id ? 'selected' : ''} onClick={() => onSelect(item.id)} key={item.id}>
            <span className="radio">{selected === item.id && <i />}</span>
            <span className="module-symbol"><Box size={17} /></span>
            <span><strong>{item.name}</strong><small>{item.id}{item.description ? ` · ${item.description}` : ''} · source: {item.source ?? 'DCIM'}</small></span>
            {item.registered ? <em>Already added</em> : <ChevronRight size={17} />}
          </button>)}
        </div>
        {integrationStatus === 'ready' && !modules.length && <div className="inline-empty"><Box size={23} /><span>NetBox has this tenant but no devices — no device roles available to select as a module.</span></div>}
        {integrationStatus === 'not_registered' && <div className="inline-empty"><Box size={23} /><span>NetBox has no tenant "{systemId}". Create a tenant and device (role = module) in NetBox, or use "Create Local Module".</span></div>}
        {integrationStatus === 'error' && <div className="inline-error" role="status">Failed to query DCIM — verify NetBox status. netCI does not invent module lists.</div>}
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
            <span>Module code / ID (lowercase, hyphens) *</span>
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
                placeholder="https://github.com/my-org/core-api or file:///path/to/local/git"
              />

            <div style={{ marginTop: '10px', padding: '10px 14px', background: 'rgba(59, 130, 246, 0.08)', borderRadius: '8px', border: '1px solid rgba(59, 130, 246, 0.2)' }}>
              <div style={{ fontWeight: 600, color: '#60a5fa', fontSize: '0.84rem', marginBottom: '3px' }}>
                💡 Distinction between Source Code and Pipeline Configuration (netci.yaml):
              </div>
              <p style={{ margin: 0, fontSize: '0.78rem', color: '#cbd5e1', lineHeight: 1.5 }}>
                • <strong>Source Code</strong>: Contains application business logic (.py, .ts, .go, Dockerfile...). Provide a Git URL checkoutable by Jenkins.<br />
                • <strong>netci.yaml</strong>: CI/CD workflow steps (Test, Build, Scan, Deploy), previewed and customizable in Step 2.
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
              placeholder="Describe the business purpose of the module..."
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
          <label className="field"><span>Module code (device role in NetBox)</span><input value={module.id} readOnly /></label>
          <label className="field"><span>Type *</span><select value={information.moduleType} onChange={(event) => onInformationChange({ ...information, moduleType: event.target.value })}><option value="">— select —</option><option>Backend</option><option>Frontend</option><option>Worker</option><option>Gateway</option></select></label>
          <label className="field full"><span>Repository URL *</span><input value={customModule.repositoryUrl} onChange={(event) => onCustomModuleChange({ ...customModule, repositoryUrl: event.target.value })} placeholder="Git URL checkoutable by Jenkins" /><small>NetBox only tracks hosts; module source repository is configured here.</small></label>
          <label className="field full"><span>Description</span><textarea value={information.description} onChange={(event) => onInformationChange({ ...information, description: event.target.value })} /></label>
        </div>
      </div>
    </>}
    <div className="info-banner">
      <CheckCircle2 size={18} />
      <div>
        <strong>Next Step: CI/CD &amp; Runtime Setup</strong>
        <p>In the next steps, you will select the shared CI pipeline and configure deployment targets (Kubernetes, Docker container, or Linux Systemd).</p>
      </div>
    </div>
  </div>
}

export function PipelineStep({
  value,
  onChange,
  onManagePipelines,
}: {
  value: string
  onChange: (name: string) => void
  onManagePipelines?: () => void
}) {
  const [pipelines, setPipelines] = useState<SharedPipeline[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setLoading(true)
    setError(null)
    listSharedPipelines()
      .then((items) => {
        if (!active) return
        setPipelines(items)
        setLoading(false)
      })
      .catch((err) => {
        if (!active) return
        setError(err instanceof Error ? err.message : 'Failed to load shared pipelines')
        setLoading(false)
      })
    return () => {
      active = false
    }
  }, [])

  const activePipelines = pipelines.filter((p) => p.activeVersion !== null)
  const unapprovedPipelines = pipelines.filter((p) => p.activeVersion === null)

  return (
    <div className="wizard-content">
      <div className="wizard-section-title">
        <span><GitBranch size={18} /></span>
        <div>
          <h2>Select Shared CI Pipeline</h2>
          <p>This shared pipeline executes CI stages (build, test, scan); continuous deployment (CD) is configured in the next step.</p>
        </div>
        <button type="button" className="secondary-button" onClick={() => onManagePipelines?.()}>
          Manage pipelines
        </button>
      </div>

      {loading && (
        <div className="panel empty-table" role="status">
          Loading pipelines…
        </div>
      )}

      {error && (
        <div className="inline-error" role="alert">
          Failed to load pipelines: {error}
        </div>
      )}

      {!loading && !error && (
        <>
          {activePipelines.length === 0 && (
            <div className="inline-empty" role="status" style={{ marginBottom: '16px', padding: '24px' }}>
              <GitBranch size={24} />
              <span>No approved pipelines found. An administrator must create and approve a pipeline on the Pipelines page.</span>
            </div>
          )}

          <div
            role="radiogroup"
            aria-label="Pipelines"
            className="pipeline-card-grid"
            style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: '14px' }}
          >
            {activePipelines.map((pipeline) => {
              const isSelected = value === pipeline.name
              return (
                <div
                  key={pipeline.name}
                  role="radio"
                  aria-checked={isSelected}
                  tabIndex={0}
                  onClick={() => onChange(pipeline.name)}
                  onKeyDown={(e) => {
                    if (e.key === ' ' || e.key === 'Enter') {
                      e.preventDefault()
                      onChange(pipeline.name)
                    }
                  }}
                  className={`panel pipeline-card selectable ${isSelected ? 'selected' : ''}`}
                  style={{
                    cursor: 'pointer',
                    padding: '16px',
                    border: isSelected ? '2px solid var(--accent, #3b82f6)' : '1px solid var(--border)',
                    borderRadius: '8px',
                    background: isSelected ? 'rgba(59, 130, 246, 0.05)' : 'var(--surface, #fff)',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '10px',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '8px' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <span
                        className="radio"
                        style={{
                          display: 'inline-flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          width: '16px',
                          height: '16px',
                          borderRadius: '50%',
                          border: isSelected ? '2px solid #3b82f6' : '1px solid #ccd0d6',
                          flexShrink: 0,
                        }}
                      >
                        {isSelected && (
                          <i
                            style={{
                              display: 'block',
                              width: '8px',
                              height: '8px',
                              borderRadius: '50%',
                              background: '#3b82f6',
                            }}
                          />
                        )}
                      </span>
                      <strong style={{ fontSize: '1rem', color: isSelected ? '#2563eb' : 'inherit' }}>
                        {pipeline.name}
                      </strong>
                    </div>
                    <span
                      style={{
                        fontSize: '0.75rem',
                        fontWeight: 600,
                        padding: '2px 8px',
                        borderRadius: '12px',
                        background: 'rgba(59, 130, 246, 0.12)',
                        color: '#2563eb',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      v{pipeline.activeVersion}
                    </span>
                  </div>

                  {pipeline.description && (
                    <p style={{ margin: 0, fontSize: '0.82rem', color: 'var(--muted)', lineHeight: 1.4 }}>
                      {pipeline.description}
                    </p>
                  )}

                  {pipeline.stages && pipeline.stages.length > 0 && (
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginTop: 'auto' }}>
                      {pipeline.stages.map((st) => (
                        <span
                          key={st.id}
                          className="stage-chip"
                          style={{
                            fontSize: '0.72rem',
                            padding: '2px 6px',
                            borderRadius: '4px',
                            background: 'rgba(255,255,255,0.06)',
                            border: '1px solid var(--border)',
                          }}
                        >
                          {st.name}
                        </span>
                      ))}
                    </div>
                  )}

                  <div style={{ fontSize: '0.76rem', color: 'var(--muted)', marginTop: '4px' }}>
                    used by {pipeline.usedBy?.length ?? 0} module{pipeline.usedBy?.length === 1 ? '' : 's'}
                  </div>
                </div>
              )
            })}

            {unapprovedPipelines.map((pipeline) => (
              <div
                key={pipeline.name}
                role="radio"
                aria-checked={false}
                aria-disabled="true"
                tabIndex={-1}
                className="panel pipeline-card disabled"
                style={{
                  cursor: 'not-allowed',
                  opacity: 0.6,
                  padding: '16px',
                  border: '1px dashed var(--border)',
                  borderRadius: '8px',
                  background: 'var(--surface-alt, #fafbfc)',
                  display: 'flex',
                  flexDirection: 'column',
                  gap: '10px',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '8px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <span
                      className="radio"
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        width: '16px',
                        height: '16px',
                        borderRadius: '50%',
                        border: '1px solid #ccd0d6',
                        opacity: 0.5,
                        flexShrink: 0,
                      }}
                    />
                    <strong style={{ fontSize: '1rem' }}>{pipeline.name}</strong>
                  </div>
                  <span
                    style={{
                      fontSize: '0.75rem',
                      fontWeight: 600,
                      padding: '2px 8px',
                      borderRadius: '12px',
                      background: 'rgba(239, 68, 68, 0.1)',
                      color: '#dc2626',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    pending approval
                  </span>
                </div>

                {pipeline.description && (
                  <p style={{ margin: 0, fontSize: '0.82rem', color: 'var(--muted)', lineHeight: 1.4 }}>
                    {pipeline.description}
                  </p>
                )}

                {pipeline.stages && pipeline.stages.length > 0 && (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginTop: 'auto' }}>
                    {pipeline.stages.map((st) => (
                      <span
                        key={st.id}
                        className="stage-chip"
                        style={{
                          fontSize: '0.72rem',
                          padding: '2px 6px',
                          borderRadius: '4px',
                          background: 'rgba(255,255,255,0.06)',
                          border: '1px solid var(--border)',
                        }}
                      >
                        {st.name}
                      </span>
                    ))}
                  </div>
                )}

                <div style={{ fontSize: '0.76rem', color: 'var(--muted)', marginTop: '4px' }}>
                  used by {pipeline.usedBy?.length ?? 0} module{pipeline.usedBy?.length === 1 ? '' : 's'}
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
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
      notify(`Successfully auto-provisioned ${res.provisioned} NetBox targets for tenant "${systemId}"!`)
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
      notify(err instanceof Error ? err.message : 'Failed to auto-provision NetBox targets', 'error')
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
  return <div className="deployment-builder"><aside><div><h3>Environments</h3><button aria-label="Add environment" disabled={!availableEnvironment} title={availableEnvironment ? undefined : 'Dev, Staging and Production are already configured'} onClick={openAddEnvironment}><Plus size={16} /></button></div>{environments.map((environment, index) => <button className={active === index ? 'active' : ''} onClick={() => setActive(index)} key={`${environment.name}-${index}`}><span className={`env-dot env-${environment.environment.toLowerCase()}`} /><span><strong>{environment.name}</strong><small>{environment.target}</small></span><ChevronRight size={16} /></button>)}{!environments.length && <div className="empty-environments"><Globe2 size={24} /><p>No environments yet</p><button onClick={openAddEnvironment}>Add environment</button></div>}</aside><main>{inventoryNotice && <div className="inline-error" role="status" style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}><div>{inventoryNotice}</div>{inventoryStatus === 'not_registered' && <div><button type="button" className="primary-button" style={{ padding: '5px 12px', fontSize: '0.85rem' }} disabled={provisioning} onClick={handleAutoProvision}><Zap size={14} style={{ marginRight: '6px' }} />{provisioning ? 'Provisioning NetBox Targets…' : '⚡ Auto-provision NetBox Tenant & Targets'}</button></div>}</div>}{current ? <><div className="panel" style={{ marginBottom: '16px', padding: '12px 16px' }}><div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px' }}><div><h3 style={{ margin: 0, fontSize: '0.95rem' }}>Target Runtime Environment</h3><p style={{ margin: '2px 0 0', fontSize: '0.8rem', color: 'var(--text-muted)' }}>Select deployment runtime target for ({current.name}):</p></div><div className="segmented compact">{deploymentTargets.map(({ label, icon: TargetIcon }) => <button key={label} type="button" className={current.target === label ? 'active' : ''} onClick={() => setEnvironments((items) => items.map((item) => ({ ...item, target: label, servers: label === 'Kubernetes' ? [] : item.servers, ...(label === 'Kubernetes' ? kubernetesDefaults(item.environment) : { kubeconfigRef: undefined, namespace: undefined }) })))}><TargetIcon size={14} style={{ marginRight: '6px' }} />{label}</button>)}</div></div></div><div className="environment-title"><div><h2>{current.name}</h2><p>{current.environment} · {current.target} deployment target</p></div><button className="secondary-button" onClick={openEditEnvironment}><Settings2 size={15} />Edit</button></div><TargetConfiguration current={current} targetServers={targetServers} updateCurrent={updateCurrent} onSelectServers={openServerPicker} /><section className="deployment-section panel"><div className="section-heading"><div><h3>Managed Deployment Adapter</h3><p>The checked-in {current.target} playbook manages the task order, immutable artifact deployment, health gate, and rollback behavior. Target selection above is the runtime routing control exposed here.</p></div></div></section></> : <div className="deployment-empty"><Globe2 size={34} /><h2>Configure deployment environments</h2><p>Add Dev, Staging or Production and bind its runtime target.</p><button className="primary-button" onClick={openAddEnvironment}><Plus size={16} />Add environment</button></div>}</main>

    {addEnv && <Modal title={editingEnvironment === null ? 'Add environment' : 'Edit environment'} description="Create a deployment target for this module." onClose={closeEnvironmentModal} footer={<><button className="secondary-button" onClick={closeEnvironmentModal}>Cancel</button><button className="primary-button" disabled={!environmentDraft.name.trim() || duplicateEnvironment} onClick={saveEnvironment}>{editingEnvironment === null ? 'Add environment' : 'Save changes'}</button></>}><div className="form-grid"><label className="field full"><span>Name</span><input value={environmentDraft.name} onChange={(event) => setEnvironmentDraft({ ...environmentDraft, name: event.target.value })} /></label><label className="field full"><span>Environment</span><select value={environmentDraft.environment} onChange={(event) => setEnvironmentDraft({ ...environmentDraft, environment: event.target.value as DeploymentEnvironment, name: environmentNames[event.target.value as DeploymentEnvironment] })}>{(['Dev', 'Staging', 'Production'] as DeploymentEnvironment[]).map((environment) => <option disabled={environments.some((item, index) => item.environment === environment && index !== editingEnvironment)} key={environment}>{environment}</option>)}</select></label>{duplicateEnvironment && <div className="inline-error full" role="alert">This environment is already configured.</div>}<div className="field full"><span>Deployment target</span><div className="target-options">{deploymentTargets.map(({ label, icon: TargetIcon, execution }) => <button className={environmentDraft.target === label ? 'selected' : ''} disabled={Boolean(lockedTarget && label !== lockedTarget)} title={lockedTarget && label !== lockedTarget ? `Application runtime is already ${lockedTarget}` : undefined} onClick={() => setEnvironmentDraft({ ...environmentDraft, target: label })} key={label}><TargetIcon size={18} /><span><strong>{label}</strong><small>{execution}</small></span></button>)}</div>{lockedTarget && <small className="target-policy-hint">Application runtime is shared by all deployment environments.</small>}</div></div></Modal>}
    {selectServers && <Modal title="Select target servers" description={`DCIM servers assigned to ${current.name}.`} onClose={() => setSelectServers(false)} footer={<><button className="secondary-button" onClick={() => setSelectServers(false)}>Cancel</button><button className="primary-button" disabled={!serverDraft.length} onClick={() => { updateCurrent({ servers: serverDraft }); setSelectServers(false) }}>Add selected servers</button></>}><div className="server-picker">{pickerCandidates.map(({ name, ip, status }) => <label key={name}><input type="checkbox" checked={serverDraft.includes(name)} onChange={(event) => setServerDraft((items) => event.target.checked ? [...items, name] : items.filter((item) => item !== name))} /><Server size={17} /><span><strong>{name}</strong><small>{ip || 'No IP in NetBox'} · NetBox: {status}</small></span></label>)}{!pickerCandidates.length && <div className="inline-empty"><Server size={23} /><span>{inventoryStatus === 'ready' ? `NetBox has no devices for tenant "${systemId}" at site "${siteSlug}".` : inventoryNotice}</span></div>}</div>{(inventoryStatus !== 'ready' || !pickerCandidates.length) && <div className="form-grid"><label className="field full"><span>Hostname (Manual Target Server)</span><input value={manualServer} placeholder="e.g. payments-api-docker-dev or localhost" onChange={(event) => setManualServer(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && manualServer.trim()) { setServerDraft((items) => items.includes(manualServer.trim()) ? items : [...items, manualServer.trim()]); setManualServer('') } }} /><small>Press Enter to add. Configured: {serverDraft.join(', ') || 'None'}</small></label></div>}</Modal>}

  </div>
}

export function NewModuleWizard({
  systemId,
  ownerTeams = [],
  canOwnAnyTeam = false,
  onCancel,
  onCreate,
  onManagePipelines,
}: {
  systemId: string
  ownerTeams?: string[]
  canOwnAnyTeam?: boolean
  onCancel: () => void
  onCreate: (module: DcimModule, configuration: ModuleWizardSubmission) => Promise<void> | void
  onManagePipelines?: () => void
}) {
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
  const [selectedPipeline, setSelectedPipeline] = useState('')
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
        stages: defaultPipelineStages,
        pipelineConfig: defaultPipelineConfig,
        ownerTeam: ownerTeam || undefined,
        pipeline: selectedPipeline,
      })
      notify('Module created successfully and ready for pipeline runs.')
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Failed to create module.', 'error')
    } finally {
      setCreating(false)
    }
  }
  return <div className="new-module-page"><div className="wizard-header"><button className="back-button" onClick={onCancel}><ArrowLeft size={16} />Back to System</button><div><h1>New Module</h1><p>Add a module and configure its delivery lifecycle.</p></div><WizardSteps step={step} /></div><section className="wizard-shell">{step === 1 && <><GeneralStep systemId={systemId} modules={dcimModules} integrationStatus={dcimStatus} selected={selected} onSelect={selectModule} information={portalInformation} onInformationChange={setPortalInformation} source={moduleSource} onSourceChange={changeSource} customModule={customModule} onCustomModuleChange={setCustomModule} gitInfo={gitInfo} sampleApps={sampleApps} onSelectSample={handleSelectSample} />{(ownerTeams.length > 0 || canOwnAnyTeam) && <div className="wizard-content"><label className="field"><span>Owning team</span>{canOwnAnyTeam
  ? <><input list="owner-teams" value={ownerTeam} onChange={(event) => setOwnerTeam(event.target.value)} placeholder="e.g. team-payments" /><datalist id="owner-teams">{ownerTeams.map((team) => <option key={team} value={team} />)}</datalist><small>Platform admins can assign modules to any team (group name in identity provider). Only verified team members can view; production promotions require approval from another team member.</small></>
  : <><select value={ownerTeam} onChange={(event) => setOwnerTeam(event.target.value)}>{ownerTeams.map((team) => <option key={team}>{team}</option>)}</select><small>Only members of this verified identity team can access the module.</small></>}</label></div>}</>}{step === 2 && <PipelineStep value={selectedPipeline} onChange={setSelectedPipeline} onManagePipelines={onManagePipelines} />}{step === 3 && <DeploymentStep systemId={systemId} moduleId={selected} initialTarget={selectedTarget} onValidityChange={setDeploymentReady} onConfigurationChange={setDeploymentConfig} />}</section><footer className="wizard-footer"><button className="secondary-button" disabled={creating} onClick={step === 1 ? onCancel : () => setStep(step - 1)}>{step === 1 ? 'Cancel' : 'Back'}</button>{step < 3 ? <button className="primary-button" disabled={(step === 1 && (!selected || !portalInformation.displayName.trim() || !portalInformation.moduleType || !(customModule.repositoryUrl ?? '').trim() || (ownerTeams.length > 0 && !ownerTeam))) || (step === 2 && !selectedPipeline)} onClick={() => setStep(step + 1)}>Next <ArrowRight size={16} /></button> : <button className="primary-button" disabled={!deploymentReady || creating} title={deploymentReady ? undefined : 'Complete an environment and its target connection'} onClick={finish}><Check size={16} />{creating ? 'Creating…' : 'Create Module'}</button>}</footer></div>
}
