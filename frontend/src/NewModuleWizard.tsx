import { useEffect, useState } from 'react'
import {
  ArrowLeft, ArrowRight, Box, Check, CheckCircle2, ChevronRight, Code2, Container,
  Database, FileCode2, FolderPlus, GitBranch, Globe2, HardDrive, HeartPulse,
  Layers3, Plus, RotateCcw, Server, Settings2, Upload, X,
} from 'lucide-react'
import { Modal } from './PortalShell'
import { usePortalFeedback } from './PortalFeedback'
import type { DeploymentEnvironmentConfig, Environment, Runtime } from './api/netciClient'
import { dcimModules, deploymentTasks } from './portalData'

type DeploymentEnvironment = 'Dev' | 'Staging' | 'Production'
type DeploymentTarget = 'Systemd' | 'Docker' | 'Kubernetes'
type EnvironmentConfig = { name: string; environment: DeploymentEnvironment; target: DeploymentTarget; servers: string[]; tasks: string[]; kubeconfigRef?: string; namespace?: string }
type EnvironmentDraft = Pick<EnvironmentConfig, 'name' | 'environment' | 'target'>
type PortalInformation = { displayName: string; moduleType: string; description: string }
type ModuleWizardSubmission = PortalInformation & { runtime: Runtime; defaultEnvironment: Environment; deploymentEnvironments: DeploymentEnvironmentConfig[]; stages: string[] }

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

const targetServers = [
  { name: 'srv-dev-01', ip: '10.60.12.21', environment: 'Dev' as DeploymentEnvironment },
  { name: 'srv-dev-02', ip: '10.60.12.22', environment: 'Dev' as DeploymentEnvironment },
  { name: 'srv-stg-01', ip: '10.60.18.31', environment: 'Staging' as DeploymentEnvironment },
]

const defaultPipelineStages = ['checkout', 'unit-test', 'build', 'sbom', 'vulnerability-scan', 'sign', 'publish', 'deploy', 'health-check']
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

function GeneralStep({ selected, onSelect, information, onInformationChange }: { selected: string; onSelect: (id: string) => void; information: PortalInformation; onInformationChange: (value: PortalInformation) => void }) {
  const module = dcimModules.find((item) => item.id === selected)
  return <div className="wizard-content"><div className="wizard-section-title"><span><Layers3 size={18} /></span><div><h2>Select a DCIM module</h2><p>Choose a module registered under netChat.</p></div></div><div className="dcim-module-list">{dcimModules.map((item) => <button disabled={item.registered} className={selected === item.id ? 'selected' : ''} onClick={() => onSelect(item.id)} key={item.id}><span className="radio">{selected === item.id && <i />}</span><span className="module-symbol"><Box size={17} /></span><span><strong>{item.name}</strong><small>{item.code} · {item.type}</small></span>{item.registered ? <em>Already added</em> : <ChevronRight size={17} />}</button>)}</div>{module && <><div className="wizard-section-title spaced"><span><Settings2 size={18} /></span><div><h2>Portal information</h2><p>Review display fields before continuing.</p></div></div><div className="panel wizard-form"><div className="form-grid"><label className="field full"><span>Display name</span><input value={information.displayName} onChange={(event) => onInformationChange({ ...information, displayName: event.target.value })} /></label><label className="field"><span>Module code</span><input value={module.code} readOnly /></label><label className="field"><span>Type</span><select value={information.moduleType} onChange={(event) => onInformationChange({ ...information, moduleType: event.target.value })}><option>Backend</option><option>Frontend</option><option>Worker</option><option>Gateway</option></select></label><label className="field full"><span>Git repository</span><input value={module.repo} readOnly /></label><label className="field full"><span>Description</span><textarea value={information.description} onChange={(event) => onInformationChange({ ...information, description: event.target.value })} /></label></div></div><div className="info-banner"><CheckCircle2 size={18} /><div><strong>What happens next?</strong><p>Configure CI/CD pipelines, runners, environments, deployment tasks and target servers.</p></div></div></>}</div>
}

