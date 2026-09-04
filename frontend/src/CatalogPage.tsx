import { useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle, BookOpen, Box, CheckCircle2, ChevronRight, Clock,
  Code2, Compass, Cpu, Database, ExternalLink, GitBranch, GitCommit,
  GitFork, Layers, Network, Play, Plus, RefreshCw, Server, Shield,
  ShieldAlert, ShieldCheck, Terminal, Trash2, X, XCircle, Zap,
} from 'lucide-react'
import {
  addServiceDependency,
  approveSelfServiceResource,
  createPreviewEnvironment,
  deprovisionSelfServiceResource,
  getServiceDependencies,
  instantiateCatalogTemplate,
  listCatalogServices,
  listCatalogTemplates,
  listPreviewEnvironments,
  listSelfServiceResources,
  registerCatalogService,
  removeServiceDependency,
  requestSelfServiceResource,
  teardownPreviewEnvironment,
  type CatalogService,
  type CatalogServiceCreate,
  type CatalogTemplate,
  type PreviewEnvironment,
  type ResourceRequest,
  type ServiceDependencyGraph,
  type TemplateInstantiatedPlan,
} from './api/netciClient'
import { Modal, PageHeader, StatusPill } from './PortalShell'
import { usePortalFeedback } from './PortalFeedback'
import type { AuthSession } from './LoginPage'
import type { Navigate } from './PortalShell'

