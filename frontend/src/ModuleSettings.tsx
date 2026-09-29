import { useEffect, useState } from 'react'
import { Activity, ArrowLeft, Check, History, ListChecks, Settings as Cog } from 'lucide-react'
import { approveCustomStage, deleteModule, getModule, getModuleStages, getStageCatalog, listAuditEvents, registerCustomStage, removeCustomStage, setModuleOwner, setModuleStages, updateModule, whoami, type AuditEvent, type CustomStageCreate, type StageDefinition } from './api/netciClient'
import { usePortalFeedback } from './PortalFeedback'
import { Modal } from './PortalShell'
import type { SettingsTab } from './portalTypes'

type SettingsModule = { name: string; type: string; description: string; ownerTeam?: string | null }

function GeneralSettings({ module, moduleId, onDeleted }: { module: SettingsModule; moduleId: string; onDeleted?: () => void }) {
  const { notify } = usePortalFeedback()
  const [form, setForm] = useState(module)
  const [removeModal, setRemoveModal] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => setForm(module), [moduleId, module])
  const [me, setMe] = useState<{ subject: string; admin: boolean; teams: string[] }>({ subject: '', admin: false, teams: [] })
  useEffect(() => { whoami().then((id) => setMe({ subject: id.principal.subject, admin: id.principal.roles.includes('platform-admin'), teams: id.principal.teams ?? [] })).catch(() => undefined) }, [])
  const [owner, setOwner] = useState(module.ownerTeam ?? '')
  useEffect(() => setOwner(module.ownerTeam ?? ''), [module.ownerTeam])
  const [movingOwner, setMovingOwner] = useState(false)
  const transferOwner = async () => {
    setMovingOwner(true)
    try {
      const updated = await setModuleOwner(moduleId, owner.trim() || null)
      setForm((current) => ({ ...current, ownerTeam: updated.ownerTeam ?? null }))
      notify(`Module ${moduleId} is now owned by team ${updated.ownerTeam ?? '(none)'}.`)
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Failed to transfer owner team.', 'error')
    } finally {
      setMovingOwner(false)
    }
  }

  const save = async () => {
    setSaving(true)
    try {
      await updateModule(moduleId, { displayName: form.name, moduleType: form.type, description: form.description })
      notify('Module configuration saved to netCI.')
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Failed to save module configuration.', 'error')
    } finally {
      setSaving(false)
    }
  }

  const handleRemove = async () => {
    setDeleting(true)
    try {
      await deleteModule(moduleId)
      notify(`Deleted module ${moduleId}.`)
      setRemoveModal(false)
      onDeleted?.()
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Failed to delete module.', 'error')
    } finally {
      setDeleting(false)
    }
  }

  return <>
    <div className="settings-toolbar"><div><h2>General</h2><p>Module information stored persistently in netCI.</p></div></div>
    <section className="panel settings-card">
      <div className="form-grid">
        <label className="field full"><span>Display name</span><input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></label>
        <label className="field"><span>Module ID</span><input value={moduleId} readOnly /></label>
        <label className="field"><span>Module type</span><select value={form.type} onChange={(event) => setForm({ ...form, type: event.target.value })}><option>Backend</option><option>Frontend</option><option>Worker</option><option>Gateway</option></select></label>
        <label className="field full"><span>Description</span><textarea value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} /></label>
      </div>
      <div className="settings-save"><button className="primary-button" disabled={saving || !form.name.trim()} onClick={save}><Check size={16} />{saving ? 'Saving…' : 'Save changes'}</button></div>
    </section>
    <section className="panel settings-card">
      <div className="section-heading"><div><h3>Owning Team</h3><p>Only members of this team can view and edit the module; prod configuration changes require approval from another team member.</p></div></div>
      <div className="form-grid">
        <label className="field full"><span>Owning team</span>{me.admin
          ? <><input list="owner-team-options" value={owner} onChange={(event) => setOwner(event.target.value)} placeholder="group name in identity provider" /><datalist id="owner-team-options">{me.teams.map((team) => <option key={team} value={team} />)}</datalist></>
          : <input value={form.ownerTeam ?? '(unassigned)'} readOnly />}</label>
      </div>
      {me.admin && <div className="settings-save"><button className="secondary-button" disabled={movingOwner || (owner.trim() || null) === (form.ownerTeam ?? null)} onClick={transferOwner}>{movingOwner ? 'Transferring…' : 'Transfer owner team'}</button></div>}
    </section>
    <section className="danger-zone"><h3>Danger zone</h3><div><span><strong>Remove module</strong><p>The module will be removed from Release Portal; delivery history is preserved.</p></span><button className="danger-button" onClick={() => setRemoveModal(true)}>Remove module</button></div></section>
    {removeModal && <Modal title="Remove module" description={`Are you sure you want to remove ${moduleId}?`} onClose={() => setRemoveModal(false)} footer={<><button className="secondary-button" onClick={() => setRemoveModal(false)}>Cancel</button><button className="danger-button" disabled={deleting} onClick={handleRemove}>{deleting ? 'Removing…' : 'Confirm Remove'}</button></>}><div className="inline-error" role="status">Module will no longer accept new delivery commands from the portal.</div></Modal>}
  </>
}

