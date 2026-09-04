import { useEffect, useState } from 'react'
import {
  ArrowLeft, ArrowRight, Box, Check, CheckCircle2, ChevronRight, Code2, Container,
  FileCode2, GitBranch, Globe2, HardDrive,
  Layers3, Plus, Server, Settings2, X,
} from 'lucide-react'
import { Modal } from './PortalShell'
import { usePortalFeedback } from './PortalFeedback'
import { listDcimModules, listDcimServers, type DcimModule, type DeploymentEnvironmentConfig, type Environment, type ModulePipelineConfig, type ModulePipelineTabConfig, type Runtime } from './api/netciClient'

type DeploymentEnvironment = 'Dev' | 'Staging' | 'Production'
type DeploymentTarget = 'Systemd' | 'Docker' | 'Kubernetes'
type EnvironmentConfig = { name: string; environment: DeploymentEnvironment; target: DeploymentTarget; servers: string[]; kubeconfigRef?: string; namespace?: string }
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
const defaultPipelineConfig: ModulePipelineConfig = {
  runner: 'docker-linux',
  strategy: 'Gitflow',
  pipelines: Object.fromEntries(['CI', 'CD Dev', 'CD Staging', 'CD Prod', 'Automation Test'].map((tab) => [tab, {
    branch: tab === 'CI' ? 'main, merge_requests' : tab === 'CD Prod' ? 'tags/v*' : 'develop',
    coverageReportPath: 'coverage/lcov.info',
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

function GeneralStep({ modules = [], integrationStatus = 'loading', selected, onSelect, information, onInformationChange }: { modules?: DcimModule[]; integrationStatus?: string; selected: string; onSelect: (id: string) => void; information: PortalInformation; onInformationChange: (value: PortalInformation) => void }) {
  const module = modules.find((item) => item.id === selected)
  return <div className="wizard-content"><div className="wizard-section-title"><span><Layers3 size={18} /></span><div><h2>Select a DCIM module</h2><p>Choose a module returned by the configured DCIM integration.</p></div></div>{integrationStatus === 'not_configured' && <div className="inline-error" role="status">DCIM is not configured. Set NETCI_DCIM_BASE_URL on the API before adding a module.</div>}<div className="dcim-module-list">{modules.map((item) => <button disabled={item.registered} className={selected === item.id ? 'selected' : ''} onClick={() => onSelect(item.id)} key={item.id}><span className="radio">{selected === item.id && <i />}</span><span className="module-symbol"><Box size={17} /></span><span><strong>{item.name}</strong><small>{item.code} · {item.type}</small></span>{item.registered ? <em>Already added</em> : <ChevronRight size={17} />}</button>)}</div>{integrationStatus === 'ready' && !modules.length && <div className="inline-empty"><Box size={23} /><span>DCIM returned no modules for this system.</span></div>}{module && <><div className="wizard-section-title spaced"><span><Settings2 size={18} /></span><div><h2>Portal information</h2><p>Review display fields before continuing.</p></div></div><div className="panel wizard-form"><div className="form-grid"><label className="field full"><span>Display name</span><input value={information.displayName} onChange={(event) => onInformationChange({ ...information, displayName: event.target.value })} /></label><label className="field"><span>Module code</span><input value={module.code} readOnly /></label><label className="field"><span>Type</span><select value={information.moduleType} onChange={(event) => onInformationChange({ ...information, moduleType: event.target.value })}><option>Backend</option><option>Frontend</option><option>Worker</option><option>Gateway</option></select></label><label className="field full"><span>Git repository</span><input value={module.repositoryUrl} readOnly /></label><label className="field full"><span>Description</span><textarea value={information.description} onChange={(event) => onInformationChange({ ...information, description: event.target.value })} /></label></div></div><div className="info-banner"><CheckCircle2 size={18} /><div><strong>What happens next?</strong><p>Configure CI/CD pipelines, runners, environments, deployment tasks and target servers.</p></div></div></>}</div>
}

function PipelineDesigner({ initialConfig, onCancel, onSave }: { initialConfig: ModulePipelineConfig; onCancel: () => void; onSave: (config: ModulePipelineConfig) => void }) {
  const [tab, setTab] = useState('CI')
  const [mode, setMode] = useState<'visual' | 'code'>('visual')
  const [config, setConfig] = useState<ModulePipelineConfig>(() => ({ ...initialConfig, pipelines: Object.fromEntries(Object.entries(initialConfig.pipelines).map(([key, value]) => [key, { ...value, stages: [...value.stages] }])) }))
  const [validated, setValidated] = useState(false)
  const current = config.pipelines[tab]
  const updateCurrent = (changes: Partial<ModulePipelineTabConfig>) => setConfig({ ...config, pipelines: { ...config.pipelines, [tab]: { ...current, ...changes } } })
  const yaml = ['pipeline:', `  name: ${tab.toLowerCase().replace(/\s+/g, '-')}`, `  runner: ${config.runner}`, `  strategy: ${config.strategy.toLowerCase()}`, '  branches:', `    - ${current.branch}`, `  coverageReportPath: ${current.coverageReportPath}`, '  stages:', ...current.stages.map((stage) => `    - ${stage}`)].join('\n')
  const valid = Boolean(config.runner.trim() && current.branch.trim() && current.coverageReportPath.trim() && current.stages.length)
  return <div className="pipeline-designer">
    <div className="designer-top">
      <button className="back-button" onClick={onCancel}><ArrowLeft size={16} />Back to CI / CD</button>
      <div className="segmented compact"><button className={mode === 'visual' ? 'active' : ''} onClick={() => setMode('visual')}>Visual</button><button className={mode === 'code' ? 'active' : ''} onClick={() => setMode('code')}>As code</button></div>
    </div>
    <div className="settings-pipeline-tabs">{Object.keys(config.pipelines).map((item) => <button className={tab === item ? 'active' : ''} onClick={() => { setTab(item); setValidated(false) }} key={item}>{item}</button>)}</div>
    {mode === 'visual' ? <>
      <div className="form-grid designer-form">
        <label className="field"><span>Runner routing label</span><input value={config.runner} onChange={(event) => setConfig({ ...config, runner: event.target.value })} placeholder="docker-linux" /><small>The configured CI engine resolves this label.</small></label>
        <label className="field"><span>Branch configuration</span><input value={current.branch} onChange={(event) => updateCurrent({ branch: event.target.value })} /></label>
        <label className="field full"><span>Coverage report path</span><input value={current.coverageReportPath} onChange={(event) => updateCurrent({ coverageReportPath: event.target.value })} /></label>
      </div>
      <div className="stage-list">
        <div className="section-heading"><h3>{tab} stages</h3><button className="secondary-button" disabled title="Register custom stages in the stage catalog first"><Plus size={15} />Add custom stage</button></div>
        {current.stages.map((stage, index) => <div key={`${stage}-${index}`}><span className="drag-handle">⠿</span><span className="stage-number">{index + 1}</span><strong>{stageLabels[stage] ?? stage}</strong><small>Catalog stage</small><button aria-label={`Configure ${stageLabels[stage] ?? stage}`} disabled title="Stage parameters are managed by the catalog"><Settings2 size={15} /></button><button aria-label={`Remove ${stageLabels[stage] ?? stage}`} onClick={() => updateCurrent({ stages: current.stages.filter((_, itemIndex) => itemIndex !== index) })}><X size={15} /></button></div>)}
      </div>
    </> : <div className="code-editor"><div><span>pipeline.yml · read-only projection</span><button onClick={() => setValidated(valid)}><Code2 size={15} />Validate</button>{validated && <small role="status">Required catalog fields are present</small>}</div><pre>{yaml}</pre></div>}
    <div className="designer-footer"><button className="primary-button" disabled={!valid} onClick={() => onSave(config)}><Check size={16} />Save configuration</button></div>
  </div>
}

function CicdStep({ config, onConfigChange }: { config: ModulePipelineConfig; onConfigChange: (config: ModulePipelineConfig) => void }) {
  const [designer, setDesigner] = useState(false)
  if (designer) return <PipelineDesigner initialConfig={config} onCancel={() => setDesigner(false)} onSave={(nextConfig) => { onConfigChange(nextConfig); setDesigner(false) }} />
  return <div className="wizard-content">
    <div className="wizard-section-title"><span><Server size={18} /></span><div><h2>Runner routing label</h2><p>This label is sent to the configured CI adapter. Availability is verified by the CI engine when the run starts.</p></div></div>
    <label className="field runner-label"><span>Runner label</span><input value={config.runner} onChange={(event) => onConfigChange({ ...config, runner: event.target.value })} placeholder="docker-linux" /></label>
    <div className="wizard-section-title spaced"><span><GitBranch size={18} /></span><div><h2>Branching and pipeline template</h2><p>Start from a managed template and customize its stages.</p></div></div>
    <div className="strategy-grid">{[['Gitflow', 'develop, release/* and main branches'], ['Trunk-based', 'Short-lived branches merged to main'], ['Custom Pipeline', 'Build every pipeline from scratch']].map(([name, description]) => <button className={config.strategy === name ? 'selected' : ''} onClick={() => onConfigChange({ ...config, strategy: name })} key={name}><span className="radio">{config.strategy === name && <i />}</span><GitBranch size={20} /><strong>{name}</strong><small>{description}</small>{name !== 'Custom Pipeline' && <em>Recommended</em>}</button>)}</div>
    <button className="configure-template" onClick={() => setDesigner(true)}><span><FileCode2 size={18} /><span><strong>Configure {config.strategy}</strong><small>Review CI, CD and Automation Test stages</small></span></span><ArrowRight size={17} /></button>
  </div>
}

function TargetConfiguration({ current, targetServers = [], updateCurrent, onSelectServers }: { current: EnvironmentConfig; targetServers?: TargetServer[]; updateCurrent: (changes: Partial<EnvironmentConfig>) => void; onSelectServers: () => void }) {
  if (current.target === 'Kubernetes') {
    return <section className="deployment-section panel"><div className="section-heading"><div><h3>Cluster access</h3><p>Reference stored cluster credentials; never paste kubeconfig contents into the module.</p></div></div><div className="form-grid target-connection-form"><label className="field"><span>Kubeconfig secret reference</span><input value={current.kubeconfigRef ?? ''} onChange={(event) => updateCurrent({ kubeconfigRef: event.target.value })} placeholder="netci-staging-kubeconfig" /><small>Secret reference resolved by the runtime adapter.</small></label><label className="field"><span>Target namespace</span><input value={current.namespace ?? ''} onChange={(event) => updateCurrent({ namespace: event.target.value })} placeholder="staging" /><small>The namespace must already exist and be authorized.</small></label></div></section>
  }
  return <section className="deployment-section panel"><div className="section-heading"><div><h3>Target servers</h3><p>Select servers returned by the DCIM module inventory.</p></div><button className="secondary-button" onClick={onSelectServers}><Plus size={15} />Select servers</button></div>{current.servers.length ? <div className="target-server-list">{current.servers.map((server) => { const target = targetServers.find((item) => item.name === server); return <div key={server}><HardDrive size={17} /><span><strong>{server}</strong><small>{target?.ip || 'Address unavailable'}</small></span><span className={`status status-${target?.status === 'online' ? 'online' : 'unknown'}`}><i />{target?.status || 'unknown'}</span><button aria-label={`Remove ${server}`} onClick={() => updateCurrent({ servers: current.servers.filter((item) => item !== server) })}><X size={15} /></button></div> })}</div> : <div className="inline-empty"><Server size={23} /><span>No target servers selected</span></div>}</section>
}

function DeploymentStep({ systemId, moduleId, targetServers = [], onValidityChange, onConfigurationChange }: { systemId: string; moduleId: string; targetServers?: TargetServer[]; onValidityChange: (valid: boolean) => void; onConfigurationChange: (config: DeploymentEnvironmentConfig[]) => void }) {
  const [liveTargetServers, setLiveTargetServers] = useState<TargetServer[]>(targetServers)
  const [inventoryStatus, setInventoryStatus] = useState('loading')
  useEffect(() => {
    listDcimServers(systemId, moduleId).then((result) => {
      setInventoryStatus(result.status)
      setLiveTargetServers(result.items.map((item) => ({
      name: item.hostname || item.id,
      ip: item.ipAddress ?? '',
      environment: item.environment === 'prod' ? 'Production' : item.environment === 'staging' ? 'Staging' : 'Dev',
      status: item.status ?? 'unknown',
    })))
    }).catch(() => { setInventoryStatus('error'); setLiveTargetServers([]) })
  }, [systemId, moduleId])
  targetServers = liveTargetServers
  const [environments, setEnvironments] = useState<EnvironmentConfig[]>([])
  const [active, setActive] = useState(0)
  const [addEnv, setAddEnv] = useState(false)
  const [editingEnvironment, setEditingEnvironment] = useState<number | null>(null)
  const [environmentDraft, setEnvironmentDraft] = useState<EnvironmentDraft>({ name: 'Development', environment: 'Dev', target: 'Docker' })
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
    if (editingEnvironment === null) {
      setEnvironments([...environments, { ...normalizedDraft, servers: [], ...(normalizedDraft.target === 'Kubernetes' ? kubernetesDefaults(normalizedDraft.environment) : {}) }])
      setActive(environments.length)
    } else {
      setEnvironments((items) => items.map((item, index) => {
        if (index !== editingEnvironment) return item
        const keepKubernetesConfig = item.target === 'Kubernetes' && item.environment === normalizedDraft.environment && Boolean(item.kubeconfigRef?.trim() && item.namespace?.trim())
        return {
          ...item,
          ...normalizedDraft,
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
  const openServerPicker = () => {
    setServerDraft(current.servers.length
      ? current.servers
      : targetServers.filter((server) => server.environment === current.environment).map((server) => server.name))
    setSelectServers(true)
  }
  return <div className="deployment-builder"><aside><div><h3>Environments</h3><button aria-label="Add environment" disabled={!availableEnvironment} title={availableEnvironment ? undefined : 'Dev, Staging and Production are already configured'} onClick={openAddEnvironment}><Plus size={16} /></button></div>{environments.map((environment, index) => <button className={active === index ? 'active' : ''} onClick={() => setActive(index)} key={`${environment.name}-${index}`}><span className={`env-dot env-${environment.environment.toLowerCase()}`} /><span><strong>{environment.name}</strong><small>{environment.target}</small></span><ChevronRight size={16} /></button>)}{!environments.length && <div className="empty-environments"><Globe2 size={24} /><p>No environments yet</p><button onClick={openAddEnvironment}>Add environment</button></div>}</aside><main>{inventoryStatus === 'not_configured' && <div className="inline-error" role="status">DCIM server inventory is not configured. Kubernetes targets can still use a managed kubeconfig reference.</div>}{current ? <><div className="environment-title"><div><h2>{current.name}</h2><p>{current.environment} · {current.target} deployment target</p></div><button className="secondary-button" onClick={openEditEnvironment}><Settings2 size={15} />Edit</button></div><TargetConfiguration current={current} targetServers={targetServers} updateCurrent={updateCurrent} onSelectServers={openServerPicker} /><section className="deployment-section panel"><div className="section-heading"><div><h3>Managed deployment adapter</h3><p>The checked-in {current.target} playbook owns the task order, immutable artifact deployment, health gate and rollback behavior. Target selection above is the only runtime routing control exposed here.</p></div></div></section></> : <div className="deployment-empty"><Globe2 size={34} /><h2>Configure deployment environments</h2><p>Add Dev, Staging or Production and bind its runtime target.</p><button className="primary-button" onClick={openAddEnvironment}><Plus size={16} />Add environment</button></div>}</main>
    {addEnv && <Modal title={editingEnvironment === null ? 'Add environment' : 'Edit environment'} description="Create a deployment target for this module." onClose={closeEnvironmentModal} footer={<><button className="secondary-button" onClick={closeEnvironmentModal}>Cancel</button><button className="primary-button" disabled={!environmentDraft.name.trim() || duplicateEnvironment} onClick={saveEnvironment}>{editingEnvironment === null ? 'Add environment' : 'Save changes'}</button></>}><div className="form-grid"><label className="field full"><span>Name</span><input value={environmentDraft.name} onChange={(event) => setEnvironmentDraft({ ...environmentDraft, name: event.target.value })} /></label><label className="field full"><span>Environment</span><select value={environmentDraft.environment} onChange={(event) => setEnvironmentDraft({ ...environmentDraft, environment: event.target.value as DeploymentEnvironment, name: environmentNames[event.target.value as DeploymentEnvironment] })}>{(['Dev', 'Staging', 'Production'] as DeploymentEnvironment[]).map((environment) => <option disabled={environments.some((item, index) => item.environment === environment && index !== editingEnvironment)} key={environment}>{environment}</option>)}</select></label>{duplicateEnvironment && <div className="inline-error full" role="alert">This environment is already configured.</div>}<div className="field full"><span>Deployment target</span><div className="target-options">{deploymentTargets.map(({ label, icon: TargetIcon, execution }) => <button className={environmentDraft.target === label ? 'selected' : ''} disabled={Boolean(lockedTarget && label !== lockedTarget)} title={lockedTarget && label !== lockedTarget ? `Application runtime is already ${lockedTarget}` : undefined} onClick={() => setEnvironmentDraft({ ...environmentDraft, target: label })} key={label}><TargetIcon size={18} /><span><strong>{label}</strong><small>{execution}</small></span></button>)}</div>{lockedTarget && <small className="target-policy-hint">Application runtime is shared by all deployment environments.</small>}</div></div></Modal>}
    {selectServers && <Modal title="Select target servers" description={`DCIM servers assigned to ${current.name}.`} onClose={() => setSelectServers(false)} footer={<><button className="secondary-button" onClick={() => setSelectServers(false)}>Cancel</button><button className="primary-button" disabled={!serverDraft.length} onClick={() => { updateCurrent({ servers: serverDraft }); setSelectServers(false) }}>Add selected servers</button></>}><div className="server-picker">{targetServers.filter((server) => server.environment === current.environment).map(({ name, ip, status }) => <label key={name}><input type="checkbox" checked={serverDraft.includes(name)} onChange={(event) => setServerDraft((items) => event.target.checked ? [...items, name] : items.filter((item) => item !== name))} /><Server size={17} /><span><strong>{name}</strong><small>{ip || 'Address unavailable'} · {status || 'unknown'}</small></span></label>)}</div>{!targetServers.some((server) => server.environment === current.environment) && <div className="empty-table"><strong>No DCIM servers available</strong><span>The inventory returned no targets for this environment.</span></div>}</Modal>}
  </div>
}

export function NewModuleWizard({ systemId, ownerTeams = [], onCancel, onCreate }: { systemId: string; ownerTeams?: string[]; onCancel: () => void; onCreate: (module: DcimModule, configuration: ModuleWizardSubmission) => Promise<void> | void }) {
  const { notify } = usePortalFeedback()
  const [step, setStep] = useState(1)
  const [selected, setSelected] = useState('')
  const [dcimModules, setDcimModules] = useState<DcimModule[]>([])
  const [dcimStatus, setDcimStatus] = useState('loading')
  const [ownerTeam, setOwnerTeam] = useState(ownerTeams[0] ?? '')
  const [portalInformation, setPortalInformation] = useState<PortalInformation>({ displayName: '', moduleType: 'Backend', description: '' })
  const [pipelineConfig, setPipelineConfig] = useState<ModulePipelineConfig>(defaultPipelineConfig)
  const [deploymentReady, setDeploymentReady] = useState(false)
  const [deploymentConfig, setDeploymentConfig] = useState<DeploymentEnvironmentConfig[]>([])
  const [creating, setCreating] = useState(false)
  useEffect(() => {
    let active = true
    listDcimModules(systemId)
      .then((modulesResult) => {
        if (!active) return
        setDcimModules(modulesResult.items)
        setDcimStatus(modulesResult.status)
      })
      .catch((error) => {
        if (!active) return
        setDcimStatus('error')
        notify(error instanceof Error ? error.message : 'Cannot load DCIM modules.', 'error')
      })
    return () => { active = false }
  }, [systemId])
  const selectModule = (moduleId: string) => {
    const module = dcimModules.find((item) => item.id === moduleId)
    if (!module) return
    setSelected(moduleId)
    setPortalInformation({
      displayName: module.name,
      moduleType: module.type,
      description: '',
    })
  }
  const finish = async () => {
    const defaultConfig = deploymentConfig[0]
    if (!defaultConfig) {
      notify('Configure at least one deployment environment.', 'error')
      return
    }
    setCreating(true)
    try {
      const selectedModule = dcimModules.find((item) => item.id === selected)
      if (!selectedModule) throw new Error('The selected module is no longer available in DCIM.')
      await onCreate(selectedModule, {
        ...portalInformation,
        runtime: defaultConfig.runtime,
        defaultEnvironment: defaultConfig.environment,
        deploymentEnvironments: deploymentConfig,
        stages: pipelineConfig.pipelines.CI.stages,
        pipelineConfig,
        ownerTeam: ownerTeam || undefined,
      })
      notify('Module đã được tạo từ DCIM và sẵn sàng nhận pipeline run.')
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Không thể tạo module.', 'error')
    } finally {
      setCreating(false)
    }
  }
  return <div className="new-module-page"><div className="wizard-header"><button className="back-button" onClick={onCancel}><ArrowLeft size={16} />Back to System</button><div><h1>New Module</h1><p>Add a DCIM module and configure its delivery lifecycle.</p></div><WizardSteps step={step} /></div><section className="wizard-shell">{step === 1 && <><GeneralStep modules={dcimModules} integrationStatus={dcimStatus} selected={selected} onSelect={selectModule} information={portalInformation} onInformationChange={setPortalInformation} />{ownerTeams.length > 0 && <div className="wizard-content"><label className="field"><span>Owning team</span><select value={ownerTeam} onChange={(event) => setOwnerTeam(event.target.value)}>{ownerTeams.map((team) => <option key={team}>{team}</option>)}</select><small>Only members of this verified identity team can access the module.</small></label></div>}</>}{step === 2 && <CicdStep config={pipelineConfig} onConfigChange={setPipelineConfig} />}{step === 3 && <DeploymentStep systemId={systemId} moduleId={selected} onValidityChange={setDeploymentReady} onConfigurationChange={setDeploymentConfig} />}</section><footer className="wizard-footer"><button className="secondary-button" disabled={creating} onClick={step === 1 ? onCancel : () => setStep(step - 1)}>{step === 1 ? 'Cancel' : 'Back'}</button>{step < 3 ? <button className="primary-button" disabled={step === 1 && (!selected || !portalInformation.displayName.trim() || !portalInformation.moduleType || ownerTeams.length > 0 && !ownerTeam) || step === 2 && !pipelineConfig.runner.trim()} onClick={() => setStep(step + 1)}>Next <ArrowRight size={16} /></button> : <button className="primary-button" disabled={!deploymentReady || creating} title={deploymentReady ? undefined : 'Complete an environment and its target connection'} onClick={finish}><Check size={16} />{creating ? 'Creating…' : 'Create Module'}</button>}</footer></div>
}
