export type Runtime = 'docker' | 'kubernetes' | 'systemd'
export type Environment = 'dev' | 'staging' | 'prod'
export type PipelineStatus =
  | 'queued'
  | 'running'
  | 'waiting_approval'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'rolled_back'

export type StageDefinition = {
  id: string
  name: string
  category: 'source' | 'test' | 'build' | 'security' | 'publish' | 'deploy' | 'verify'
  enabledByDefault: boolean
  parameters?: Record<string, unknown>
}

export type TemplateDefinition = {
  id: string
  name: string
  runtime: Runtime
  stageIds: string[]
}

export type StageCatalog = {
  stages: StageDefinition[]
  templates: TemplateDefinition[]
}

export type ApplicationCreate = {
  name: string
  repositoryUrl: string
  pipelineTemplate: string
  runtime: Runtime
  defaultEnvironment?: Environment
  stages?: string[]
}

export type Application = ApplicationCreate & {
  id: string
  createdAt: string
}

export type PipelineRunCreate = {
  commitSha: string
  branch?: string
  environment: Environment
  parameters?: Record<string, unknown>
}

export type PipelineRun = {
  id: string
  applicationId: string
  status: PipelineStatus
  commitSha: string
  branch: string
  environment: Environment
  jenkinsRunId: string | null
  workflowId: string | null
  artifactDigest: string | null
  createdAt: string
  updatedAt: string
}

type ErrorResponse = {
  code?: string
  message?: string
  correlationId?: string | null
  detail?: string | { code?: string; message?: string }
}

const configuredBaseUrl = import.meta.env.VITE_NETCI_API_URL?.trim()
const baseUrl = (configuredBaseUrl || '/api').replace(/\/$/, '')

export class NetciApiError extends Error {
  readonly status: number
  readonly code: string
  readonly correlationId: string | null

  constructor(status: number, payload: ErrorResponse | null, fallbackMessage: string) {
    const detail = typeof payload?.detail === 'object' ? payload.detail : undefined
    const message = payload?.message ?? detail?.message ?? (typeof payload?.detail === 'string' ? payload.detail : fallbackMessage)
    super(message)
    this.name = 'NetciApiError'
    this.status = status
    this.code = payload?.code ?? detail?.code ?? `HTTP_${status}`
    this.correlationId = payload?.correlationId ?? null
  }
}

function requestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body !== undefined) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(`${baseUrl}${path}`, { ...init, headers })
  const rawBody = await response.text()
  let body: unknown = null
  if (rawBody) {
    try {
      body = JSON.parse(rawBody)
    } catch {
      body = null
    }
  }

  if (!response.ok) {
    throw new NetciApiError(
      response.status,
      body && typeof body === 'object' ? (body as ErrorResponse) : null,
      rawBody || response.statusText || 'netCI API request failed',
    )
  }
  return body as T
}

export function getStageCatalog(): Promise<StageCatalog> {
  return request<StageCatalog>('/stage-catalog')
}

export function createApplication(payload: ApplicationCreate, idempotencyKey = requestId()): Promise<Application> {
  return request<Application>('/applications', {
    method: 'POST',
    headers: {
      'Idempotency-Key': idempotencyKey,
      'X-Correlation-Id': requestId(),
    },
    body: JSON.stringify(payload),
  })
}

export function startPipeline(
  applicationId: string,
  payload: PipelineRunCreate,
  idempotencyKey = requestId(),
): Promise<PipelineRun> {
  return request<PipelineRun>(`/applications/${encodeURIComponent(applicationId)}/pipeline-runs`, {
    method: 'POST',
    headers: {
      'Idempotency-Key': idempotencyKey,
      'X-Correlation-Id': requestId(),
    },
    body: JSON.stringify(payload),
  })
}

export type PortalMetric = {
  key: string
  label: string
  value: number
  unit: string
  hint: string
}

export type PortalModule = {
  id: string
  systemId: string
  name: string
  type: string
  description: string
  runtime: Runtime
  applicationId: string | null
  versions: string[]
  environments: Array<{ name: Environment; status: string }>
  pipelineRuns: PipelineRun[]
  dora: PortalMetric[]
}

export type PortalSystem = {
  id: string
  unit: string
  description: string
  owner: string
  status: string
  moduleCount: number
  pipelineRuns: number
  failedRuns: number
  modules: PortalModule[]
}

export type PortalDashboard = {
  kpis: {
    systems: number
    modules: number
    pipelineRuns: number
    successRate: number
    failureRate: number
  }
  pipelineActivity: Array<{ date: string; succeeded: number; failed: number }>
  systems: PortalSystem[]
}

export type ProductionRequest = {
  id: string
  moduleId: string
  moduleName: string
  systemId: string | null
  version: string
  requestedBy: string
  status: string
  deploymentId: string | null
  comment: string | null
}

export function getPortalDashboard(): Promise<PortalDashboard> {
  return request<PortalDashboard>('/portal/dashboard')
}

export function listSystems(): Promise<PortalSystem[]> {
  return request<PortalSystem[]>('/systems')
}

export function getSystem(systemId: string): Promise<PortalSystem> {
  return request<PortalSystem>(`/systems/${encodeURIComponent(systemId)}`)
}

export function getModule(moduleId: string): Promise<PortalModule> {
  return request<PortalModule>(`/modules/${encodeURIComponent(moduleId)}`)
}

export function getModuleOverview(moduleId: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/modules/${encodeURIComponent(moduleId)}/overview`)
}

export function listModulePipelineRuns(moduleId: string): Promise<{ moduleId: string; items: PipelineRun[] }> {
  return request<{ moduleId: string; items: PipelineRun[] }>(`/modules/${encodeURIComponent(moduleId)}/pipeline-runs`)
}

export function listModuleVersions(moduleId: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/modules/${encodeURIComponent(moduleId)}/versions`)
}

export function getDora(scope: 'systems' | 'modules', scopeId: string): Promise<{ scope: string; scopeId: string; metrics: PortalMetric[] }> {
  return request<{ scope: string; scopeId: string; metrics: PortalMetric[] }>(`/${scope}/${encodeURIComponent(scopeId)}/dora`)
}

export function listProductionRequests(): Promise<ProductionRequest[]> {
  return request<ProductionRequest[]>('/production-requests')
}

export function approveProductionRequest(productionRequestId: string, payload: { actor: string; comment?: string }): Promise<ProductionRequest> {
  return request<ProductionRequest>(`/production-requests/${encodeURIComponent(productionRequestId)}/approve`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function createSystem(payload: { id: string; unit: string; description: string; owner?: string }): Promise<PortalSystem> {
  return request<PortalSystem>('/systems', {
    method: 'POST',
    headers: { 'Idempotency-Key': requestId(), 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function createModule(systemId: string, payload: { name: string; repositoryUrl: string; pipelineTemplate: string; runtime: Runtime; moduleType: string; description: string; defaultEnvironment: Environment; stages?: string[] }): Promise<PortalModule> {
  return request<PortalModule>(`/systems/${encodeURIComponent(systemId)}/modules`, {
    method: 'POST',
    headers: { 'Idempotency-Key': requestId(), 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function startModulePipeline(moduleId: string, payload: PipelineRunCreate): Promise<PipelineRun> {
  return request<PipelineRun>(`/modules/${encodeURIComponent(moduleId)}/pipeline-runs`, {
    method: 'POST',
    headers: { 'Idempotency-Key': requestId(), 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function rejectProductionRequest(productionRequestId: string, payload: { actor: string; comment: string }): Promise<ProductionRequest> {
  return request<ProductionRequest>(`/production-requests/${encodeURIComponent(productionRequestId)}/reject`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}
