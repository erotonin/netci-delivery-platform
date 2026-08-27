import { useEffect, useMemo, useState } from 'react'
import {
  Activity, ArrowLeft, Check, CheckCircle2, Copy, History, KeyRound, Minus,
  MoreHorizontal, Pencil, Plus, Search, Settings as Cog, SlidersHorizontal,
  Trash2, UserPlus, Users,
} from 'lucide-react'
import { getStageCatalog, type StageDefinition } from './api/netciClient'
import { usePortalFeedback } from './PortalFeedback'
import { Modal } from './PortalShell'
import { auditEvents, dcimModules, modules, type SettingsTab } from './portalData'

const pipelineTabs = ['CI', 'CD Dev', 'CD Staging', 'CD Prod', 'Automation Test']
const roles = ['Owner', 'Maintainer', 'Developer', 'Viewer'] as const
type Role = (typeof roles)[number]

type SettingsModule = { name: string; code: string; type: string; description: string; repository: string }
type AccessUser = { name: string; role: Role; edit: boolean; trigger: boolean; logs: boolean }
type TeamUser = { initials: string; name: string; email: string; role: Role }

const defaultAccessUsers: AccessUser[] = [
  { name: 'Admin', role: 'Owner', edit: true, trigger: true, logs: true },
  { name: 'TrungTT', role: 'Maintainer', edit: true, trigger: true, logs: true },
  { name: 'LinhPT', role: 'Developer', edit: false, trigger: true, logs: true },
  { name: 'HaiNM', role: 'Viewer', edit: false, trigger: false, logs: true },
]

const defaultTeamUsers: TeamUser[] = [
  { initials: 'AD', name: 'Admin', email: 'admin@netchat.io', role: 'Owner' },
  { initials: 'TT', name: 'TrungTT', email: 'trung.tt@netchat.io', role: 'Maintainer' },
  { initials: 'LP', name: 'LinhPT', email: 'linh.pt@netchat.io', role: 'Developer' },
  { initials: 'HN', name: 'HaiNM', email: 'hai.nm@netchat.io', role: 'Viewer' },
]

function previewKey(moduleId: string, section: string) {
  return `netci.preview.settings.${moduleId}.${section}`
}

function readPreview<T>(key: string, fallback: T): T {
  try {
    const value = sessionStorage.getItem(key)
    return value ? JSON.parse(value) as T : fallback
  } catch {
    return fallback
  }
}

function writePreview<T>(key: string, value: T) {
  sessionStorage.setItem(key, JSON.stringify(value))
}

function GeneralSettings({ module, moduleId }: { module: SettingsModule; moduleId: string }) {
  const { notify } = usePortalFeedback()
  const [form, setForm] = useState(() => readPreview(previewKey(moduleId, 'general'), module))
  const [removeModal, setRemoveModal] = useState(false)

  useEffect(() => setForm(readPreview(previewKey(moduleId, 'general'), module)), [moduleId])

  const save = () => {
    writePreview(previewKey(moduleId, 'general'), form)
    notify('Đã lưu General settings trong Windows preview. API cập nhật module sẽ được nối trên Ubuntu.')
  }

  return <>
    <div className="settings-toolbar"><div><h2>General</h2><p>Basic information synced with DCIM where applicable.</p></div></div>
    <section className="panel settings-card">
      <div className="preview-notice">Windows preview · Display name, type and description are stored only in this browser session.</div>
      <div className="form-grid">
        <label className="field full"><span>Display name</span><input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></label>
        <label className="field"><span>Module code</span><input value={module.code} readOnly /></label>
        <label className="field"><span>Module type</span><select value={form.type} onChange={(event) => setForm({ ...form, type: event.target.value })}><option>Backend</option><option>Frontend</option><option>Worker</option><option>Gateway</option></select></label>
        <label className="field full"><span>Description</span><textarea value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} /></label>
        <label className="field full"><span>Git repository</span><input value={module.repository} readOnly /></label>
      </div>
      <div className="settings-save"><button className="primary-button" disabled={!form.name.trim()} onClick={save}><Check size={16} />Save changes</button></div>
    </section>
    <section className="danger-zone"><h3>Danger zone</h3><div><span><strong>Remove module</strong><p>Disconnect the module from Release Portal. Delivery history will be preserved.</p></span><button className="danger-button" onClick={() => setRemoveModal(true)}>Remove module</button></div></section>
    {removeModal && <Modal title="Remove module" description="This destructive operation requires a persistent portal API that is intentionally not emulated in Windows preview." onClose={() => setRemoveModal(false)} footer={<button className="secondary-button" onClick={() => setRemoveModal(false)}>Close</button>}><div className="inline-error" role="status">No data was removed. Implement and verify the detach contract on Ubuntu before enabling this action.</div></Modal>}
  </>
}

