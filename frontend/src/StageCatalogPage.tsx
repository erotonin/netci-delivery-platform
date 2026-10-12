import { useState } from 'react'
import { CircleAlert, Hourglass, ListChecks, Lock, Plus, Trash2, X } from 'lucide-react'
import {
  approveCustomStage, getStageCatalog, registerCustomStage, removeCustomStage,
  type CustomStageCreate, type StageCatalog, type StageDefinition, type StageParameterDeclaration,
} from './api/netciClient'
import { AsyncPanel, EmptyState, useAsyncData } from './AsyncState'
import { hasRole, type AuthSession } from './LoginPage'
import { usePortalFeedback } from './PortalFeedback'
import { PageHeader } from './PortalShell'

/**
 * The stage catalog as a page of its own: what any module's pipeline may consist of.
 *
 * Everything here is what GET /stage-catalog returned -- nothing is filled in. The admin
 * controls are a convenience for the people who hold `platform-admin`; the server checks
 * the role, the script path and separation of duties again on every call, and whatever
 * it refuses with is shown as it said it.
 */

// Pipeline order, the backend's CATEGORIES tuple.
const CATEGORY_ORDER: StageDefinition['category'][] = ['source', 'test', 'build', 'security', 'publish', 'deploy', 'verify', 'custom']
const CATEGORY_LABEL: Record<StageDefinition['category'], string> = {
  source: 'Source', test: 'Test', build: 'Build', security: 'Security',
  publish: 'Publish', deploy: 'Deploy', verify: 'Verify', custom: 'Custom',
}
// The built-in stages a custom stage may run after (stage_catalog.ANCHORS): `publish` is
// the last one on the build agent, deploy and health-check run in netCI's worker.
const ANCHORS: CustomStageCreate['afterStage'][] = ['checkout', 'unit-test', 'build', 'sbom', 'vulnerability-scan', 'sign', 'publish']
// stage_catalog.SCRIPT_PATH, as a typing hint only: it never blocks the request, so the
// server's own refusal is what the administrator sees when the two disagree.
const SCRIPT_PATH = /^(?!.*(?:^|\/)\.\.(?:\/|$))[A-Za-z0-9._][A-Za-z0-9._/-]{0,253}\.sh$/

const muted = { color: 'var(--muted)', fontSize: '0.85rem' } as const

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback
}

type Draft = { id: string; name: string; category: StageDefinition['category']; script: string; afterStage: CustomStageCreate['afterStage']; description: string; parameters: Array<Required<StageParameterDeclaration>> }
const emptyDraft: Draft = { id: '', name: '', category: 'custom', script: '', afterStage: 'unit-test', description: '', parameters: [] }

