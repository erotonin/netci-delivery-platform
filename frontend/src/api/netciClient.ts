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
  parameters: Record<string, unknown>
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

export type DeploymentEnvironmentConfig = {
  displayName: string
  environment: Environment
  runtime: Runtime
  servers: string[]
  tasks: string[]
  taskSettings?: Record<string, unknown>
  kubeconfigRef?: string | null
  namespace?: string | null
}

export type ModulePipelineTabConfig = {
  branch: string
  coverageReportPath: string
  stages: string[]
}

export type ModulePipelineConfig = {
  runner: string
  strategy: string
  pipelines: Record<string, ModulePipelineTabConfig>
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
  deploymentEnvironments: DeploymentEnvironmentConfig[]
  pipelineConfig: Partial<ModulePipelineConfig>
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

export type ProductionRequestModule = {
  moduleId: string
  moduleName: string
  version: string
  deploymentOrder: number
}

export type ProductionRequest = {
  id: string
  modules: ProductionRequestModule[]
  requestedBy: string
  scheduledFor: string
  rollbackStrategy: 'automatic' | 'manual'
  runAutomationTests: boolean
  status: string
  deploymentId: string | null
  comment: string | null
}

export type ProductionRequestCreate = {
  modules: Array<{ moduleId: string; version: string; deploymentOrder: number }>
  requestedBy: string
  scheduledFor: string
  rollbackStrategy: 'automatic' | 'manual'
  runAutomationTests: boolean
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

export function getPipelineRun(pipelineRunId: string): Promise<PipelineRun> {
  return request<PipelineRun>(`/pipeline-runs/${encodeURIComponent(pipelineRunId)}`)
}

export function getPipelineLogs(pipelineRunId: string): Promise<{ pipelineRunId: string; correlationId: string; lines: string[] }> {
  return request<{ pipelineRunId: string; correlationId: string; lines: string[] }>(`/pipeline-runs/${encodeURIComponent(pipelineRunId)}/logs`)
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

export function createProductionRequest(payload: ProductionRequestCreate): Promise<ProductionRequest> {
  return request<ProductionRequest>('/production-requests', {
    method: 'POST',
    headers: { 'Idempotency-Key': requestId(), 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
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

export function createModule(systemId: string, payload: { name: string; displayName: string; repositoryUrl: string; pipelineTemplate: string; runtime: Runtime; moduleType: string; description: string; defaultEnvironment: Environment; deploymentEnvironments: DeploymentEnvironmentConfig[]; stages?: string[]; pipelineConfig?: ModulePipelineConfig }): Promise<PortalModule> {
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

export type DcimService = {
  id: string
  name: string
  code: string
  tenant: string
  tier: string
  description: string
}

export type DcimModule = {
  id: string
  name: string
  code: string
  type: string
  repositoryUrl: string
  registered: boolean
}

export type ServerInventoryItem = {
  id: string
  hostname: string
  systemId: string
  ipAddress: string
  environment: Environment
  status: string
  kind: string
  runtime?: Runtime
}

export function searchDcimServices(query: string): Promise<{ source: string; items: DcimService[] }> {
  return request<{ source: string; items: DcimService[] }>(`/dcim/services?query=${encodeURIComponent(query)}`)
}

export function listDcimModules(systemId: string): Promise<{ source: string; systemId: string; items: DcimModule[] }> {
  return request<{ source: string; systemId: string; items: DcimModule[] }>(`/dcim/modules?systemId=${encodeURIComponent(systemId)}`)
}

export function listServerInventory(): Promise<ServerInventoryItem[]> {
  return request<ServerInventoryItem[]>('/servers')
}

export function createModuleVersion(moduleId: string, payload: { tag: string; gitTagUrl: string; artifactUrl: string; createdBy?: string }): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/modules/${encodeURIComponent(moduleId)}/versions`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}