function PipelineSettings({ moduleId }: { moduleId: string }) {
  const { notify } = usePortalFeedback()
  const [tab, setTab] = useState('CI')
  const [mode, setMode] = useState<'visual' | 'code'>('visual')
  const [stages, setStages] = useState<string[]>(() => readPreview(previewKey(moduleId, 'pipeline.CI'), ['Checkout', 'Install dependencies', 'Lint', 'Unit test', 'Type check', 'Code analysis', 'Security scan', 'Build', 'Publish artifact']))
  const [catalog, setCatalog] = useState<StageDefinition[]>([])
  const [stagePicker, setStagePicker] = useState(false)
  const [editIndex, setEditIndex] = useState<number | null>(null)
  const [editValue, setEditValue] = useState('')
  const [branch, setBranch] = useState('main, merge_requests')
  const [runner, setRunner] = useState('Runner 01 · docker-linux')

  useEffect(() => {
    getStageCatalog().then((result) => setCatalog(result.stages)).catch(() => setCatalog([]))
  }, [])

  useEffect(() => {
    const defaults = tab === 'CI'
      ? ['Checkout', 'Install dependencies', 'Lint', 'Unit test', 'Type check', 'Code analysis', 'Security scan', 'Build', 'Publish artifact']
      : ['Checkout', 'Download artifact', 'Deploy', 'Health check']
    setStages(readPreview(previewKey(moduleId, `pipeline.${tab}`), defaults))
    setBranch(tab === 'CI' ? 'main, merge_requests' : tab.includes('Prod') ? 'tags/v*' : 'develop')
  }, [moduleId, tab])

  const yaml = useMemo(() => [
    'pipeline:',
    `  name: ${tab.toLowerCase().replace(/ /g, '-')}`,
    `  runner: ${runner.includes('02') ? 'on-prem' : 'docker-linux'}`,
    '  branches:',
    `    - ${branch}`,
    '  stages:',
    ...stages.map((stage) => `    - ${stage.toLowerCase().replace(/\s+/g, '-')}`),
  ].join('\n'), [branch, runner, stages, tab])

  const save = () => {
    writePreview(previewKey(moduleId, `pipeline.${tab}`), stages)
    notify(`Đã lưu ${tab} pipeline trong Windows preview.`)
  }

  const copyYaml = async () => {
    try {
      await navigator.clipboard.writeText(yaml)
      notify('Đã sao chép pipeline.yml.')
    } catch {
      notify('Trình duyệt không cho phép truy cập clipboard.', 'error')
    }
  }

  const addStage = (stage: string) => {
    if (!stages.includes(stage)) setStages([...stages, stage])
    setStagePicker(false)
  }

  return <>
    <div className="settings-pipeline-tabs">{pipelineTabs.map((item) => <button className={tab === item ? 'active' : ''} onClick={() => setTab(item)} key={item}>{item}</button>)}</div>
    <div className="settings-toolbar"><div><h2>{tab} pipeline</h2><p>Configure branches, runner and delivery stages.</p></div><div className="segmented compact"><button className={mode === 'visual' ? 'active' : ''} onClick={() => setMode('visual')}>Visual</button><button className={mode === 'code' ? 'active' : ''} onClick={() => setMode('code')}>As code</button></div></div>
    {mode === 'visual' ? <>
      <div className="preview-notice">Windows preview · Pipeline edits are stored only in this browser session.</div>
      <div className="form-grid settings-form"><label className="field"><span>Source branch</span><input value={branch} onChange={(event) => setBranch(event.target.value)} /></label><label className="field"><span>Runner</span><select value={runner} onChange={(event) => setRunner(event.target.value)}><option>Runner 01 · docker-linux</option><option>Runner 02 · on-prem</option></select></label></div>
      <div className="stage-list"><div className="section-heading"><h3>Stages</h3><button className="secondary-button" onClick={() => setStagePicker(true)}><Plus size={15} />Add stage</button></div>{stages.map((stage, index) => <div key={`${stage}-${index}`}><span className="drag-handle">⠿</span><span className="stage-number">{index + 1}</span><strong>{stage}</strong><small>{catalog.find((item) => item.name.toLowerCase() === stage.toLowerCase())?.category ?? 'custom'}</small><button aria-label={`Sửa ${stage}`} onClick={() => { setEditIndex(index); setEditValue(stage) }}><Pencil size={15} /></button><button aria-label={`Xóa ${stage}`} onClick={() => setStages(stages.filter((_, itemIndex) => itemIndex !== index))}><Trash2 size={15} /></button></div>)}</div>
      <div className="settings-save"><button className="primary-button" disabled={!stages.length || !branch.trim()} onClick={save}><Check size={16} />Save pipeline</button></div>
    </> : <div className="code-editor"><div><span>pipeline.yml</span><button onClick={copyYaml}><Copy size={15} />Copy</button></div><pre>{yaml}</pre></div>}
    {stagePicker && <Modal title="Add pipeline stage" description="Choose a stage from the netCI stage catalog." onClose={() => setStagePicker(false)} footer={<button className="secondary-button" onClick={() => setStagePicker(false)}>Cancel</button>}><div className="task-grid">{catalog.filter((item) => !stages.includes(item.name)).map((item) => <button key={item.id} onClick={() => addStage(item.name)}><Plus size={16} /><span><strong>{item.name}</strong><small>{item.category}</small></span></button>)}</div>{!catalog.length && <div className="empty-table"><strong>Stage catalog unavailable</strong><span>Start the local API and try again.</span></div>}</Modal>}
    {editIndex !== null && <Modal title="Edit stage label" description="This label is stored in Windows preview only." onClose={() => setEditIndex(null)} footer={<><button className="secondary-button" onClick={() => setEditIndex(null)}>Cancel</button><button className="primary-button" disabled={!editValue.trim()} onClick={() => { setStages(stages.map((stage, index) => index === editIndex ? editValue.trim() : stage)); setEditIndex(null) }}>Save label</button></>}><label className="field"><span>Stage label</span><input value={editValue} onChange={(event) => setEditValue(event.target.value)} autoFocus /></label></Modal>}
  </>
}