function PipelineDesigner({ initialStages, onCancel, onSave }: { initialStages: string[]; onCancel: () => void; onSave: (stages: string[]) => void }) {
  const [tab, setTab] = useState('CI')
  const [mode, setMode] = useState<'visual' | 'code'>('visual')
  const [stages, setStages] = useState(initialStages)
  const [validated, setValidated] = useState(false)
  const yaml = ['pipeline:', '  runner: runner-01', '  strategy: gitflow', '  stages:', ...stages.map((stage) => `    - ${stage}`)].join('\n')
  return <div className="pipeline-designer"><div className="designer-top"><button className="back-button" onClick={onCancel}><ArrowLeft size={16} />Back to CI / CD</button><div className="segmented compact"><button className={mode === 'visual' ? 'active' : ''} onClick={() => setMode('visual')}>Visual</button><button className={mode === 'code' ? 'active' : ''} onClick={() => setMode('code')}>As code</button></div></div><div className="settings-pipeline-tabs">{['CI', 'CD Dev', 'CD Staging', 'CD Prod', 'Automation Test'].map((item) => <button className={tab === item ? 'active' : ''} onClick={() => setTab(item)} key={item}>{item}</button>)}</div>{mode === 'visual' ? <><div className="form-grid designer-form"><label className="field"><span>Runner</span><select><option>Runner 01 · docker-linux</option><option>Runner 02 · on-prem</option></select></label><label className="field"><span>Branch configuration</span><input defaultValue={tab === 'CI' ? 'main, merge_requests' : tab === 'CD Prod' ? 'tags/v*' : 'develop'} /></label><label className="field full"><span>Coverage report path</span><input defaultValue="coverage/lcov.info" /></label></div><div className="stage-list"><div className="section-heading"><h3>{tab} stages</h3><button className="secondary-button" disabled title="Register custom stages in the stage catalog first"><Plus size={15} />Add custom stage</button></div>{stages.map((stage, index) => <div key={stage}><span className="drag-handle">⠿</span><span className="stage-number">{index + 1}</span><strong>{stageLabels[stage] ?? stage}</strong><small>{index < 2 ? 'Managed' : 'Catalog stage'}</small><button aria-label={`Configure ${stageLabels[stage] ?? stage}`} disabled title="Stage parameters are managed by the catalog"><Settings2 size={15} /></button><button aria-label={`Remove ${stageLabels[stage] ?? stage}`} onClick={() => setStages(stages.filter((_, itemIndex) => itemIndex !== index))}><X size={15} /></button></div>)}</div></> : <div className="code-editor"><div><span>pipeline.yml</span><button onClick={() => setValidated(true)}><Code2 size={15} />Validate</button>{validated && <small role="status">Valid catalog configuration</small>}</div><pre>{yaml}</pre></div>}<div className="designer-footer"><button className="primary-button" disabled={!stages.length} onClick={() => onSave(stages)}><Check size={16} />Save configuration</button></div></div>
}

function CicdStep({ stages, onStagesChange }: { stages: string[]; onStagesChange: (stages: string[]) => void }) {
  const [runnerMode, setRunnerMode] = useState<'system' | 'custom'>('system')
  const [strategy, setStrategy] = useState('Gitflow')
  const [designer, setDesigner] = useState(false)
  if (designer) return <PipelineDesigner initialStages={stages} onCancel={() => setDesigner(false)} onSave={(nextStages) => { onStagesChange(nextStages); setDesigner(false) }} />
  return <div className="wizard-content"><div className="wizard-section-title"><span><Server size={18} /></span><div><h2>Runner configuration</h2><p>Select a system runner or use a custom self-hosted runner.</p></div></div><div className="segmented wide"><button className={runnerMode === 'system' ? 'active' : ''} onClick={() => setRunnerMode('system')}>System runner</button><button className={runnerMode === 'custom' ? 'active' : ''} onClick={() => setRunnerMode('custom')}>Custom runner</button></div>{runnerMode === 'system' ? <div className="runner-grid">{[['Runner 01', 'docker-linux', 'Online'], ['Runner 02', 'on-prem', 'Online'], ['Runner GPU', 'cuda-linux', 'Busy']].map(([name, label, status], index) => <button className={index === 0 ? 'selected' : ''} key={name}><span className="radio">{index === 0 && <i />}</span><Server size={19} /><span><strong>{name}</strong><small>{label}</small></span><em className={status === 'Busy' ? 'busy' : ''}>{status}</em></button>)}</div> : <div className="custom-runner-list"><div><span className="status status-online"><i />Active</span><strong>self-hosted-gpu</strong><small>linux · x64 · GPU</small></div><div><span className="status status-offline"><i />Inactive</span><strong>onprem-arm64</strong><small>linux · arm64</small></div><button className="secondary-button"><Plus size={15} />Add runner</button></div>}<label className="field runner-label"><span>Runner label</span><input defaultValue={runnerMode === 'system' ? 'docker-linux' : 'self-hosted'} /></label><div className="wizard-section-title spaced"><span><GitBranch size={18} /></span><div><h2>Branching and pipeline template</h2><p>Start from a managed template and customize its stages.</p></div></div><div className="strategy-grid">{[['Gitflow', 'develop, release/* and main branches'], ['Trunk-based', 'Short-lived branches merged to main'], ['Custom Pipeline', 'Build every pipeline from scratch']].map(([name, description]) => <button className={strategy === name ? 'selected' : ''} onClick={() => setStrategy(name)} key={name}><span className="radio">{strategy === name && <i />}</span><GitBranch size={20} /><strong>{name}</strong><small>{description}</small>{name !== 'Custom Pipeline' && <em>Recommended</em>}</button>)}</div><button className="configure-template" onClick={() => setDesigner(true)}><span><FileCode2 size={18} /><span><strong>Configure {strategy}</strong><small>Review CI, CD and Automation Test stages</small></span></span><ArrowRight size={17} /></button></div>
}

