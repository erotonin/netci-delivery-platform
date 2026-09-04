import { useEffect, useState } from 'react'
import { Activity, ArrowLeft, Check, History, Settings as Cog } from 'lucide-react'
import { deleteModule, getModule, listAuditEvents, updateModule, type AuditEvent } from './api/netciClient'
import { usePortalFeedback } from './PortalFeedback'
import { Modal } from './PortalShell'
import type { SettingsTab } from './portalTypes'

type SettingsModule = { name: string; type: string; description: string }

function GeneralSettings({ module, moduleId, onDeleted }: { module: SettingsModule; moduleId: string; onDeleted?: () => void }) {
  const { notify } = usePortalFeedback()
  const [form, setForm] = useState(module)
  const [removeModal, setRemoveModal] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => setForm(module), [moduleId, module])

  const save = async () => {
    setSaving(true)
    try {
      await updateModule(moduleId, { displayName: form.name, moduleType: form.type, description: form.description })
      notify('Đã lưu cấu hình module vào netCI.')
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Không thể lưu cấu hình module.', 'error')
    } finally {
      setSaving(false)
    }
  }

  const handleRemove = async () => {
    setDeleting(true)
    try {
      await deleteModule(moduleId)
      notify(`Đã xóa module ${moduleId}.`)
      setRemoveModal(false)
      onDeleted?.()
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Không thể xóa module.', 'error')
    } finally {
      setDeleting(false)
    }
  }

  return <>
    <div className="settings-toolbar"><div><h2>General</h2><p>Thông tin module được lưu bền vững trong netCI.</p></div></div>
    <section className="panel settings-card">
      <div className="form-grid">
        <label className="field full"><span>Display name</span><input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></label>
        <label className="field"><span>Module ID</span><input value={moduleId} readOnly /></label>
        <label className="field"><span>Module type</span><select value={form.type} onChange={(event) => setForm({ ...form, type: event.target.value })}><option>Backend</option><option>Frontend</option><option>Worker</option><option>Gateway</option></select></label>
        <label className="field full"><span>Description</span><textarea value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} /></label>
      </div>
      <div className="settings-save"><button className="primary-button" disabled={saving || !form.name.trim()} onClick={save}><Check size={16} />{saving ? 'Saving…' : 'Save changes'}</button></div>
    </section>
    <section className="danger-zone"><h3>Danger zone</h3><div><span><strong>Remove module</strong><p>Module sẽ bị gỡ khỏi Release Portal; lịch sử delivery vẫn được giữ lại.</p></span><button className="danger-button" onClick={() => setRemoveModal(true)}>Remove module</button></div></section>
    {removeModal && <Modal title="Remove module" description={`Are you sure you want to remove ${moduleId}?`} onClose={() => setRemoveModal(false)} footer={<><button className="secondary-button" onClick={() => setRemoveModal(false)}>Cancel</button><button className="danger-button" disabled={deleting} onClick={handleRemove}>{deleting ? 'Removing…' : 'Confirm Remove'}</button></>}><div className="inline-error" role="status">Module sẽ không còn nhận lệnh delivery mới từ portal.</div></Modal>}
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
      time: new Date(event.createdAt).toLocaleString('vi-VN'),
    })))).catch((error) => {
      setAuditEvents([])
      setLoadError(error instanceof Error ? error.message : 'Không thể tải audit log.')
    })
  }, [moduleId])

  const visibleEvents = filter === 'All actions' ? auditEvents : auditEvents.filter((event) => `${event.action} ${event.pipeline}`.toLowerCase().includes(filter.toLowerCase()))
  const hasHistory = auditEvents.length > 0
  return <><div className="settings-toolbar"><div><h2>Activity Log</h2><p>Audit trail bền vững, lấy trực tiếp từ backend.</p></div><select value={filter} onChange={(event) => setFilter(event.target.value)}><option>All actions</option><option>Pipeline</option><option>Access</option><option>Version</option></select></div>{loadError && <div className="inline-error" role="alert">{loadError}</div>}<section className="panel table-panel"><div className="data-table audit-table"><div className="table-row table-head"><span>Action</span><span>User</span><span>Pipeline</span><span>Detail</span><span>Time</span></div>{visibleEvents.map((event) => <div className="table-row" key={event.id}><span className="audit-action"><Activity size={15} />{event.action}</span><span>{event.user}</span><span>{event.pipeline}</span><span>{event.detail}</span><span>{event.time}</span></div>)}</div>{!loadError && !visibleEvents.length && <div className="empty-table"><History size={22} /><strong>{hasHistory ? 'No matching activity' : 'No activity yet'}</strong><span>{hasHistory ? 'Try another action filter.' : 'Audit events sẽ xuất hiện sau lệnh delivery đầu tiên.'}</span></div>}</section></>
}

export function ModuleSettings({ systemId, moduleId, onClose, onDeleted }: { systemId: string; moduleId: string; onClose: () => void; onDeleted?: () => void }) {
  const [tab, setTab] = useState<SettingsTab>('general')
  const [module, setModule] = useState<SettingsModule>({ name: moduleId, type: 'Module', description: '' })

  useEffect(() => {
    getModule(moduleId).then((item) => {
      setModule({ name: item.name, type: item.type, description: item.description })
    }).catch(() => undefined)
  }, [moduleId])

  const items: Array<[SettingsTab, string, typeof Cog]> = [['general', 'General', Cog], ['activity', 'Activity Log', History]]
  return <div className="settings-page"><div className="settings-top"><button className="back-button" onClick={onClose}><ArrowLeft size={16} />Back to {module.name}</button><div><h1>Module Settings</h1><p>{module.name} · {systemId}</p></div></div><div className="settings-layout"><aside>{items.map(([id, label, Icon]) => <button className={tab === id ? 'active' : ''} onClick={() => setTab(id)} key={id}><Icon size={17} />{label}</button>)}</aside><main>{tab === 'general' && <GeneralSettings module={module} moduleId={moduleId} onDeleted={onDeleted} />}{tab === 'activity' && <ActivitySettings moduleId={moduleId} />}</main></div></div>
}