function AccessSettings({ moduleId }: { moduleId: string }) {
  const { notify } = usePortalFeedback()
  const [tab, setTab] = useState('CI')
  const [grant, setGrant] = useState(false)
  const [users, setUsers] = useState<AccessUser[]>(() => readPreview(previewKey(moduleId, 'access.CI'), defaultAccessUsers))
  const [name, setName] = useState('')
  const [role, setRole] = useState<Role>('Developer')

  useEffect(() => setUsers(readPreview(previewKey(moduleId, `access.${tab}`), defaultAccessUsers)), [moduleId, tab])

  const persist = (next: AccessUser[]) => {
    setUsers(next)
    writePreview(previewKey(moduleId, `access.${tab}`), next)
  }

  const add = () => {
    const trimmed = name.trim()
    if (!trimmed) return
    const permissions = role === 'Owner' || role === 'Maintainer'
      ? { edit: true, trigger: true, logs: true }
      : role === 'Developer' ? { edit: false, trigger: true, logs: true } : { edit: false, trigger: false, logs: true }
    persist([...users.filter((user) => user.name.toLowerCase() !== trimmed.toLowerCase()), { name: trimmed, role, ...permissions }])
    setGrant(false)
    setName('')
    notify(`Đã cấp ${role} access cho ${trimmed} trong Windows preview.`)
  }

  const togglePermission = (nameToUpdate: string, permission: 'edit' | 'trigger' | 'logs') => persist(users.map((user) => user.name === nameToUpdate ? { ...user, [permission]: !user[permission] } : user))

  return <>
    <div className="settings-pipeline-tabs">{pipelineTabs.map((item) => <button className={tab === item ? 'active' : ''} onClick={() => setTab(item)} key={item}>{item}</button>)}</div>
    <div className="settings-toolbar"><div><h2>{tab} access</h2><p>Control who can edit, trigger and view pipeline logs.</p></div><button className="primary-button" onClick={() => setGrant(true)}><UserPlus size={16} />Grant access</button></div>
    <div className="preview-notice">Windows preview · Access changes are local until the identity/authorization API is connected.</div>
    <section className="panel table-panel"><div className="data-table access-table"><div className="table-row table-head"><span>User</span><span>Role</span><span>Edit</span><span>Trigger</span><span>View logs</span><span /></div>{users.map((user) => <div className="table-row" key={user.name}><span className="user-cell"><i className="avatar">{user.name.slice(0, 2).toUpperCase()}</i><strong>{user.name}</strong></span><span>{user.role}</span>{(['edit', 'trigger', 'logs'] as const).map((permission) => <button className={user[permission] ? 'permission-yes permission-button' : 'permission-no permission-button'} aria-label={`${permission} permission for ${user.name}`} onClick={() => togglePermission(user.name, permission)} key={permission}>{user[permission] ? <CheckCircle2 size={17} /> : <Minus size={17} />}</button>)}<button className="row-more" aria-label={`Remove ${user.name}`} disabled={user.role === 'Owner'} title={user.role === 'Owner' ? 'The owner cannot be removed' : 'Remove access'} onClick={() => { persist(users.filter((item) => item.name !== user.name)); notify(`Đã gỡ access của ${user.name} khỏi Windows preview.`, 'info') }}><MoreHorizontal size={17} /></button></div>)}</div></section>
    {grant && <Modal title="Grant pipeline access" description={`Add a user to ${tab} pipeline.`} onClose={() => setGrant(false)} footer={<><button className="secondary-button" onClick={() => setGrant(false)}>Cancel</button><button className="primary-button" disabled={!name.trim()} onClick={add}>Grant access</button></>}><label className="field"><span>Search people</span><div className="input-with-icon"><Search size={16} /><input value={name} onChange={(event) => setName(event.target.value)} placeholder="Name or email" /></div></label><label className="field"><span>Role</span><select value={role} onChange={(event) => setRole(event.target.value as Role)}>{roles.map((item) => <option key={item}>{item}</option>)}</select></label></Modal>}
  </>
}