function TargetConfiguration({ current, updateCurrent, onSelectServers }: { current: EnvironmentConfig; updateCurrent: (changes: Partial<EnvironmentConfig>) => void; onSelectServers: () => void }) {
  if (current.target === 'Kubernetes') {
    return <section className="deployment-section panel"><div className="section-heading"><div><h3>Cluster access</h3><p>Reference stored cluster credentials; never paste kubeconfig contents into the module.</p></div></div><div className="form-grid target-connection-form"><label className="field"><span>Kubeconfig secret reference</span><input value={current.kubeconfigRef ?? ''} onChange={(event) => updateCurrent({ kubeconfigRef: event.target.value })} placeholder="netci-staging-kubeconfig" /><small>Secret reference resolved by the runtime adapter.</small></label><label className="field"><span>Target namespace</span><input value={current.namespace ?? ''} onChange={(event) => updateCurrent({ namespace: event.target.value })} placeholder="staging" /><small>The namespace must already exist and be authorized.</small></label></div></section>
  }
  return <section className="deployment-section panel"><div className="section-heading"><div><h3>Target servers</h3><p>Select servers from the DCIM module group.</p></div><button className="secondary-button" onClick={onSelectServers}><Plus size={15} />Select servers</button></div>{current.servers.length ? <div className="target-server-list">{current.servers.map((server) => <div key={server}><HardDrive size={17} /><span><strong>{server}</strong><small>{targetServers.find((item) => item.name === server)?.ip ?? 'Address unavailable'}</small></span><span className="status status-online"><i />Online</span><button aria-label={`Remove ${server}`} onClick={() => updateCurrent({ servers: current.servers.filter((item) => item !== server) })}><X size={15} /></button></div>)}</div> : <div className="inline-empty"><Server size={23} /><span>No target servers selected</span></div>}</section>
}