function RegisterStageForm({ onRegistered, onCancel }: { onRegistered: (stage: StageDefinition) => void; onCancel: () => void }) {
  const [draft, setDraft] = useState<Draft>(emptyDraft)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const scriptHint = draft.script !== '' && !SCRIPT_PATH.test(draft.script)
  const setParam = (index: number, field: keyof StageParameterDeclaration, value: string) =>
    setDraft((current) => ({ ...current, parameters: current.parameters.map((p, i) => i === index ? { ...p, [field]: value } : p) }))

  const submit = async () => {
    setSaving(true)
    setError('')
    try {
      const payload: CustomStageCreate = {
        id: draft.id.trim(), name: draft.name.trim(), category: draft.category, script: draft.script.trim(),
        afterStage: draft.afterStage, description: draft.description,
        parameters: draft.parameters.filter((p) => p.name.trim() !== '').map((p) => ({ name: p.name.trim(), default: p.default, description: p.description })),
      }
      onRegistered(await registerCustomStage(payload))
      setDraft(emptyDraft)
    } catch (cause) {
      setError(errorText(cause, 'Could not register the stage'))
    } finally {
      setSaving(false)
    }
  }

  return <section className="panel" aria-label="Register custom stage">
    <div className="section-heading"><div><h3>Register custom stage</h3><p>A custom stage runs one script from the module's own repository (reviewed in git) in the builder container, right after the built-in stage it is anchored to. The portal never accepts a command, only a repository-relative path.</p></div></div>
    <div className="form-grid">
      <label className="field"><span>Stage id</span><input value={draft.id} onChange={(e) => setDraft({ ...draft, id: e.target.value })} placeholder="lint" /></label>
      <label className="field"><span>Name</span><input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="Lint" /></label>
      <label className="field"><span>Script (repository path)</span><input value={draft.script} onChange={(e) => setDraft({ ...draft, script: e.target.value })} placeholder="ci/lint.sh" aria-describedby="stage-script-hint" /></label>
      <label className="field"><span>Runs after</span><select value={draft.afterStage} onChange={(e) => setDraft({ ...draft, afterStage: e.target.value as Draft['afterStage'] })}>{ANCHORS.map((id) => <option key={id} value={id}>{id}</option>)}</select></label>
      <label className="field"><span>Category</span><select value={draft.category} onChange={(e) => setDraft({ ...draft, category: e.target.value as Draft['category'] })}>{CATEGORY_ORDER.map((c) => <option key={c} value={c}>{CATEGORY_LABEL[c]}</option>)}</select></label>
      <label className="field full"><span>Description</span><input value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} /></label>
    </div>
    <p id="stage-script-hint" style={{ ...muted, color: scriptHint ? '#b83240' : muted.color }}>
      {scriptHint ? 'This does not look like a repository-relative path ending in .sh (no leading slash, no ".."); netCI will most likely refuse it.' : 'A repository-relative path ending in .sh, without ".." or a leading slash. netCI checks it again.'}
    </p>
    <div style={{ display: 'grid', gap: 6 }}>
      <strong style={{ fontSize: '0.9rem' }}>Parameters</strong>
      <span style={muted}>Each reaches the script as an environment variable; a module sets its value in its pipeline settings.</span>
      {draft.parameters.map((param, index) => <div key={index} style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 2fr auto', gap: 6, alignItems: 'end' }}>
        <label className="field"><span>Name</span><input value={param.name} onChange={(e) => setParam(index, 'name', e.target.value)} placeholder="LEVEL" aria-label={`Parameter ${index + 1} name`} /></label>
        <label className="field"><span>Default</span><input value={param.default} onChange={(e) => setParam(index, 'default', e.target.value)} aria-label={`Parameter ${index + 1} default`} /></label>
        <label className="field"><span>Description</span><input value={param.description} onChange={(e) => setParam(index, 'description', e.target.value)} aria-label={`Parameter ${index + 1} description`} /></label>
        <button type="button" className="icon-button" aria-label={`Remove parameter ${index + 1}`} onClick={() => setDraft((c) => ({ ...c, parameters: c.parameters.filter((_, i) => i !== index) }))}><X size={15} /></button>
      </div>)}
      {draft.parameters.length < 16 && <div><button type="button" className="secondary-button" onClick={() => setDraft((c) => ({ ...c, parameters: [...c.parameters, { name: '', default: '', description: '' }] }))}><Plus size={14} />Add parameter</button></div>}
    </div>
    {error && <div className="inline-error" role="alert"><CircleAlert size={14} />{error}</div>}
    <p style={muted}>With separation of duties on, a registered stage is proposed and runs nowhere until a different platform administrator approves it.</p>
    <div style={{ display: 'flex', gap: 8 }}>
      <button type="button" className="primary-button" disabled={saving || !draft.id || !draft.name || !draft.script} onClick={submit}>{saving ? 'Registering…' : 'Register stage'}</button>
      <button type="button" className="secondary-button" onClick={onCancel}>Cancel</button>
    </div>
  </section>
}

function StageStatus({ stage }: { stage: StageDefinition }) {
  if (stage.kind !== 'custom') return null
  if (stage.status === 'active') return <em className="type-badge" style={{ background: '#eaf7ef', color: '#23804a' }}>approved{stage.approvedBy ? ` by ${stage.approvedBy}` : ''}</em>
  if (stage.status === 'proposed') return <em className="type-badge" style={{ background: '#fff6e5', color: '#9a5b00', display: 'inline-flex', alignItems: 'center', gap: 4 }}><Hourglass size={11} aria-hidden="true" />Pending approval — not yet usable</em>
  // `rejected`, or anything the API adds later: say what it is and that it does not run.
  return <em className="type-badge" style={{ background: '#fff4f5', color: '#b83240' }}>{stage.status ?? 'unknown state'} — not usable</em>
}