function TeamSettings({ moduleId }: { moduleId: string }) {
  const { notify } = usePortalFeedback()
  const [users, setUsers] = useState<TeamUser[]>(() => readPreview(previewKey(moduleId, 'team'), defaultTeamUsers))
  const [addModal, setAddModal] = useState(false)
  const [member, setMember] = useState({ name: '', email: '', role: 'Developer' as Role })

  useEffect(() => setUsers(readPreview(previewKey(moduleId, 'team'), defaultTeamUsers)), [moduleId])

  const persist = (next: TeamUser[]) => {
    setUsers(next)
    writePreview(previewKey(moduleId, 'team'), next)
  }

  const add = () => {
    const name = member.name.trim()
    const email = member.email.trim()
    if (!name || !/^\S+@\S+\.\S+$/.test(email)) return
    persist([...users.filter((user) => user.email.toLowerCase() !== email.toLowerCase()), { ...member, name, email, initials: name.split(/\s+/).map((part) => part[0]).join('').slice(0, 2).toUpperCase() }])
    setMember({ name: '', email: '', role: 'Developer' })
    setAddModal(false)
    notify(`Đã thêm ${name} vào Windows preview team.`)
  }

  return <>
    <div className="settings-toolbar"><div><h2>Team & Access</h2><p>Manage module members and role-based permissions.</p></div><button className="primary-button" onClick={() => setAddModal(true)}><UserPlus size={16} />Add member</button></div>
    <div className="preview-notice">Windows preview · Team changes are local until the identity/authorization API is connected.</div>
    <section className="panel team-list">{users.map((user) => <div key={user.email}><span className="avatar">{user.initials}</span><span><strong>{user.name}</strong><small>{user.email}</small></span><select value={user.role} onChange={(event) => { const next = users.map((item) => item.email === user.email ? { ...item, role: event.target.value as Role } : item); persist(next); notify(`Đã đổi role của ${user.name} trong Windows preview.`) }}>{roles.map((item) => <option key={item}>{item}</option>)}</select><button className="row-more" aria-label={`Remove ${user.name}`} disabled={user.role === 'Owner'} title={user.role === 'Owner' ? 'The owner cannot be removed' : 'Remove member'} onClick={() => { persist(users.filter((item) => item.email !== user.email)); notify(`Đã gỡ ${user.name} khỏi Windows preview team.`, 'info') }}><MoreHorizontal size={17} /></button></div>)}</section>
    <div className="role-guide">{[['Owner', 'Full access, including members and settings.'], ['Maintainer', 'Edit pipelines, trigger runs and manage versions.'], ['Developer', 'Trigger allowed pipelines and view logs.'], ['Viewer', 'Read-only access to module delivery data.']].map(([itemRole, description]) => <div key={itemRole}><strong>{itemRole}</strong><p>{description}</p></div>)}</div>
    {addModal && <Modal title="Add team member" description="Assign a module role in this Windows preview." onClose={() => setAddModal(false)} footer={<><button className="secondary-button" onClick={() => setAddModal(false)}>Cancel</button><button className="primary-button" disabled={!member.name.trim() || !/^\S+@\S+\.\S+$/.test(member.email)} onClick={add}>Add member</button></>}><div className="form-grid"><label className="field full"><span>Name</span><input value={member.name} onChange={(event) => setMember({ ...member, name: event.target.value })} /></label><label className="field full"><span>Email</span><input type="email" value={member.email} onChange={(event) => setMember({ ...member, email: event.target.value })} /></label><label className="field full"><span>Role</span><select value={member.role} onChange={(event) => setMember({ ...member, role: event.target.value as Role })}>{roles.map((item) => <option key={item}>{item}</option>)}</select></label></div></Modal>}
  </>
}