/**
 * The module's pipeline, chosen from the stage catalog. Built-in stages keep the
 * template's order and the required ones cannot be unticked; custom stages (registered
 * by a platform administrator, each a script in the repository) slot in after their
 * anchor. Nothing here writes a Jenkinsfile: netCI hands the list to the shared pipeline.
 */
export function PipelineStagesSettings({ moduleId }: { moduleId: string }) {
  const { notify } = usePortalFeedback()
  const [catalog, setCatalog] = useState<StageDefinition[]>([])
  const [templateStages, setTemplateStages] = useState<string[]>([])
  const [chosen, setChosen] = useState<string[]>([])
  const [saved, setSaved] = useState<string[]>([])
  const [values, setValues] = useState<Record<string, Record<string, string>>>({})
  const [savedValues, setSavedValues] = useState<Record<string, Record<string, string>>>({})
  const [me, setMe] = useState<string>('')
  const [isAdmin, setIsAdmin] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [draft, setDraft] = useState<CustomStageCreate>({ id: '', name: '', script: '', afterStage: 'unit-test', description: '', parameters: [] })
  const [draftParams, setDraftParams] = useState('')

  const load = () => {
    setLoading(true)
    Promise.all([getStageCatalog(), getModuleStages(moduleId), whoami().catch(() => null)])
      .then(([cat, mine, me]) => {
        setCatalog(cat.stages)
        const template = cat.templates.find((t) => t.id === mine.pipelineTemplate)
        setTemplateStages(template ? template.stageIds : cat.stages.filter((s) => s.kind === 'builtin').map((s) => s.id))
        setChosen(mine.stages)
        setSaved(mine.stages)
        setValues(mine.stageParameters ?? {})
        setSavedValues(mine.stageParameters ?? {})
        setMe(me?.principal.subject ?? '')
        setIsAdmin(Boolean(me?.principal.roles.includes('platform-admin')))
      })
      .catch((error) => notify(error instanceof Error ? error.message : 'Could not load the stage catalog', 'error'))
      .finally(() => setLoading(false))
  }
  useEffect(load, [moduleId]) // eslint-disable-line react-hooks/exhaustive-deps

  const byId = Object.fromEntries(catalog.map((s) => [s.id, s]))
  const builtins = templateStages.map((id) => byId[id]).filter(Boolean)
  const customs = catalog.filter((s) => s.kind === 'custom')
  const dirty = JSON.stringify(chosen) !== JSON.stringify(saved) || JSON.stringify(values) !== JSON.stringify(savedValues)
  const setValue = (stageId: string, name: string, value: string) => setValues((cur) => ({ ...cur, [stageId]: { ...(cur[stageId] ?? {}), [name]: value } }))

  const toggle = (stage: StageDefinition) => {
    if (stage.required) return
    setChosen((current) => {
      if (current.includes(stage.id)) {
        // Dropping an anchor drops the custom stages hanging off it, visibly, rather
        // than letting the server refuse the save with STAGE_ANCHOR_DISABLED.
        return current.filter((id) => id !== stage.id && byId[id]?.afterStage !== stage.id)
      }
      return [...current, stage.id]
    })
  }

  const save = async () => {
    setSaving(true)
    try {
      // Only values for stages in the pipeline are sent; the server refuses the rest.
      const sent = Object.fromEntries(Object.entries(values).filter(([id]) => chosen.includes(id)))
      const result = await setModuleStages(moduleId, chosen, sent)
      setChosen(result.stages)
      setSaved(result.stages)
      setValues(result.stageParameters ?? {})
      setSavedValues(result.stageParameters ?? {})
      notify('Pipeline stages saved; the next run uses them')
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Could not save the stages', 'error')
    } finally {
      setSaving(false)
    }
  }

  const register = async () => {
    try {
      // "NAME=default:description" per line; the server validates names and values.
      const parameters = draftParams.split('\n').map((l) => l.trim()).filter(Boolean).map((line) => {
        const [head, ...desc] = line.split(':')
        const [name, def = ''] = head.split('=')
        return { name: name.trim(), default: def.trim(), description: desc.join(':').trim() }
      })
      const created = await registerCustomStage({ ...draft, description: draft.description || undefined, parameters })
      notify(created.status === 'proposed' ? `Stage ${draft.id} proposed; a second administrator must approve it` : `Stage ${draft.id} registered`)
      setDraft({ id: '', name: '', script: '', afterStage: 'unit-test', description: '', parameters: [] })
      setDraftParams('')
      load()
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Could not register the stage', 'error')
    }
  }

  const approve = async (stage: StageDefinition) => {
    try {
      await approveCustomStage(stage.id)
      notify(`Stage ${stage.id} approved`)
      load()
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Could not approve the stage', 'error')
    }
  }

  const remove = async (stage: StageDefinition) => {
    try {
      await removeCustomStage(stage.id)
      notify(`Stage ${stage.id} removed from the catalog`)
      load()
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Could not remove the stage', 'error')
    }
  }

  if (loading) return <section className="panel"><p>Loading the stage catalog…</p></section>

  // Preview the order the server will produce: template order, custom stages after their anchor.
  const preview = templateStages.flatMap((id) => chosen.includes(id) ? [id, ...chosen.filter((c) => byId[c]?.afterStage === id)] : [])

  return <>
    <section className="panel" id="pipeline-stages">
      <div className="section-heading"><div><h3>Pipeline stages</h3><p>Chosen from the catalog; the shared Jenkins pipeline runs exactly this list. Required stages are what make an artifact deployable and cannot be removed.</p></div></div>
      <ul className="stage-list" style={{ listStyle: 'none', padding: 0, margin: 0, display: 'grid', gap: 6 }}>
        {builtins.map((stage) => <li key={stage.id} style={{ display: 'grid', gap: 4 }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <input type="checkbox" checked={chosen.includes(stage.id)} disabled={stage.required} onChange={() => toggle(stage)} aria-label={stage.name} />
            <strong>{stage.name}</strong>
            <code className="mono">{stage.id}</code>
            {stage.required && <span className="badge" title="Required by netCI's supply-chain policy">required</span>}
            <span style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>{stage.description}</span>
          </label>
          {customs.filter((c) => c.afterStage === stage.id).map((custom) => <div key={custom.id} style={{ marginLeft: 28, display: 'grid', gap: 4 }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <input type="checkbox" checked={chosen.includes(custom.id)} disabled={!chosen.includes(stage.id) || custom.status !== 'active'} onChange={() => setChosen((cur) => cur.includes(custom.id) ? cur.filter((id) => id !== custom.id) : [...cur, custom.id])} aria-label={custom.name} />
              <span>↳ {custom.name}</span>
              <code className="mono">{custom.script}</code>
              <span className="badge">{custom.status === 'active' ? 'custom' : `custom · ${custom.status}`}</span>
              {custom.status === 'proposed' && <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>proposed by {custom.createdBy ?? '?'}; needs a second administrator</span>}
              {isAdmin && custom.status === 'proposed' && custom.createdBy !== me && <button className="primary-button" onClick={() => approve(custom)} aria-label={`Approve ${custom.name}`}>Approve</button>}
              {isAdmin && <button className="secondary-button" onClick={() => remove(custom)} title="Remove from the catalog (refused while any module uses it)">Remove</button>}
            </label>
            {chosen.includes(custom.id) && (custom.parameters ?? []).length > 0 && <div style={{ marginLeft: 28, display: 'grid', gap: 4 }}>
              {(custom.parameters ?? []).map((param) => <label key={param.name} className="field" style={{ maxWidth: 420 }}>
                <span><code className="mono">{param.name}</code>{param.description ? ` — ${param.description}` : ''}</span>
                <input value={values[custom.id]?.[param.name] ?? param.default ?? ''} onChange={(e) => setValue(custom.id, param.name, e.target.value)} aria-label={`${custom.id} ${param.name}`} />
              </label>)}
            </div>}
          </div>)}
        </li>)}
      </ul>
      <div style={{ marginTop: 12, fontSize: '0.85rem', color: 'var(--text-muted)' }}>Next run will execute: <code className="mono">{preview.join(' → ')}</code></div>
      <div style={{ marginTop: 12, display: 'flex', gap: 8 }}>
        <button className="primary-button" id="btn-save-stages" disabled={!dirty || saving} onClick={save}><Check size={15} />{saving ? 'Saving…' : 'Save stages'}</button>
        {dirty && <button className="secondary-button" onClick={() => setChosen(saved)}>Discard</button>}
      </div>
    </section>
    {isAdmin && <section className="panel" id="stage-catalog-admin">
      <div className="section-heading"><div><h3>Register a custom stage</h3><p>Platform administrators only. A custom stage runs one script that lives in the module's repository (reviewed in git), in the builder container, after the built-in stage it is anchored to. The portal never accepts a command.</p></div></div>
      <div className="form-grid">
        <label className="field"><span>Stage id</span><input value={draft.id} onChange={(e) => setDraft({ ...draft, id: e.target.value })} placeholder="lint" /></label>
        <label className="field"><span>Name</span><input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="Lint" /></label>
        <label className="field"><span>Script (repository path)</span><input value={draft.script} onChange={(e) => setDraft({ ...draft, script: e.target.value })} placeholder="ci/lint.sh" /></label>
        <label className="field"><span>Runs after</span><select value={draft.afterStage} onChange={(e) => setDraft({ ...draft, afterStage: e.target.value as CustomStageCreate['afterStage'] })}>{['checkout', 'unit-test', 'build', 'sbom', 'vulnerability-scan', 'sign', 'publish'].map((id) => <option key={id} value={id}>{id}</option>)}</select></label>
        <label className="field full"><span>Description</span><input value={draft.description ?? ''} onChange={(e) => setDraft({ ...draft, description: e.target.value })} /></label>
        <label className="field full"><span>Parameters (one per line: <code className="mono">NAME=default:description</code>; reach the script as environment variables)</span><textarea rows={3} value={draftParams} onChange={(e) => setDraftParams(e.target.value)} placeholder={'LEVEL=basic:lint strictness'} /></label>
      </div>
      <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>A proposed stage runs nowhere until a different platform administrator approves it.</p>
      <button className="primary-button" id="btn-register-stage" disabled={!draft.id || !draft.name || !draft.script} onClick={register}>Register stage</button>
    </section>}
  </>
}

function ActivitySettings({ moduleId }: { moduleId: string }) {
  const [filter, setFilter] = useState('All actions')
  const [auditEvents, setAuditEvents] = useState<Array<{ id: string; action: string; user: string; pipeline: string; detail: string; time: string }>>([])
  const [loadError, setLoadError] = useState('')

  useEffect(() => {
    setLoadError('')
    listAuditEvents(moduleId).then((events: AuditEvent[]) => setAuditEvents(events.map((event) => ({
      id: event.id,
      action: event.action,
      user: event.actor,
      pipeline: event.pipelineRunId ?? '—',
      detail: event.correlationId ?? '',
      time: new Date(event.createdAt).toLocaleString('en-US'),
    })))).catch((error) => {
      setAuditEvents([])
      setLoadError(error instanceof Error ? error.message : 'Failed to load audit log.')
    })
  }, [moduleId])

  const visibleEvents = filter === 'All actions' ? auditEvents : auditEvents.filter((event) => `${event.action} ${event.pipeline}`.toLowerCase().includes(filter.toLowerCase()))
  const hasHistory = auditEvents.length > 0
  return <><div className="settings-toolbar"><div><h2>Activity Log</h2><p>Persistent audit trail, retrieved directly from the backend.</p></div><select value={filter} onChange={(event) => setFilter(event.target.value)}><option>All actions</option><option>Pipeline</option><option>Access</option><option>Version</option></select></div>{loadError && <div className="inline-error" role="alert">{loadError}</div>}<section className="panel table-panel"><div className="data-table audit-table"><div className="table-row table-head"><span>Action</span><span>User</span><span>Pipeline</span><span>Detail</span><span>Time</span></div>{visibleEvents.map((event) => <div className="table-row" key={event.id}><span className="audit-action"><Activity size={15} />{event.action}</span><span>{event.user}</span><span>{event.pipeline}</span><span>{event.detail}</span><span>{event.time}</span></div>)}</div>{!loadError && !visibleEvents.length && <div className="empty-table"><History size={22} /><strong>{hasHistory ? 'No matching activity' : 'No activity yet'}</strong><span>{hasHistory ? 'Try another action filter.' : 'Audit events will appear after the first delivery command.'}</span></div>}</section></>
}

export function ModuleSettings({ systemId, moduleId, onClose, onDeleted }: { systemId: string; moduleId: string; onClose: () => void; onDeleted?: () => void }) {
  const [tab, setTab] = useState<SettingsTab>('general')
  const [module, setModule] = useState<SettingsModule>({ name: moduleId, type: 'Module', description: '' })

  useEffect(() => {
    getModule(moduleId).then((item) => {
      setModule({ name: item.name, type: item.type, description: item.description, ownerTeam: item.ownerTeam ?? null })
    }).catch(() => undefined)
  }, [moduleId])

  const items: Array<[SettingsTab, string, typeof Cog]> = [['general', 'General', Cog], ['pipeline', 'Pipeline stages', ListChecks], ['activity', 'Activity Log', History]]
  return <div className="settings-page"><div className="settings-top"><button className="back-button" onClick={onClose}><ArrowLeft size={16} />Back to {module.name}</button><div><h1>Module Settings</h1><p>{module.name} · {systemId}</p></div></div><div className="settings-layout"><aside>{items.map(([id, label, Icon]) => <button className={tab === id ? 'active' : ''} onClick={() => setTab(id)} key={id}><Icon size={17} />{label}</button>)}</aside><main>{tab === 'general' && <GeneralSettings module={module} moduleId={moduleId} onDeleted={onDeleted} />}{tab === 'pipeline' && <PipelineStagesSettings moduleId={moduleId} />}{tab === 'activity' && <ActivitySettings moduleId={moduleId} />}</main></div></div>
}
