import { useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle, BookOpen, Box, CheckCircle2, ChevronRight, Clock,
  Code2, Compass, Cpu, Database, ExternalLink, GitBranch, GitCommit,
  GitFork, Info, Layers, Network, Plus, RefreshCw, Server, Shield,
  ShieldAlert, ShieldCheck, Terminal, Trash2, X, XCircle,
} from 'lucide-react'
import {
  addServiceDependency,
  approveSelfServiceResource,
  createPreviewEnvironment,
  deprovisionSelfServiceResource,
  getServiceDependencies,
  listCatalogServices,
  listPreviewEnvironments,
  listSelfServiceResources,
  registerCatalogService,
  removeServiceDependency,
  requestSelfServiceResource,
  teardownPreviewEnvironment,
  type CatalogService,
  type CatalogServiceCreate,
  type PreviewEnvironment,
  type ResourceRequest,
  type ServiceDependencyGraph,
} from './api/netciClient'
import { Modal, PageHeader, StatusPill } from './PortalShell'
import { usePortalFeedback } from './PortalFeedback'
import type { AuthSession } from './LoginPage'
import type { Navigate } from './PortalShell'
import './catalog.css'

type CatalogTab = 'services' | 'previews' | 'resources'

const INTRO_HIDDEN_STORAGE_KEY = 'netci.catalog.introHidden'

function getInitialIntroHidden(): boolean {
  try {
    return window.localStorage.getItem(INTRO_HIDDEN_STORAGE_KEY) === 'true'
  } catch {
    return false
  }
}

const tierLabels: Record<string, { label: string; tone: string }> = {
  'tier-1': { label: 'Tier 1 · Mission Critical', tone: 'red' },
  'tier-2': { label: 'Tier 2 · Standard Core', tone: 'blue' },
  'tier-3': { label: 'Tier 3 · Supporting / Internal', tone: 'gray' },
}

const lifecycleLabels: Record<string, { label: string; tone: string }> = {
  active: { label: 'Active', tone: 'green' },
  deprecated: { label: 'Deprecated', tone: 'amber' },
  decommissioned: { label: 'Decommissioned', tone: 'gray' },
}

function resourceStatusBadge(status: string): { label: string; className: string } {
  switch (status) {
    case 'provisioned':
      return { label: 'Provisioned', className: 'status-success' }
    case 'pending_approval':
      return { label: 'Pending Approval', className: 'status-pending' }
    case 'provider_not_configured':
      return { label: 'Provider Unconfigured (Fail-closed)', className: 'status-failed' }
    case 'failed':
      return { label: 'Failed', className: 'status-failed' }
    case 'deprovisioned':
      return { label: 'Deprovisioned', className: 'status-gray' }
    default:
      return { label: status, className: 'status-gray' }
  }
}