function DeploymentStep({ onValidityChange, onConfigurationChange }: { onValidityChange: (valid: boolean) => void; onConfigurationChange: (config: DeploymentEnvironmentConfig[]) => void }) {
  const [environments, setEnvironments] = useState<EnvironmentConfig[]>([])
  const [active, setActive] = useState(0)
  const [addEnv, setAddEnv] = useState(false)
  const [editingEnvironment, setEditingEnvironment] = useState<number | null>(null)
  const [environmentDraft, setEnvironmentDraft] = useState<EnvironmentDraft>({ name: 'Development', environment: 'Dev', target: 'Docker' })
  const [selectServers, setSelectServers] = useState(false)
  const [serverDraft, setServerDraft] = useState<string[]>([])
  const [taskPicker, setTaskPicker] = useState(false)
  const [healthConfig, setHealthConfig] = useState(false)
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
    tasks: environment.tasks,
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
      setEnvironments([...environments, { ...normalizedDraft, servers: [], tasks: [], ...(normalizedDraft.target === 'Kubernetes' ? kubernetesDefaults(normalizedDraft.environment) : {}) }])
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
  return <div className="deployment-builder"><aside><div><h3>Environments</h3><button aria-label="Add environment" disabled={!availableEnvironment} title={availableEnvironment ? undefined : 'Dev, Staging and Production are already configured'} onClick={openAddEnvironment}><Plus size={16} /></button></div>{environments.map((environment, index) => <button className={active === index ? 'active' : ''} onClick={() => setActive(index)} key={`${environment.name}-${index}`}><span className={`env-dot env-${environment.environment.toLowerCase()}`} /><span><strong>{environment.name}</strong><small>{environment.target}</small></span><ChevronRight size={16} /></button>)}{!environments.length && <div className="empty-environments"><Globe2 size={24} /><p>No environments yet</p><button onClick={openAddEnvironment}>Add environment</button></div>}</aside><main>{current ? <><div className="environment-title"><div><h2>{current.name}</h2><p>{current.environment} · {current.target} deployment target</p></div><button className="secondary-button" onClick={openEditEnvironment}><Settings2 size={15} />Edit</button></div><TargetConfiguration current={current} updateCurrent={updateCurrent} onSelectServers={openServerPicker} /><section className="deployment-section panel"><div className="section-heading"><div><h3>Deployment tasks</h3><p>Tasks run in the order shown below.</p></div><div><button className="secondary-button"><Upload size={15} />Upload tasks</button><button className="primary-button" onClick={() => setTaskPicker(true)}><Plus size={15} />Add task</button></div></div>{current.tasks.length ? <div className="deployment-task-list">{current.tasks.map((task, index) => <div key={`${task}-${index}`}><span className="drag-handle">⠿</span><span className="stage-number">{index + 1}</span><HeartPulse size={17} /><strong>{task}</strong><button aria-label={`Configure ${task}`} onClick={() => task === 'Health check' && setHealthConfig(true)}><Settings2 size={15} /></button><button aria-label={`Remove ${task}`} onClick={() => updateCurrent({ tasks: current.tasks.filter((_, itemIndex) => index !== itemIndex) })}><X size={15} /></button></div>)}</div> : <div className="inline-empty"><Container size={23} /><span>No deployment tasks configured</span></div>}</section></> : <div className="deployment-empty"><Globe2 size={34} /><h2>Configure deployment environments</h2><p>Add Dev, Staging or Production and define targets, servers and tasks.</p><button className="primary-button" onClick={openAddEnvironment}><Plus size={16} />Add environment</button></div>}</main>
    {addEnv && <Modal title={editingEnvironment === null ? 'Add environment' : 'Edit environment'} description="Create a deployment target for this module." onClose={closeEnvironmentModal} footer={<><button className="secondary-button" onClick={closeEnvironmentModal}>Cancel</button><button className="primary-button" disabled={!environmentDraft.name.trim() || duplicateEnvironment} onClick={saveEnvironment}>{editingEnvironment === null ? 'Add environment' : 'Save changes'}</button></>}><div className="form-grid"><label className="field full"><span>Name</span><input value={environmentDraft.name} onChange={(event) => setEnvironmentDraft({ ...environmentDraft, name: event.target.value })} /></label><label className="field full"><span>Environment</span><select value={environmentDraft.environment} onChange={(event) => setEnvironmentDraft({ ...environmentDraft, environment: event.target.value as DeploymentEnvironment, name: environmentNames[event.target.value as DeploymentEnvironment] })}>{(['Dev', 'Staging', 'Production'] as DeploymentEnvironment[]).map((environment) => <option disabled={environments.some((item, index) => item.environment === environment && index !== editingEnvironment)} key={environment}>{environment}</option>)}</select></label>{duplicateEnvironment && <div className="inline-error full" role="alert">This environment is already configured.</div>}<div className="field full"><span>Deployment target</span><div className="target-options">{deploymentTargets.map(({ label, icon: TargetIcon, execution }) => <button className={environmentDraft.target === label ? 'selected' : ''} disabled={Boolean(lockedTarget && label !== lockedTarget)} title={lockedTarget && label !== lockedTarget ? `Application runtime is already ${lockedTarget}` : undefined} onClick={() => setEnvironmentDraft({ ...environmentDraft, target: label })} key={label}><TargetIcon size={18} /><span><strong>{label}</strong><small>{execution}</small></span></button>)}</div>{lockedTarget && <small className="target-policy-hint">Application runtime is shared by all deployment environments.</small>}</div></div></Modal>}
    {selectServers && <Modal title="Select target servers" description={`DCIM servers assigned to ${current.name}.`} onClose={() => setSelectServers(false)} footer={<><button className="secondary-button" onClick={() => setSelectServers(false)}>Cancel</button><button className="primary-button" disabled={!serverDraft.length} onClick={() => { updateCurrent({ servers: serverDraft }); setSelectServers(false) }}>Add selected servers</button></>}><div className="server-picker">{targetServers.map(({ name, ip }) => <label key={name}><input type="checkbox" checked={serverDraft.includes(name)} onChange={(event) => setServerDraft((items) => event.target.checked ? [...items, name] : items.filter((item) => item !== name))} /><Server size={17} /><span><strong>{name}</strong><small>{ip} · Online</small></span></label>)}</div></Modal>}
    {taskPicker && <Modal wide title="Add deployment task" description="Choose a managed task or build a custom step." onClose={() => setTaskPicker(false)} footer={<button className="secondary-button" onClick={() => setTaskPicker(false)}>Close</button>}><div className="task-categories"><button className="active">All tasks</button><button>Service</button><button>Docker</button><button>Files</button><button>Database</button><button>Verification</button></div><div className="task-grid">{deploymentTasks.map(([name, description], index) => { const icons = [RotateCcw, Container, Container, FileCode2, FileCode2, Database, HeartPulse, FolderPlus]; const TaskIcon = icons[index]; return <button onClick={() => { updateCurrent({ tasks: [...current.tasks, name] }); setTaskPicker(false); if (name === 'Health check') setHealthConfig(true) }} key={name}><TaskIcon size={19} /><span><strong>{name}</strong><small>{description}</small></span><Plus size={15} /></button> })}</div></Modal>}
    {healthConfig && <Modal title="Configure Health check" description="The deployment fails when this script exits non-zero." onClose={() => setHealthConfig(false)} footer={<><button className="secondary-button" onClick={() => setHealthConfig(false)}>Cancel</button><button className="primary-button" onClick={() => setHealthConfig(false)}>Save task</button></>}><label className="field"><span>Health check script</span><textarea className="script-area" defaultValue={'curl -sf http://localhost:$PORT/health || exit 1'} /></label><div className="form-grid"><label className="field"><span>Retries</span><input type="number" defaultValue="3" /></label><label className="field"><span>Delay</span><input defaultValue="10s" /></label></div></Modal>}
  </div>
}