function ActivitySettings({ hasHistory }: { hasHistory: boolean }) {
  const [filter, setFilter] = useState('All actions')
  const visibleEvents = filter === 'All actions' ? auditEvents : auditEvents.filter((event) => `${event.action} ${event.pipeline}`.toLowerCase().includes(filter.toLowerCase()))
  return <><div className="settings-toolbar"><div><h2>Activity Log</h2><p>Auditable changes and pipeline operations for this module.</p></div><select value={filter} onChange={(event) => setFilter(event.target.value)}><option>All actions</option><option>Pipeline</option><option>Access</option><option>Version</option></select></div><section className="panel table-panel"><div className="data-table audit-table"><div className="table-row table-head"><span>Action</span><span>User</span><span>Pipeline</span><span>Detail</span><span>Time</span></div>{hasHistory && visibleEvents.map((event) => <div className="table-row" key={`${event.action}-${event.time}`}><span className="audit-action"><Activity size={15} />{event.action}</span><span>{event.user}</span><span>{event.pipeline}</span><span>{event.detail}</span><span>{event.time}</span></div>)}</div>{(!hasHistory || !visibleEvents.length) && <div className="empty-table"><History size={22} /><strong>{hasHistory ? 'No matching activity' : 'No activity yet'}</strong><span>{hasHistory ? 'Try another action filter.' : 'Module actions will appear here after its first configuration change or pipeline run.'}</span></div>}</section></>
}

export function ModuleSettings({ systemId, moduleId, onClose }: { systemId: string; moduleId: string; onClose: () => void }) {
  const [tab, setTab] = useState<SettingsTab>('general')
  const seed = modules.find((item) => item.id === moduleId)
  const dcim = dcimModules.find((item) => item.id === moduleId)
  const module: SettingsModule = { name: seed?.name ?? dcim?.name ?? moduleId, code: dcim?.code ?? moduleId.toUpperCase().replace(/-/g, '_'), type: seed?.type ?? dcim?.type ?? 'Backend', description: seed?.description ?? `${dcim?.name ?? moduleId} imported from DCIM.`, repository: dcim?.repo ?? '' }
  const items: Array<[SettingsTab, string, typeof Cog]> = [['general', 'General', Cog], ['pipelines', 'Pipelines', SlidersHorizontal], ['pipeline-access', 'Pipeline Access', KeyRound], ['team', 'Team & Access', Users], ['activity', 'Activity Log', History]]
  return <div className="settings-page"><div className="settings-top"><button className="back-button" onClick={onClose}><ArrowLeft size={16} />Back to {module.name}</button><div><h1>Module Settings</h1><p>{module.name} · {systemId}</p></div></div><div className="settings-layout"><aside>{items.map(([id, label, Icon]) => <button className={tab === id ? 'active' : ''} onClick={() => setTab(id)} key={id}><Icon size={17} />{label}</button>)}</aside><main>{tab === 'general' && <GeneralSettings module={module} moduleId={moduleId} />}{tab === 'pipelines' && <PipelineSettings moduleId={moduleId} />}{tab === 'pipeline-access' && <AccessSettings moduleId={moduleId} />}{tab === 'team' && <TeamSettings moduleId={moduleId} />}{tab === 'activity' && <ActivitySettings hasHistory={Boolean(seed)} />}</main></div></div>
}