function StageRow({ stage, anchorName, admin, subject, separationOfDuties, onChanged }: {
  stage: StageDefinition; anchorName: string | null; admin: boolean; subject: string; separationOfDuties: boolean; onChanged: (message: string) => void
}) {
  const [busy, setBusy] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [error, setError] = useState('')
  const ownProposal = separationOfDuties && stage.createdBy === subject

  const act = async (run: () => Promise<unknown>, done: string, fallback: string) => {
    setBusy(true)
    setError('')
    try {
      await run()
      onChanged(done)
    } catch (cause) {
      setError(errorText(cause, fallback))
    } finally {
      setBusy(false)
      setConfirmDelete(false)
    }
  }

  return <li className="panel" style={{ display: 'grid', gap: 6, padding: 12 }} aria-label={stage.name}>
    <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8 }}>
      <strong>{stage.name}</strong>
      <code className="mono">{stage.id}</code>
      <em className={`type-badge ${stage.kind === 'builtin' ? 'blue' : 'purple'}`}>{stage.kind === 'builtin' ? 'Built-in' : 'Custom'}</em>
      <StageStatus stage={stage} />
    </div>
    {stage.required && <div style={{ display: 'flex', alignItems: 'center', gap: 6, ...muted }}><Lock size={13} aria-hidden="true" />Required by policy — cannot be disabled</div>}
    {stage.description && <span style={muted}>{stage.description}</span>}
    {stage.kind === 'custom' && <div style={{ display: 'grid', gap: 2, ...muted }}>
      {stage.script && <span>Script: <code className="mono">{stage.script}</code></span>}
      {stage.afterStage && <span>Runs after {anchorName ?? stage.afterStage}</span>}
      {stage.createdBy && <span>Registered by {stage.createdBy}</span>}
    </div>}
    {(stage.parameters ?? []).length > 0 && <div style={{ display: 'grid', gap: 2 }}>
      <span style={muted}>Parameters</span>
      <ul style={{ margin: 0, paddingLeft: 18, ...muted }}>
        {(stage.parameters ?? []).map((param) => <li key={param.name}><code className="mono">{param.name}</code>{param.default ? ` = ${param.default}` : ''}{param.description ? ` — ${param.description}` : ''}</li>)}
      </ul>
    </div>}
    {admin && stage.kind === 'custom' && <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
      {stage.status === 'proposed' && (ownProposal
        ? <span style={muted}>You proposed this stage; a different administrator must approve it.</span>
        : <button type="button" className="primary-button" disabled={busy} aria-label={`Approve ${stage.name}`} onClick={() => act(() => approveCustomStage(stage.id), `${stage.name} approved`, 'Could not approve the stage')}>Approve</button>)}
      {confirmDelete
        ? <>
          <button type="button" className="secondary-button" disabled={busy} aria-label={`Confirm delete ${stage.name}`} onClick={() => act(() => removeCustomStage(stage.id), `${stage.name} removed from the catalog`, 'Could not delete the stage')}><Trash2 size={14} />Confirm delete</button>
          <button type="button" className="link-button" onClick={() => setConfirmDelete(false)}>Keep</button>
        </>
        : <button type="button" className="secondary-button" disabled={busy} aria-label={`Delete ${stage.name}`} title="Refused by netCI while any module uses it" onClick={() => setConfirmDelete(true)}><Trash2 size={14} />Delete</button>}
    </div>}
    {error && <div className="inline-error" role="alert"><CircleAlert size={14} />{error}</div>}
  </li>
}

export function StageCatalogPage({ session }: { session: AuthSession }) {
  const catalog = useAsyncData<StageCatalog>(getStageCatalog)
  const { notify } = usePortalFeedback()
  const [registering, setRegistering] = useState(false)
  const admin = hasRole(session, 'platform-admin')
  const subject = session.identity.principal.subject
  const changed = (message: string) => { notify(message); catalog.reload() }

  return <>
    <PageHeader
      title="Stage Catalog"
      description="Every stage a module's pipeline may run, in pipeline order. Built-in stages come with the shared Jenkins pipeline; custom stages run one repository script after the stage they are anchored to."
      action={admin && !registering ? <button className="primary-button" onClick={() => setRegistering(true)}><Plus size={16} />Register custom stage</button> : undefined}
    />
    {admin && registering && <RegisterStageForm onCancel={() => setRegistering(false)} onRegistered={(stage) => {
      setRegistering(false)
      changed(stage.status === 'proposed' ? `${stage.name} proposed; it runs nowhere until another administrator approves it` : `${stage.name} registered`)
    }} />}
    <AsyncPanel state={catalog} skeletonRows={6} isEmpty={(data) => data.stages.length === 0}
      empty={<EmptyState icon={<ListChecks size={22} />} title="The stage catalog is empty" hint="netCI returned no stages." />}>
      {(data) => {
        const byId = new Map(data.stages.map((stage) => [stage.id, stage]))
        const known = new Set<string>(CATEGORY_ORDER)
        // A category the API returns that this page does not know yet still gets shown,
        // after the known ones, rather than silently dropping its stages.
        const categories = [...CATEGORY_ORDER, ...Array.from(new Set(data.stages.map((s) => s.category))).filter((c) => !known.has(c))]
        return <div style={{ display: 'grid', gap: 18 }}>
          {categories.map((category) => {
            const stages = data.stages.filter((stage) => stage.category === category).sort((a, b) => a.position - b.position)
            if (!stages.length) return null
            return <section key={category} aria-labelledby={`stage-category-${category}`} style={{ display: 'grid', gap: 8 }}>
              <h2 id={`stage-category-${category}`} style={{ fontSize: '1rem', margin: 0 }}>{CATEGORY_LABEL[category as StageDefinition['category']] ?? category}</h2>
              <ul style={{ listStyle: 'none', padding: 0, margin: 0, display: 'grid', gap: 8 }}>
                {stages.map((stage) => <StageRow key={stage.id} stage={stage} admin={admin} subject={subject}
                  separationOfDuties={session.identity.separationOfDuties}
                  anchorName={stage.afterStage ? byId.get(stage.afterStage)?.name ?? null : null} onChanged={changed} />)}
              </ul>
            </section>
          })}
        </div>
      }}
    </AsyncPanel>
  </>
}