type CatalogTab = 'services' | 'templates' | 'previews' | 'resources'

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

  // Services state
  const [services, setServices] = useState<CatalogService[]>([])
  const [selectedService, setSelectedService] = useState<CatalogService | null>(null)
  const [dependencyGraph, setDependencyGraph] = useState<ServiceDependencyGraph | null>(null)
  const [loadingGraph, setLoadingGraph] = useState(false)
  const [serviceSearch, setServiceSearch] = useState('')
  const [serviceTierFilter, setServiceTierFilter] = useState('all')

  // Templates state
  const [templates, setTemplates] = useState<CatalogTemplate[]>([])
  const [templateSearch, setTemplateSearch] = useState('')
  const [selectedTemplate, setSelectedTemplate] = useState<CatalogTemplate | null>(null)
  const [instantiatePlan, setInstantiatePlan] = useState<TemplateInstantiatedPlan | null>(null)

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

  // Template instantiate form
  const [instAppName, setInstAppName] = useState('')
  const [instTeam, setInstTeam] = useState(session?.identity?.principal?.teams?.[0] ?? 'platform-core')
  const [instParams, setInstParams] = useState<Record<string, unknown>>({})

  // Load active tab data
  const refreshData = async () => {
    setLoading(true)
    try {
      if (tab === 'services') {
        const res = await listCatalogServices()
        setServices(res.items)
      } else if (tab === 'templates') {
        const res = await listCatalogTemplates()
        setTemplates(res.items)
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

  const handleInstantiateTemplate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!selectedTemplate) return
    try {
      const plan = await instantiateCatalogTemplate(selectedTemplate.templateId, {
        applicationName: instAppName,
        owningTeam: instTeam,
        parameters: instParams,
      })
      setInstantiatePlan(plan)
      feedback.notify(`Template '${selectedTemplate.name}' instantiated!`, 'success')
    } catch (err: unknown) {
      feedback.notify(`Instantiation failed: ${String(err)}`, 'error')
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

  const filteredTemplates = useMemo(() => {
    return templates.filter(
      (t) =>
        t.name.toLowerCase().includes(templateSearch.toLowerCase()) ||
        t.templateId.toLowerCase().includes(templateSearch.toLowerCase()) ||
        t.category.toLowerCase().includes(templateSearch.toLowerCase())
    )
  }, [templates, templateSearch])

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
        description="Discover platform services, Golden Path pipeline templates, ephemeral preview environments, and request cloud infrastructure resources with dual-control governance."
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
              <button className="primary-button" onClick={() => setShowRequestResource(true)}>
                <Plus size={16} /> Request Resource
              </button>
            )}
          </div>
        }
      />

      {/* Tabs Navigation */}
      <div
        role="tablist"
        style={{
          display: 'flex',
          gap: '1rem',
          borderBottom: '1px solid var(--border-color, #e2e8f0)',
          marginBottom: '1.5rem',
        }}
      >
        <button
          role="tab"
          aria-selected={tab === 'services'}
          onClick={() => setTab('services')}
          style={{
            padding: '0.75rem 1.25rem',
            fontWeight: 600,
            borderBottom: tab === 'services' ? '2px solid var(--primary, #2563eb)' : '2px solid transparent',
            color: tab === 'services' ? 'var(--primary, #2563eb)' : 'var(--text-muted, #64748b)',
            background: 'none',
            borderTop: 'none',
            borderLeft: 'none',
            borderRight: 'none',
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            gap: '0.5rem',
          }}
        >
          <Compass size={18} /> Services & Dependency Graph
        </button>

        <button
          role="tab"
          aria-selected={tab === 'templates'}
          onClick={() => setTab('templates')}
          style={{
            padding: '0.75rem 1.25rem',
            fontWeight: 600,
            borderBottom: tab === 'templates' ? '2px solid var(--primary, #2563eb)' : '2px solid transparent',
            color: tab === 'templates' ? 'var(--primary, #2563eb)' : 'var(--text-muted, #64748b)',
            background: 'none',
            borderTop: 'none',
            borderLeft: 'none',
            borderRight: 'none',
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            gap: '0.5rem',
          }}
        >
          <Zap size={18} /> Golden Path Templates
        </button>

        <button
          role="tab"
          aria-selected={tab === 'previews'}
          onClick={() => setTab('previews')}
          style={{
            padding: '0.75rem 1.25rem',
            fontWeight: 600,
            borderBottom: tab === 'previews' ? '2px solid var(--primary, #2563eb)' : '2px solid transparent',
            color: tab === 'previews' ? 'var(--primary, #2563eb)' : 'var(--text-muted, #64748b)',
            background: 'none',
            borderTop: 'none',
            borderLeft: 'none',
            borderRight: 'none',
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            gap: '0.5rem',
          }}
        >
          <GitBranch size={18} /> Ephemeral Preview Environments
        </button>

        <button
          role="tab"
          aria-selected={tab === 'resources'}
          onClick={() => setTab('resources')}
          style={{
            padding: '0.75rem 1.25rem',
            fontWeight: 600,
            borderBottom: tab === 'resources' ? '2px solid var(--primary, #2563eb)' : '2px solid transparent',
            color: tab === 'resources' ? 'var(--primary, #2563eb)' : 'var(--text-muted, #64748b)',
            background: 'none',
            borderTop: 'none',
            borderLeft: 'none',
            borderRight: 'none',
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            gap: '0.5rem',
          }}
        >
          <Database size={18} /> Self-Service Resources
        </button>
      </div>

      {/* TAB 1: Services & Dependency Graph */}
      {tab === 'services' && (
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
      )}

      {/* TAB 2: Golden Path Templates */}
      {tab === 'templates' && (
        <div>
          <div style={{ display: 'flex', gap: '1rem', marginBottom: '1.5rem' }}>
            <input
              type="text"
              placeholder="Search Golden Path templates by name, ID, or category..."
              value={templateSearch}
              onChange={(e) => setTemplateSearch(e.target.value)}
              style={{ flex: 1, padding: '0.5rem 0.75rem', borderRadius: '6px', border: '1px solid #cbd5e1' }}
            />
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(350px, 1fr))', gap: '1.5rem' }}>
            {filteredTemplates.map((tpl) => (
              <article
                key={tpl.templateId}
                style={{
                  background: '#ffffff',
                  border: '1px solid #e2e8f0',
                  borderRadius: '8px',
                  padding: '1.5rem',
                  display: 'flex',
                  flexDirection: 'column',
                  justifyContent: 'space-between',
                  boxShadow: '0 2px 4px rgba(0,0,0,0.02)',
                }}
              >
                <div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.5rem' }}>
                    <span className="status status-blue" style={{ fontSize: '0.75rem' }}>{tpl.category}</span>
                    <span style={{ fontSize: '0.8rem', color: '#64748b' }}>v{tpl.version}</span>
                  </div>
                  <h3 style={{ margin: '0.25rem 0 0.5rem', fontSize: '1.2rem', color: '#0f172a' }}>{tpl.name}</h3>
                  <p style={{ fontSize: '0.9rem', color: '#475569', marginBottom: '1rem' }}>{tpl.description}</p>
                </div>

                <div>
                  <div style={{ background: '#f8fafc', padding: '0.75rem', borderRadius: '6px', marginBottom: '1rem', fontSize: '0.8rem' }}>
                    <div style={{ color: '#64748b', marginBottom: '0.25rem' }}><strong>Template ID:</strong> {tpl.templateId}</div>
                    <div style={{ color: '#64748b' }}>
                      <strong>Parameters:</strong> {Object.keys(tpl.parametersSchema || {}).length} configurable options
                    </div>
                  </div>

                  <button
                    className="primary-button"
                    style={{ width: '100%', justifyContent: 'center' }}
                    onClick={() => {
                      setSelectedTemplate(tpl)
                      setInstAppName('')
                      setInstParams({})
                      setInstantiatePlan(null)
                    }}
                  >
                    <Play size={15} /> 1-Click Instantiate
                  </button>
                </div>
              </article>
            ))}
          </div>
        </div>
      )}

      {/* TAB 3: Ephemeral Preview Environments */}
      {tab === 'previews' && (
        <div>
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
              <button className="primary-button" onClick={() => setShowRequestResource(true)} style={{ marginTop: '1rem' }}>
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

      {/* MODAL: 1-Click Template Instantiate */}
      {selectedTemplate && (
        <Modal
          title={`Instantiate Golden Path: ${selectedTemplate.name}`}
          description={`Version ${selectedTemplate.version} · Category: ${selectedTemplate.category}`}
          onClose={() => setSelectedTemplate(null)}
          wide
          footer={
            <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'flex-end' }}>
              <button className="secondary-button" onClick={() => setSelectedTemplate(null)}>Close</button>
              {!instantiatePlan && (
                <button className="primary-button" form="instantiate-template-form" type="submit">
                  Generate Application Plan
                </button>
              )}
            </div>
          }
        >
          {!instantiatePlan ? (
            <form id="instantiate-template-form" onSubmit={handleInstantiateTemplate} style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
                <div>
                  <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Application Name (slug)</label>
                  <input
                    required
                    type="text"
                    placeholder="e.g. order-api"
                    value={instAppName}
                    onChange={(e) => setInstAppName(e.target.value)}
                    style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
                  />
                </div>
                <div>
                  <label style={{ display: 'block', fontWeight: 600, fontSize: '0.85rem', marginBottom: '0.3rem' }}>Owning Team</label>
                  <input
                    required
                    type="text"
                    value={instTeam}
                    onChange={(e) => setInstTeam(e.target.value)}
                    style={{ width: '100%', padding: '0.5rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
                  />
                </div>
              </div>

              {/* Dynamic Parameter Inputs from Template Schema */}
              {selectedTemplate.parametersSchema && Object.keys(selectedTemplate.parametersSchema).length > 0 && (
                <div style={{ background: '#f8fafc', padding: '1rem', borderRadius: '6px' }}>
                  <h4 style={{ margin: '0 0 0.75rem', fontSize: '0.95rem' }}>Template Parameters</h4>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
                    {Object.entries(selectedTemplate.parametersSchema).map(([key, schemaAny]: [string, any]) => (
                      <div key={key}>
                        <label style={{ display: 'block', fontWeight: 500, fontSize: '0.85rem', marginBottom: '0.2rem' }}>
                          {key} {schemaAny.required && <span style={{ color: '#ef4444' }}>*</span>}
                          {schemaAny.description && <small style={{ color: '#64748b', marginLeft: '0.5rem' }}>{schemaAny.description}</small>}
                        </label>
                        <input
                          type={schemaAny.type === 'integer' || schemaAny.type === 'number' ? 'number' : 'text'}
                          defaultValue={schemaAny.default ?? ''}
                          onChange={(e) => {
                            const val = schemaAny.type === 'integer' ? parseInt(e.target.value, 10) : e.target.value
                            setInstParams({ ...instParams, [key]: val })
                          }}
                          style={{ width: '100%', padding: '0.4rem 0.6rem', borderRadius: '4px', border: '1px solid #cbd5e1' }}
                        />
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </form>
          ) : (
            <div>
              <div style={{ padding: '1rem', background: '#f0fdf4', border: '1px solid #86efac', borderRadius: '6px', marginBottom: '1.25rem', color: '#166534' }}>
                <CheckCircle2 size={20} style={{ verticalAlign: 'middle', marginRight: '0.5rem' }} />
                <strong>Instantiated Configuration Plan Ready!</strong>
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem', marginBottom: '1.25rem' }}>
                <div>
                  <strong>Runtime:</strong> <code>{instantiatePlan.runtime}</code>
                </div>
                <div>
                  <strong>Stages:</strong> {instantiatePlan.stages.join(' ➔ ')}
                </div>
              </div>

              <div style={{ marginBottom: '1rem' }}>
                <h4 style={{ margin: '0 0 0.5rem', fontSize: '0.95rem' }}>Generated Pipeline Config</h4>
                <pre style={{ background: '#0f172a', color: '#f8fafc', padding: '1rem', borderRadius: '6px', fontSize: '0.85rem', overflowX: 'auto' }}>
                  {JSON.stringify(instantiatePlan.pipelineConfig, null, 2)}
                </pre>
              </div>

              <div>
                <h4 style={{ margin: '0 0 0.5rem', fontSize: '0.95rem' }}>Deployment Config</h4>
                <pre style={{ background: '#0f172a', color: '#f8fafc', padding: '1rem', borderRadius: '6px', fontSize: '0.85rem', overflowX: 'auto' }}>
                  {JSON.stringify(instantiatePlan.deploymentConfig, null, 2)}
                </pre>
              </div>
            </div>
          )}
        </Modal>
      )}

      {/* MODAL: Create Preview Environment */}
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