export function CatalogPage({
  session,
  navigate,
}: {
  session?: AuthSession
  navigate?: Navigate
}) {
  const feedback = usePortalFeedback()
  const [tab, setTab] = useState<CatalogTab>('services')
  const [loading, setLoading] = useState(false)
  const [introHidden, setIntroHidden] = useState<boolean>(getInitialIntroHidden)

  const toggleIntro = () => {
    setIntroHidden((prev) => {
      const next = !prev
      try {
        window.localStorage.setItem(INTRO_HIDDEN_STORAGE_KEY, String(next))
      } catch {
        // Storage might be unavailable
      }
      return next
    })
  }

  // Services state
  const [services, setServices] = useState<CatalogService[]>([])
  const [selectedService, setSelectedService] = useState<CatalogService | null>(null)
  const [dependencyGraph, setDependencyGraph] = useState<ServiceDependencyGraph | null>(null)
  const [loadingGraph, setLoadingGraph] = useState(false)
  const [serviceSearch, setServiceSearch] = useState('')
  const [serviceTierFilter, setServiceTierFilter] = useState('all')

  // Previews state
  const [previews, setPreviews] = useState<PreviewEnvironment[]>([])
  const [previewFilter, setPreviewFilter] = useState('active')

  // Self-service resources state
  const [resources, setResources] = useState<ResourceRequest[]>([])
  const [resourceFilter, setResourceFilter] = useState('all')

  // Modals
  const [showRegisterService, setShowRegisterService] = useState(false)
  const [showAddDependency, setShowAddDependency] = useState(false)
  const [showCreatePreview, setShowCreatePreview] = useState(false)
  const [showRequestResource, setShowRequestResource] = useState(false)

  // Service form
  const [serviceForm, setServiceForm] = useState<CatalogServiceCreate>({
    serviceId: '',
    name: '',
    description: '',
    owningTeam: session?.identity?.principal?.teams?.[0] ?? 'platform-core',
    tier: 'tier-2',
    lifecycle: 'active',
    repoUrl: '',
    docsUrl: '',
  })

  // Dependency form
  const [depTargetId, setDepTargetId] = useState('')
  const [depType, setDepType] = useState('sync')
  const [depDesc, setDepDesc] = useState('')

  // Preview form
  const [previewAppId, setPreviewAppId] = useState('')
  const [previewPr, setPreviewPr] = useState('')
  const [previewSha, setPreviewSha] = useState('')
  const [previewTtl, setPreviewTtl] = useState(86400)

  // Resource form
  const [resAppId, setResAppId] = useState('')
  const [resTeam, setResTeam] = useState(session?.identity?.principal?.teams?.[0] ?? 'platform-core')
  const [resEnv, setResEnv] = useState('preview')
  const [resType, setResType] = useState('postgresql')
  const [resSpec, setResSpec] = useState('{\n  "version": "16",\n  "storageGb": 20\n}')

  // Load active tab data
  const refreshData = async () => {
    setLoading(true)
    try {
      if (tab === 'services') {
        const res = await listCatalogServices()
        setServices(res.items)
      } else if (tab === 'previews') {
        const res = await listPreviewEnvironments()
        setPreviews(res.items)
      } else if (tab === 'resources') {
        const res = await listSelfServiceResources()
        setResources(res.items)
      }
    } catch (err: unknown) {
      feedback.notify(`Failed to load catalog data: ${String(err)}`, 'error')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refreshData()
  }, [tab])

  // Fetch dependency graph when service selected
  useEffect(() => {
    if (!selectedService) {
      setDependencyGraph(null)
      return
    }
    setLoadingGraph(true)
    getServiceDependencies(selectedService.serviceId)
      .then(setDependencyGraph)
      .catch((err) => {
        feedback.notify(`Failed to load dependencies: ${String(err)}`, 'error')
      })
      .finally(() => setLoadingGraph(false))
  }, [selectedService])

  // Handlers
  const handleRegisterService = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      await registerCatalogService(serviceForm)
      feedback.notify(`Service '${serviceForm.serviceId}' registered successfully!`, 'success')
      setShowRegisterService(false)
      setServiceForm({
        serviceId: '',
        name: '',
        description: '',
        owningTeam: session?.identity?.principal?.teams?.[0] ?? 'platform-core',
        tier: 'tier-2',
        lifecycle: 'active',
        repoUrl: '',
        docsUrl: '',
      })
      refreshData()
    } catch (err: unknown) {
      feedback.notify(`Registration failed: ${String(err)}`, 'error')
    }
  }

  const handleAddDependency = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!selectedService || !depTargetId) return
    try {
      await addServiceDependency(selectedService.serviceId, {
        targetServiceId: depTargetId,
        dependencyType: depType,
        description: depDesc,
      })
      feedback.notify(`Dependency on '${depTargetId}' added!`, 'success')
      setShowAddDependency(false)
      setDepTargetId('')
      setDepDesc('')
      // Reload graph
      const graph = await getServiceDependencies(selectedService.serviceId)
      setDependencyGraph(graph)
    } catch (err: unknown) {
      feedback.notify(`Failed to add dependency: ${String(err)}`, 'error')
    }
  }

  const handleRemoveDependency = async (targetId: string) => {
    if (!selectedService) return
    try {
      await removeServiceDependency(selectedService.serviceId, targetId)
      feedback.notify(`Dependency '${targetId}' removed.`, 'info')
      const graph = await getServiceDependencies(selectedService.serviceId)
      setDependencyGraph(graph)
    } catch (err: unknown) {
      feedback.notify(`Failed to remove dependency: ${String(err)}`, 'error')
    }
  }



  const handleCreatePreview = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      await createPreviewEnvironment({
        applicationId: previewAppId,
        pullRequestId: previewPr,
        commitSha: previewSha,
        ttlSeconds: previewTtl,
      })
      feedback.notify(`Preview environment created!`, 'success')
      setShowCreatePreview(false)
      setPreviewPr('')
      setPreviewSha('')
      refreshData()
    } catch (err: unknown) {
      feedback.notify(`Preview creation failed: ${String(err)}`, 'error')
    }
  }

  const handleTeardownPreview = async (previewId: string) => {
    try {
      await teardownPreviewEnvironment(previewId)
      feedback.notify(`Preview '${previewId}' torn down.`, 'info')
      refreshData()
    } catch (err: unknown) {
      feedback.notify(`Teardown failed: ${String(err)}`, 'error')
    }
  }

  const handleRequestResource = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      let parsedSpec = {}
      try {
        parsedSpec = JSON.parse(resSpec)
      } catch {
        feedback.notify('Spec must be valid JSON', 'error')
        return
      }
      await requestSelfServiceResource({
        applicationId: resAppId,
        teamId: resTeam,
        environment: resEnv,
        resourceType: resType,
        spec: parsedSpec,
      })
      feedback.notify(`Resource request submitted!`, 'success')
      setShowRequestResource(false)
      refreshData()
    } catch (err: unknown) {
      feedback.notify(`Resource request failed: ${String(err)}`, 'error')
    }
  }

  const handleApproveResource = async (requestId: string, reqBy: string, env: string) => {
    const currentSubject = session?.identity?.principal?.subject ?? 'anonymous'
    if (['staging', 'prod', 'production'].includes(env.toLowerCase()) && currentSubject === reqBy) {
      feedback.notify('Dual-control violation: You cannot approve your own production/staging resource request.', 'error')
      return
    }
    try {
      await approveSelfServiceResource(requestId, { approvedBy: currentSubject })
      feedback.notify(`Resource request approved!`, 'success')
      refreshData()
    } catch (err: unknown) {
      feedback.notify(`Approval failed: ${String(err)}`, 'error')
    }
  }

  const handleDeprovisionResource = async (requestId: string) => {
    try {
      await deprovisionSelfServiceResource(requestId)
      feedback.notify(`Resource deprovisioned.`, 'info')
      refreshData()
    } catch (err: unknown) {
      feedback.notify(`Deprovision failed: ${String(err)}`, 'error')
    }
  }

  // Filtered views
  const filteredServices = useMemo(() => {
    return services.filter((s) => {
      const matchSearch =
        s.name.toLowerCase().includes(serviceSearch.toLowerCase()) ||
        s.serviceId.toLowerCase().includes(serviceSearch.toLowerCase()) ||
        s.owningTeam.toLowerCase().includes(serviceSearch.toLowerCase())
      const matchTier = serviceTierFilter === 'all' || s.tier === serviceTierFilter
      return matchSearch && matchTier
    })
  }, [services, serviceSearch, serviceTierFilter])

  const filteredPreviews = useMemo(() => {
    if (previewFilter === 'all') return previews
    return previews.filter((p) => p.status === previewFilter)
  }, [previews, previewFilter])

  const filteredResources = useMemo(() => {
    if (resourceFilter === 'all') return resources
    return resources.filter((r) => r.status === resourceFilter)
  }, [resources, resourceFilter])

  return (
    <main className="catalog-page" style={{ padding: '1.5rem', maxWidth: '1440px', margin: '0 auto' }}>
      <PageHeader
        title="Service Catalog & Self-Service Portal"
        description="Discover platform services, ephemeral preview environments, and request cloud infrastructure resources with dual-control governance."
        action={
          <div style={{ display: 'flex', gap: '0.75rem' }}>
            <button className="secondary-button" onClick={refreshData} disabled={loading}>
              <RefreshCw size={15} className={loading ? 'animate-spin' : ''} /> Refresh
            </button>
            {tab === 'services' && (
              <button className="primary-button" onClick={() => setShowRegisterService(true)}>
                <Plus size={16} /> Register Service
              </button>
            )}
            {tab === 'previews' && (
              <button className="primary-button" onClick={() => setShowCreatePreview(true)}>
                <Plus size={16} /> Create Preview
              </button>
            )}
            {tab === 'resources' && (
              <button className="primary-button" data-testid="catalog-header-request-resource" onClick={() => setShowRequestResource(true)}>
                <Plus size={16} /> Request Resource
              </button>
            )}
          </div>
        }
      />

      {/* Collapsible intro panel */}
      <section className="cat-intro-panel" aria-label="Giới thiệu Service Catalog">
        <div className="cat-intro-header">
          <div className="cat-intro-title-wrap">
            <h2 className="cat-intro-title">Giới thiệu Service Catalog</h2>
            {introHidden && (
              <span className="cat-intro-collapsed-hint">
                Danh bạ dịch vụ, môi trường preview và tài nguyên self-service.
              </span>
            )}
          </div>
          <button
            type="button"
            className="cat-intro-toggle"
            onClick={toggleIntro}
            aria-expanded={!introHidden}
          >
            {introHidden ? 'Xem giải thích' : 'Ẩn giải thích'}
          </button>
        </div>
        {!introHidden && (
          <div className="cat-intro-body">
            <p className="cat-intro-lead">
              Service Catalog là danh bạ của mọi service/module: ai sở hữu, mức độ quan trọng (tier), vòng đời, phụ thuộc giữa các service. Nó là nguồn sự thật cho câu hỏi &ldquo;service này của ai, gọi tới ai, có được deploy không&rdquo;.
            </p>
            <div className="cat-cards-grid">
              <article className="cat-card">
                <div className="cat-card-header">
                  <h3 className="cat-card-title">
                    <Compass size={16} /> Services
                  </h3>
                  <p className="cat-card-desc">
                    Danh bạ định danh mọi service/module: quản lý team sở hữu (owner), mức độ quan trọng (tier) và trạng thái vòng đời (lifecycle). Quản lý đồ thị phụ thuộc gọi dịch vụ upstream/downstream và phát hiện chu trình.
                  </p>
                </div>
                <div className="cat-card-demo">
                  <span className="cat-card-demo-label">Demo được gì:</span>
                  <span className="cat-card-demo-text">
                    Xem owner/tier/lifecycle, đồ thị phụ thuộc; đăng ký service mới; thêm/xoá dependency; kiểm tra cảnh báo chu trình phụ thuộc.
                  </span>
                </div>
              </article>

              <article className="cat-card">
                <div className="cat-card-header">
                  <h3 className="cat-card-title">
                    <GitBranch size={16} /> Previews
                  </h3>
                  <p className="cat-card-desc">
                    Môi trường preview tạm thời và cô lập cho một merge request / pull request, phục vụ kiểm thử tính năng trước khi hợp nhất.
                  </p>
                </div>
                <div className="cat-card-demo">
                  <span className="cat-card-demo-label">Demo được gì:</span>
                  <span className="cat-card-demo-text">
                    Tạo môi trường preview theo PR và commit SHA; cấu hình thời gian sống (TTL) để tự huỷ khi hết hạn; kiểm tra URL endpoint và chủ động huỷ sớm.
                  </span>
                </div>
              </article>

              <article className="cat-card">
                <div className="cat-card-header">
                  <h3 className="cat-card-title">
                    <Database size={16} /> Resources
                  </h3>
                  <p className="cat-card-desc">
                    Cổng tự phục vụ yêu cầu tài nguyên đám mây (DB, Redis, S3, IAM role) có kiểm soát phê duyệt kép (dual-control governance) cho staging/production.
                  </p>
                </div>
                <div className="cat-card-demo">
                  <span className="cat-card-demo-label">Demo được gì:</span>
                  <span className="cat-card-demo-text">
                    Gửi yêu cầu tài nguyên qua JSON spec; phê duyệt (Approve); thu hồi (Deprovision); provider chưa cấu hình thì trạng thái fail-closed chứ không giả lập.
                  </span>
                </div>
              </article>
            </div>
          </div>
        )}
      </section>

      {/* Tabs Navigation */}
      <div
        role="tablist"
        className="tabs"
        style={{
          borderBottom: '1px solid var(--border-color, #e2e8f0)',
          marginBottom: '1.5rem',
        }}
      >
        <button
          role="tab"
          aria-selected={tab === 'services'}
          className={tab === 'services' ? 'active' : ''}
          onClick={() => setTab('services')}
        >
          <Compass size={17} /> Services & Dependency Graph
        </button>

        <button
          role="tab"
          aria-selected={tab === 'previews'}
          className={tab === 'previews' ? 'active' : ''}
          onClick={() => setTab('previews')}
        >
          <GitBranch size={17} /> Ephemeral Preview Environments
        </button>

        <button
          role="tab"
          aria-selected={tab === 'resources'}
          className={tab === 'resources' ? 'active' : ''}
          onClick={() => setTab('resources')}
        >
          <Database size={17} /> Self-Service Resources
        </button>
      </div>

      {/* TAB 1: Services & Dependency Graph */}
      {tab === 'services' && (
        <div>
          <div className="cat-tab-hint">
            <Info size={16} className="cat-tab-hint-icon" />
            <span className="cat-tab-hint-text">
              Services: xem owner/tier/lifecycle, đồ thị phụ thuộc, đánh dấu deprecated và kiểm tra chu trình phụ thuộc.
            </span>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: selectedService ? '1fr 1fr' : '1fr', gap: '1.5rem' }}>
          <div>
            <div style={{ display: 'flex', gap: '1rem', marginBottom: '1rem' }}>
              <input
                type="text"
                placeholder="Search services by ID, name, or owning team..."
                value={serviceSearch}
                onChange={(e) => setServiceSearch(e.target.value)}
                style={{ flex: 1, padding: '0.5rem 0.75rem', borderRadius: '6px', border: '1px solid #cbd5e1' }}
              />
              <select
                value={serviceTierFilter}
                onChange={(e) => setServiceTierFilter(e.target.value)}
                style={{ padding: '0.5rem 0.75rem', borderRadius: '6px', border: '1px solid #cbd5e1' }}
              >
                <option value="all">All Tiers</option>
                <option value="tier-1">Tier 1</option>
                <option value="tier-2">Tier 2</option>
                <option value="tier-3">Tier 3</option>
              </select>
            </div>

            {filteredServices.length === 0 ? (
              <div className="empty-state" style={{ textAlign: 'center', padding: '3rem', border: '1px dashed #cbd5e1', borderRadius: '8px' }}>
                <Box size={40} style={{ opacity: 0.4, margin: '0 auto 1rem' }} />
                <h3>No services found in catalog</h3>
                <p>Register your microservices and APIs to establish platform ownership and dependency topology.</p>
                <button className="primary-button" onClick={() => setShowRegisterService(true)} style={{ marginTop: '1rem' }}>
                  Register Service
                </button>
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
                {filteredServices.map((svc) => {
                  const isSelected = selectedService?.serviceId === svc.serviceId
                  const tier = tierLabels[svc.tier] ?? { label: svc.tier, tone: 'gray' }
                  const life = lifecycleLabels[svc.lifecycle] ?? { label: svc.lifecycle, tone: 'gray' }
                  return (
                    <article
                      key={svc.serviceId}
                      onClick={() => setSelectedService(svc)}
                      style={{
                        padding: '1rem',
                        borderRadius: '8px',
                        border: isSelected ? '2px solid #2563eb' : '1px solid #e2e8f0',
                        background: isSelected ? '#f8faff' : '#ffffff',
                        cursor: 'pointer',
                        transition: 'all 0.2s',
                        boxShadow: isSelected ? '0 4px 12px rgba(37, 99, 235, 0.08)' : 'none',
                      }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '0.5rem' }}>
                        <div>
                          <strong style={{ fontSize: '1.1rem', color: '#1e293b' }}>{svc.name}</strong>
                          <span style={{ marginLeft: '0.5rem', fontSize: '0.85rem', color: '#64748b' }}>({svc.serviceId})</span>
                        </div>
                        <div style={{ display: 'flex', gap: '0.5rem' }}>
                          <span className={`status status-${tier.tone}`} style={{ fontSize: '0.75rem' }}>{tier.label}</span>
                          <span className={`status status-${life.tone}`} style={{ fontSize: '0.75rem' }}>{life.label}</span>
                        </div>
                      </div>
                      {svc.description && <p style={{ fontSize: '0.9rem', color: '#475569', marginBottom: '0.75rem' }}>{svc.description}</p>}
                      <div style={{ display: 'flex', gap: '1.5rem', fontSize: '0.8rem', color: '#64748b' }}>
                        <span>Team: <strong>{svc.owningTeam}</strong></span>
                        {svc.repoUrl && (
                          <a href={svc.repoUrl} target="_blank" rel="noopener noreferrer" style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', color: '#2563eb' }}>
                            <Code2 size={13} /> Repository
                          </a>
                        )}
                        {svc.docsUrl && (
                          <a href={svc.docsUrl} target="_blank" rel="noopener noreferrer" style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', color: '#2563eb' }}>
                            <BookOpen size={13} /> Documentation
                          </a>
                        )}
                      </div>
                    </article>
                  )
                })}
              </div>
            )}
          </div>

          {/* Service Detail & Dependency Graph Inspector */}
          {selectedService && (
            <div
              style={{
                background: '#ffffff',
                border: '1px solid #e2e8f0',
                borderRadius: '8px',
                padding: '1.5rem',
                position: 'sticky',
                top: '1rem',
                height: 'fit-content',
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.25rem', borderBottom: '1px solid #f1f5f9', paddingBottom: '0.75rem' }}>
                <div>
                  <h3 style={{ margin: 0, fontSize: '1.25rem', color: '#0f172a' }}>{selectedService.name}</h3>
                  <small style={{ color: '#64748b' }}>ID: {selectedService.serviceId} · Owner: {selectedService.owningTeam}</small>
                </div>
                <button
                  type="button"
                  className="icon-button"
                  aria-label="Close Inspector"
                  onClick={() => setSelectedService(null)}
                >
                  <X size={18} />
                </button>
              </div>

              {/* Cycle Warning Banner */}
              {dependencyGraph?.hasCycle && (
                <div style={{ padding: '0.75rem 1rem', background: '#fef2f2', border: '1px solid #f87171', borderRadius: '6px', marginBottom: '1rem', display: 'flex', alignItems: 'center', gap: '0.5rem', color: '#991b1b' }}>
                  <AlertTriangle size={18} />
                  <div>
                    <strong>Circular Dependency Detected!</strong>
                    <div style={{ fontSize: '0.8rem' }}>Cycles: {dependencyGraph.cycles.map((c) => c.join(' ➔ ')).join(', ')}</div>
                  </div>
                </div>
              )}

              {/* Dependency Controls */}
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
                <h4 style={{ margin: 0, fontSize: '1rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                  <Network size={16} /> Dependency Topology
                </h4>
                <button className="secondary-button" onClick={() => setShowAddDependency(true)} style={{ padding: '0.35rem 0.75rem', fontSize: '0.85rem' }}>
                  <Plus size={14} /> Add Dependency
                </button>
              </div>

              {loadingGraph ? (
                <p style={{ color: '#64748b' }}>Analyzing dependency topology...</p>
              ) : (
                <div>
                  <div style={{ marginBottom: '1.25rem' }}>
                    <strong style={{ fontSize: '0.85rem', color: '#475569', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                      Upstream Dependencies (Calls to other services)
                    </strong>
                    {dependencyGraph?.upstream.length === 0 ? (
                      <p style={{ fontSize: '0.85rem', color: '#94a3b8', fontStyle: 'italic', margin: '0.5rem 0' }}>No upstream dependencies declared</p>
                    ) : (
                      <ul style={{ listStyle: 'none', padding: 0, margin: '0.5rem 0' }}>
                        {dependencyGraph?.edges
                          .filter((e) => e.source === selectedService.serviceId)
                          .map((edge) => (
                            <li
                              key={edge.target}
                              style={{
                                display: 'flex',
                                justifyContent: 'space-between',
                                alignItems: 'center',
                                padding: '0.5rem 0.75rem',
                                background: '#f8fafc',
                                borderRadius: '6px',
                                marginBottom: '0.4rem',
                                fontSize: '0.85rem',
                              }}
                            >
                              <span>
                                <strong>{edge.target}</strong>{' '}
                                <span style={{ color: '#64748b' }}>({edge.dependencyType})</span>
                                {edge.description && <small style={{ display: 'block', color: '#64748b' }}>{edge.description}</small>}
                              </span>
                              <button
                                type="button"
                                className="icon-button"
                                aria-label={`Remove dependency on ${edge.target}`}
                                onClick={() => handleRemoveDependency(edge.target)}
                                style={{ color: '#ef4444' }}
                              >
                                <Trash2 size={14} />
                              </button>
                            </li>
                          ))}
                      </ul>
                    )}
                  </div>

                  <div>
                    <strong style={{ fontSize: '0.85rem', color: '#475569', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                      Downstream Consumers (Depended on by)
                    </strong>
                    {dependencyGraph?.downstream.length === 0 ? (
                      <p style={{ fontSize: '0.85rem', color: '#94a3b8', fontStyle: 'italic', margin: '0.5rem 0' }}>No downstream consumers</p>
                    ) : (
                      <ul style={{ listStyle: 'none', padding: 0, margin: '0.5rem 0' }}>
                        {dependencyGraph?.downstream.map((sourceId) => (
                          <li
                            key={sourceId}
                            style={{
                              padding: '0.5rem 0.75rem',
                              background: '#f8fafc',
                              borderRadius: '6px',
                              marginBottom: '0.4rem',
                              fontSize: '0.85rem',
                            }}
                          >
                            <strong>{sourceId}</strong>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>
              )}
            </div>
          )}
          </div>
        </div>
      )}



      {/* TAB 3: Ephemeral Preview Environments */}
      {tab === 'previews' && (
        <div>
          <div className="cat-tab-hint">
            <Info size={16} className="cat-tab-hint-icon" />
            <span className="cat-tab-hint-text">
              Previews: môi trường preview tạm thời cho một merge request, tự huỷ khi hết hạn.
            </span>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
            <div style={{ display: 'flex', gap: '0.5rem' }}>
              {(['all', 'active', 'expired', 'destroyed'] as const).map((status) => (
                <button
                  key={status}
                  onClick={() => setPreviewFilter(status)}
                  className={previewFilter === status ? 'primary-button' : 'secondary-button'}
                  style={{ textTransform: 'capitalize', fontSize: '0.85rem' }}
                >
                  {status}
                </button>
              ))}
            </div>
          </div>

          {filteredPreviews.length === 0 ? (
            <div className="empty-state" style={{ textAlign: 'center', padding: '3rem', border: '1px dashed #cbd5e1', borderRadius: '8px' }}>
              <GitBranch size={40} style={{ opacity: 0.4, margin: '0 auto 1rem' }} />
              <h3>No preview environments found</h3>
              <p>Spin up ephemeral full-stack environments for pull requests with automatic TTL-based reconciliation.</p>
              <button className="primary-button" onClick={() => setShowCreatePreview(true)} style={{ marginTop: '1rem' }}>
                Create Preview Environment
              </button>
            </div>
          ) : (
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', background: '#ffffff', border: '1px solid #e2e8f0', borderRadius: '8px' }}>
                <thead>
                  <tr style={{ background: '#f8fafc', textAlign: 'left', borderBottom: '1px solid #e2e8f0', fontSize: '0.85rem', color: '#475569' }}>
                    <th style={{ padding: '0.75rem 1rem' }}>Application / Namespace</th>
                    <th style={{ padding: '0.75rem 1rem' }}>PR & Commit</th>
                    <th style={{ padding: '0.75rem 1rem' }}>Status</th>
                    <th style={{ padding: '0.75rem 1rem' }}>TTL / Expiration</th>
                    <th style={{ padding: '0.75rem 1rem' }}>Endpoint URL</th>
                    <th style={{ padding: '0.75rem 1rem', textAlign: 'right' }}>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredPreviews.map((p) => {
                    const isExpired = new Date(p.expiresAt).getTime() < Date.now()
                    return (
                      <tr key={p.previewId} style={{ borderBottom: '1px solid #f1f5f9', fontSize: '0.9rem' }}>
                        <td style={{ padding: '0.75rem 1rem' }}>
                          <strong style={{ display: 'block', color: '#0f172a' }}>{p.namespace}</strong>
                          <small style={{ color: '#64748b' }}>App: {p.applicationId.slice(0, 8)}...</small>
                        </td>
                        <td style={{ padding: '0.75rem 1rem' }}>
                          <div><strong>PR #{p.pullRequestId}</strong></div>
                          <code style={{ fontSize: '0.8rem', background: '#f1f5f9', padding: '0.1rem 0.3rem', borderRadius: '4px' }}>
                            {p.commitSha.slice(0, 7)}
                          </code>
                        </td>
                        <td style={{ padding: '0.75rem 1rem' }}>
                          <span
                            className={
                              p.status === 'active'
                                ? 'status status-green'
                                : p.status === 'expired'
                                ? 'status status-amber'
                                : 'status status-gray'
                            }
                          >
                            {p.status}
                          </span>
                        </td>
                        <td style={{ padding: '0.75rem 1rem' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '0.35rem', color: isExpired ? '#ef4444' : '#475569' }}>
                            <Clock size={14} />
                            <span>Expires: {new Date(p.expiresAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', month: 'short', day: 'numeric' })}</span>
                          </div>
                          <small style={{ color: '#64748b' }}>TTL: {Math.round(p.ttlSeconds / 3600)}h</small>
                        </td>
                        <td style={{ padding: '0.75rem 1rem' }}>
                          <a
                            href={p.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            style={{ display: 'inline-flex', alignItems: 'center', gap: '0.25rem', color: '#2563eb' }}
                          >
                            <ExternalLink size={14} /> {p.url}
                          </a>
                        </td>
                        <td style={{ padding: '0.75rem 1rem', textAlign: 'right' }}>
                          {p.status === 'active' && (
                            <button
                              className="secondary-button"
                              onClick={() => handleTeardownPreview(p.previewId)}
                              style={{ padding: '0.35rem 0.65rem', fontSize: '0.8rem', color: '#dc2626' }}
                            >
                              <Trash2 size={13} /> Teardown
                            </button>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* TAB 4: Self-Service Resources */}
      {tab === 'resources' && (
        <div>
          <div className="cat-tab-hint">
            <Info size={16} className="cat-tab-hint-icon" />
            <span className="cat-tab-hint-text">
              Resources: yêu cầu tài nguyên (DB, bucket…) qua phê duyệt; provider chưa cấu hình thì trạng thái &ldquo;fail-closed&rdquo; chứ không giả lập.
            </span>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
            <div style={{ display: 'flex', gap: '0.5rem' }}>
              {(['all', 'pending_approval', 'provisioned', 'provider_not_configured', 'failed', 'deprovisioned'] as const).map((st) => (
                <button
                  key={st}
                  onClick={() => setResourceFilter(st)}
                  className={resourceFilter === st ? 'primary-button' : 'secondary-button'}
                  style={{ textTransform: 'capitalize', fontSize: '0.85rem' }}
                >
                  {st.replace(/_/g, ' ')}
                </button>
              ))}
            </div>
          </div>

          {filteredResources.length === 0 ? (
            <div className="empty-state" style={{ textAlign: 'center', padding: '3rem', border: '1px dashed #cbd5e1', borderRadius: '8px' }}>
              <Database size={40} style={{ opacity: 0.4, margin: '0 auto 1rem' }} />
              <h3>No resource requests found</h3>
              <p>Request managed databases, caches, buckets, and cloud resources with automated dual-control enforcement.</p>
              <button className="primary-button" data-testid="catalog-empty-request-resource" onClick={() => setShowRequestResource(true)} style={{ marginTop: '1rem' }}>
                Request Resource
              </button>
            </div>
          ) : (
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', background: '#ffffff', border: '1px solid #e2e8f0', borderRadius: '8px' }}>
                <thead>
                  <tr style={{ background: '#f8fafc', textAlign: 'left', borderBottom: '1px solid #e2e8f0', fontSize: '0.85rem', color: '#475569' }}>
                    <th style={{ padding: '0.75rem 1rem' }}>Resource Type / Env</th>
                    <th style={{ padding: '0.75rem 1rem' }}>Team / Requester</th>
                    <th style={{ padding: '0.75rem 1rem' }}>Status</th>
                    <th style={{ padding: '0.75rem 1rem' }}>Provider & Outputs</th>
                    <th style={{ padding: '0.75rem 1rem', textAlign: 'right' }}>Governance Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredResources.map((req) => {
                    const badge = resourceStatusBadge(req.status)
                    return (
                      <tr key={req.requestId} style={{ borderBottom: '1px solid #f1f5f9', fontSize: '0.9rem' }}>
                        <td style={{ padding: '0.75rem 1rem' }}>
                          <strong style={{ display: 'block', color: '#0f172a', textTransform: 'capitalize' }}>
                            {req.resourceType}
                          </strong>
                          <span className={`status status-${req.environment === 'prod' ? 'red' : 'blue'}`} style={{ fontSize: '0.75rem' }}>
                            {req.environment}
                          </span>
                        </td>
                        <td style={{ padding: '0.75rem 1rem' }}>
                          <div><strong>{req.teamId}</strong></div>
                          <small style={{ color: '#64748b' }}>By: {req.requestedBy}</small>
                          {req.approvedBy && <small style={{ display: 'block', color: '#16a34a' }}>Approved by: {req.approvedBy}</small>}
                        </td>
                        <td style={{ padding: '0.75rem 1rem' }}>
                          <span className={`status ${badge.className}`}>{badge.label}</span>
                          {req.statusReason && (
                            <small style={{ display: 'block', color: '#64748b', marginTop: '0.25rem' }}>
                              {req.statusReason}
                            </small>
                          )}
                        </td>
                        <td style={{ padding: '0.75rem 1rem' }}>
                          <div><strong>Provider:</strong> {req.provider || 'none'}</div>
                          {Object.keys(req.outputs || {}).length > 0 && (
                            <code style={{ fontSize: '0.75rem', background: '#f8fafc', padding: '0.2rem 0.4rem', borderRadius: '4px', display: 'block', marginTop: '0.25rem' }}>
                              {JSON.stringify(req.outputs)}
                            </code>
                          )}
                        </td>
                        <td style={{ padding: '0.75rem 1rem', textAlign: 'right' }}>
                          <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end' }}>
                            {req.status === 'pending_approval' && (
                              <button
                                className="primary-button"
                                onClick={() => handleApproveResource(req.requestId, req.requestedBy, req.environment)}
                                style={{ padding: '0.35rem 0.65rem', fontSize: '0.8rem' }}
                              >
                                <ShieldCheck size={14} /> Approve
                              </button>
                            )}
                            {req.status === 'provisioned' && (
                              <button
                                className="secondary-button"
                                onClick={() => handleDeprovisionResource(req.requestId)}
                                style={{ padding: '0.35rem 0.65rem', fontSize: '0.8rem', color: '#dc2626' }}
                              >
                                <Trash2 size={13} /> Deprovision
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* MODAL: Register Catalog Service */}
      {showRegisterService && (
        <Modal
          title="Register Service in Catalog"
          description="Define an authoritative software entity in the platform service registry."
          onClose={() => setShowRegisterService(false)}
          footer={
            <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'flex-end' }}>
              <button className="secondary-button" onClick={() => setShowRegisterService(false)}>Cancel</button>
              <button className="primary-button" form="register-service-form" type="submit">Register</button>
            </div>
          }
        >
          <form id="register-service-form" onSubmit={handleRegisterService} style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
            <div>
              <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Service ID (unique slug)</label>
              <input
                required
                type="text"
                placeholder="e.g. payment-service"
                value={serviceForm.serviceId}
                onChange={(e) => setServiceForm({ ...serviceForm, serviceId: e.target.value })}
                style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
              />
            </div>
            <div>
              <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Display Name</label>
              <input
                required
                type="text"
                placeholder="e.g. Payment Gateway Service"
                value={serviceForm.name}
                onChange={(e) => setServiceForm({ ...serviceForm, name: e.target.value })}
                style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
              />
            </div>
            <div>
              <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Owning Team</label>
              <input
                required
                type="text"
                placeholder="e.g. payments-team"
                value={serviceForm.owningTeam}
                onChange={(e) => setServiceForm({ ...serviceForm, owningTeam: e.target.value })}
                style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
              />
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
              <div>
                <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Service Tier</label>
                <select
                  value={serviceForm.tier}
                  onChange={(e) => setServiceForm({ ...serviceForm, tier: e.target.value })}
                  style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
                >
                  <option value="tier-1">Tier 1 · Mission Critical</option>
                  <option value="tier-2">Tier 2 · Standard Core</option>
                  <option value="tier-3">Tier 3 · Supporting / Internal</option>
                </select>
              </div>
              <div>
                <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Lifecycle</label>
                <select
                  value={serviceForm.lifecycle}
                  onChange={(e) => setServiceForm({ ...serviceForm, lifecycle: e.target.value })}
                  style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
                >
                  <option value="active">Active</option>
                  <option value="deprecated">Deprecated</option>
                  <option value="decommissioned">Decommissioned</option>
                </select>
              </div>
            </div>
            <div>
              <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Repository URL</label>
              <input
                type="url"
                placeholder="https://github.com/org/payment-service"
                value={serviceForm.repoUrl}
                onChange={(e) => setServiceForm({ ...serviceForm, repoUrl: e.target.value })}
                style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
              />
            </div>
            <div>
              <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Documentation URL</label>
              <input
                type="url"
                placeholder="https://docs.corp.internal/services/payment"
                value={serviceForm.docsUrl}
                onChange={(e) => setServiceForm({ ...serviceForm, docsUrl: e.target.value })}
                style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
              />
            </div>
            <div>
              <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Description</label>
              <textarea
                rows={3}
                placeholder="Purpose, responsibilities, SLA guarantees..."
                value={serviceForm.description}
                onChange={(e) => setServiceForm({ ...serviceForm, description: e.target.value })}
                style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
              />
            </div>
          </form>
        </Modal>
      )}

      {/* MODAL: Add Dependency */}
      {showAddDependency && selectedService && (
        <Modal
          title={`Add Dependency for ${selectedService.name}`}
          description="Declare upstream service dependency with cycle verification."
          onClose={() => setShowAddDependency(false)}
          footer={
            <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'flex-end' }}>
              <button className="secondary-button" onClick={() => setShowAddDependency(false)}>Cancel</button>
              <button className="primary-button" form="add-dependency-form" type="submit">Add Dependency</button>
            </div>
          }
        >
          <form id="add-dependency-form" onSubmit={handleAddDependency} style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
            <div>
              <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Target Service</label>
              <select
                required
                value={depTargetId}
                onChange={(e) => setDepTargetId(e.target.value)}
                style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
              >
                <option value="">Select target service...</option>
                {services
                  .filter((s) => s.serviceId !== selectedService.serviceId)
                  .map((s) => (
                    <option key={s.serviceId} value={s.serviceId}>
                      {s.name} ({s.serviceId})
                    </option>
                  ))}
              </select>
            </div>
            <div>
              <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Dependency Type</label>
              <select
                value={depType}
                onChange={(e) => setDepType(e.target.value)}
                style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
              >
                <option value="sync">Synchronous (gRPC / HTTP REST)</option>
                <option value="async">Asynchronous (Kafka / RabbitMQ event)</option>
                <option value="database">Shared Database / Storage</option>
              </select>
            </div>
            <div>
              <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Description</label>
              <input
                type="text"
                placeholder="e.g. Calls auth-service for JWT token validation"
                value={depDesc}
                onChange={(e) => setDepDesc(e.target.value)}
                style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
              />
            </div>
          </form>
        </Modal>
      )}



      {showCreatePreview && (
        <Modal
          title="Create Ephemeral Preview Environment"
          description="Deploy an on-demand isolated environment with automatic TTL expiration."
          onClose={() => setShowCreatePreview(false)}
          footer={
            <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'flex-end' }}>
              <button className="secondary-button" onClick={() => setShowCreatePreview(false)}>Cancel</button>
              <button className="primary-button" form="create-preview-form" type="submit">Create Preview</button>
            </div>
          }
        >
          <form id="create-preview-form" onSubmit={handleCreatePreview} style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
            <div>
              <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Application ID (UUID)</label>
              <input
                required
                type="text"
                placeholder="e.g. 550e8400-e29b-41d4-a716-446655440000"
                value={previewAppId}
                onChange={(e) => setPreviewAppId(e.target.value)}
                style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
              />
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
              <div>
                <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Pull Request ID</label>
                <input
                  required
                  type="text"
                  placeholder="e.g. 142"
                  value={previewPr}
                  onChange={(e) => setPreviewPr(e.target.value)}
                  style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
                />
              </div>
              <div>
                <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Commit SHA</label>
                <input
                  required
                  type="text"
                  placeholder="e.g. 9f8a3c2"
                  value={previewSha}
                  onChange={(e) => setPreviewSha(e.target.value)}
                  style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
                />
              </div>
            </div>
            <div>
              <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Time-To-Live (TTL)</label>
              <select
                value={previewTtl}
                onChange={(e) => setPreviewTtl(parseInt(e.target.value, 10))}
                style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
              >
                <option value={7200}>2 Hours (Fast feature test)</option>
                <option value={28800}>8 Hours (Working day)</option>
                <option value={86400}>24 Hours (Standard default)</option>
                <option value={259200}>72 Hours (Long PR review)</option>
              </select>
            </div>
          </form>
        </Modal>
      )}

      {/* MODAL: Request Self-Service Resource */}
      {showRequestResource && (
        <Modal
          title="Request Self-Service Infrastructure"
          description="Provision compliant cloud resources governed by separation of duties."
          onClose={() => setShowRequestResource(false)}
          footer={
            <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'flex-end' }}>
              <button className="secondary-button" onClick={() => setShowRequestResource(false)}>Cancel</button>
              <button className="primary-button" form="request-resource-form" type="submit">Submit Request</button>
            </div>
          }
        >
          <form id="request-resource-form" onSubmit={handleRequestResource} style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
            <div>
              <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Application ID (UUID)</label>
              <input
                required
                type="text"
                placeholder="e.g. 550e8400-e29b-41d4-a716-446655440000"
                value={resAppId}
                onChange={(e) => setResAppId(e.target.value)}
                style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
              />
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
              <div>
                <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Team ID</label>
                <input
                  required
                  type="text"
                  value={resTeam}
                  onChange={(e) => setResTeam(e.target.value)}
                  style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
                />
              </div>
              <div>
                <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Environment</label>
                <select
                  value={resEnv}
                  onChange={(e) => setResEnv(e.target.value)}
                  style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
                >
                  <option value="preview">Preview / Ephemeral</option>
                  <option value="dev">Development</option>
                  <option value="staging">Staging (Dual-control required)</option>
                  <option value="prod">Production (Dual-control required)</option>
                </select>
              </div>
            </div>
            <div>
              <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Resource Type</label>
              <select
                value={resType}
                onChange={(e) => setResType(e.target.value)}
                style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
              >
                <option value="postgresql">PostgreSQL Database Instance</option>
                <option value="redis">Redis Cache Cluster</option>
                <option value="s3-bucket">S3 Object Storage Bucket</option>
                <option value="iam-role">IAM Service Account / Role</option>
              </select>
            </div>
            <div>
              <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Resource Specification (JSON)</label>
              <textarea
                required
                rows={4}
                value={resSpec}
                onChange={(e) => setResSpec(e.target.value)}
                style={{ width: '100%', padding: '0.5rem', fontFamily: 'monospace', fontSize: '0.85rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
              />
            </div>
          </form>
        </Modal>
      )}
    </main>
  )
}