export function NewModuleWizard({ onCancel, onCreate }: { onCancel: () => void; onCreate: (moduleId: string, configuration: ModuleWizardSubmission) => Promise<void> | void }) {
  const { notify } = usePortalFeedback()
  const [step, setStep] = useState(1)
  const [selected, setSelected] = useState('')
  const [portalInformation, setPortalInformation] = useState<PortalInformation>({ displayName: '', moduleType: 'Backend', description: '' })
  const [stages, setStages] = useState(defaultPipelineStages)
  const [deploymentReady, setDeploymentReady] = useState(false)
  const [deploymentConfig, setDeploymentConfig] = useState<DeploymentEnvironmentConfig[]>([])
  const [creating, setCreating] = useState(false)
  const selectModule = (moduleId: string) => {
    const module = dcimModules.find((item) => item.id === moduleId)
    if (!module) return
    setSelected(moduleId)
    setPortalInformation({
      displayName: module.name,
      moduleType: module.type,
      description: `${module.name} delivery module for netChat.`,
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
      await onCreate(selected, {
        ...portalInformation,
        runtime: defaultConfig.runtime,
        defaultEnvironment: defaultConfig.environment,
        deploymentEnvironments: deploymentConfig,
        stages,
      })
      notify('Module đã được tạo từ DCIM và sẵn sàng nhận pipeline run.')
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Không thể tạo module.', 'error')
    } finally {
      setCreating(false)
    }
  }
  return <div className="new-module-page"><div className="wizard-header"><button className="back-button" onClick={onCancel}><ArrowLeft size={16} />Back to netChat</button><div><h1>New Module</h1><p>Add a DCIM module and configure its delivery lifecycle.</p></div><WizardSteps step={step} /></div><section className="wizard-shell">{step === 1 && <GeneralStep selected={selected} onSelect={selectModule} information={portalInformation} onInformationChange={setPortalInformation} />}{step === 2 && <CicdStep stages={stages} onStagesChange={setStages} />}{step === 3 && <DeploymentStep onValidityChange={setDeploymentReady} onConfigurationChange={setDeploymentConfig} />}</section><footer className="wizard-footer"><button className="secondary-button" disabled={creating} onClick={step === 1 ? onCancel : () => setStep(step - 1)}>{step === 1 ? 'Cancel' : 'Back'}</button>{step < 3 ? <button className="primary-button" disabled={step === 1 && (!selected || !portalInformation.displayName.trim() || !portalInformation.moduleType)} onClick={() => setStep(step + 1)}>Next <ArrowRight size={16} /></button> : <button className="primary-button" disabled={!deploymentReady || creating} title={deploymentReady ? undefined : 'Complete an environment and its target connection'} onClick={finish}><Check size={16} />{creating ? 'Creating…' : 'Create Module'}</button>}</footer></div>
}
