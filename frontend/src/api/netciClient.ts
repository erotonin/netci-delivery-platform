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
  // The team accountable for this application. netCI refuses a team the caller does not
  // belong to, so this is a choice among the caller's own teams, not free text.
  ownerTeam?: string | null
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
  consoleUrl: string | null
  retryOf: string | null
  startedBy: string | null
  createdAt: string
  updatedAt: string
}

export type PipelineStage = {
  id: string
  pipelineRunId: string
  stageId: string
  stageName: string
  attempt: number
  status: string
  queuedAt: string | null
  startedAt: string | null
  completedAt: string | null
  durationMs: number | null
  errorMessage: string | null
  logSnippet: string | null
  createdAt: string
  updatedAt: string
}

export type Deployment = {
  id: string
  applicationId: string
  pipelineRunId?: string | null
  runtime: Runtime
  environment: Environment
  status: string
  artifactDigest?: string
  previousArtifactDigest?: string | null
  approvedBy?: string | null
  fencingToken?: number | null
  createdAt?: string
  updatedAt?: string
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

// The credential the Portal presents on every call. Held in memory and mirrored into
// sessionStorage by the login flow, so a reload keeps the session but closing the tab
// ends it. It is deliberately never put in localStorage, where it would outlive the
// browsing session and be readable by any script on the origin for as long as it lasts.
const AUTH_TOKEN_KEY = 'netci.auth.token'

export function setAuthToken(token: string | null): void {
  if (token) window.sessionStorage.setItem(AUTH_TOKEN_KEY, token)
  else window.sessionStorage.removeItem(AUTH_TOKEN_KEY)
}

export function getAuthToken(): string | null {
  try {
    return window.sessionStorage.getItem(AUTH_TOKEN_KEY)
  } catch {
    return null
  }
}

/** Called when the API says the credential is no longer good, so the shell can log out. */
let onUnauthenticated: (() => void) | null = null
export function setUnauthenticatedHandler(handler: (() => void) | null): void {
  onUnauthenticated = handler
}

export type Principal = {
  subject: string
  displayName: string
  email: string
  roles: string[]
  // Which applications this caller may act on. Roles say what kind of thing they may do.
  teams: string[]
  method: string
}

export type Identity = {
  principal: Principal
  authMode: 'none' | 'token' | 'oidc' | string
  separationOfDuties: boolean
}

/** Who the server thinks we are. The Portal never decides this for itself. */
export function whoami(token?: string): Promise<Identity> {
  return request<Identity>('/me', token ? { headers: { Authorization: `Bearer ${token}` } } : {})
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body !== undefined) {
    headers.set('Content-Type', 'application/json')
  }
  // An explicit Authorization on the call wins, so the login screen can test a token
  // before it becomes the session.
  if (!headers.has('Authorization')) {
    const token = getAuthToken()
    if (token) headers.set('Authorization', `Bearer ${token}`)
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
    // A 401 means the token is gone, expired or revoked. Nothing the page does next can
    // succeed, so end the session here rather than letting every panel render its own
    // error about a problem that is really "you are logged out".
    if (response.status === 401 && !path.startsWith('/me')) {
      setAuthToken(null)
      onUnauthenticated?.()
    }
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
  // No requestedBy: the server records the authenticated caller. It is one half of the
  // separation-of-duties check, so a value the browser chose would defeat the control.
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

export function updateModule(moduleId: string, payload: { displayName: string; moduleType: string; description: string }): Promise<PortalModule> {
  return request<PortalModule>(`/modules/${encodeURIComponent(moduleId)}`, {
    method: 'PATCH',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export type ModuleOverview = {
  deployments: Array<{ environment: Environment; status: string }>
  recentReleases: Array<{ version: string; status: string; testStatus: string }>
  trends: { testCoverage: number | null; automationPassRate: number | null; securityFindings: number | null }
}

export function getModuleOverview(moduleId: string): Promise<ModuleOverview> {
  return request<ModuleOverview>(`/modules/${encodeURIComponent(moduleId)}/overview`)
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

export type ModuleVersion = {
  version: string
  artifactDigest: string | null
  signed: boolean
  sbom: string
  scan: string
  promotable?: boolean
  createdBy?: string | null
  createdAt?: string | null
  ciReport?: { coveragePercentage?: number; automationPassRate?: number; autoTest?: string; commit?: string } | null
  environments: Record<Environment, string>
}

export function listModuleVersions(moduleId: string): Promise<{ moduleId: string; items: ModuleVersion[] }> {
  return request<{ moduleId: string; items: ModuleVersion[] }>(`/modules/${encodeURIComponent(moduleId)}/versions`)
}

/** The reporting window and source-event count travel with the metrics on purpose:
 *  a DORA figure shown without the events behind it is decoration, not measurement. */
export type DoraProjection = {
  scope: string
  scopeId: string
  metrics: PortalMetric[]
  sourceEventCount: number
  window: { from: string; to: string; days: number }
}

export function getDora(scope: 'systems' | 'modules', scopeId: string): Promise<DoraProjection> {
  return request<DoraProjection>(`/${scope}/${encodeURIComponent(scopeId)}/dora`)
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

export function approveProductionRequest(productionRequestId: string, payload: { comment?: string }): Promise<ProductionRequest> {
  return request<ProductionRequest>(`/production-requests/${encodeURIComponent(productionRequestId)}/approve`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function createSystem(payload: { id: string; unit: string; description: string }): Promise<PortalSystem> {
  return request<PortalSystem>('/systems', {
    method: 'POST',
    headers: { 'Idempotency-Key': requestId(), 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function createModule(systemId: string, payload: { name: string; displayName: string; repositoryUrl: string; pipelineTemplate: string; runtime: Runtime; moduleType: string; description: string; defaultEnvironment: Environment; deploymentEnvironments: DeploymentEnvironmentConfig[]; stages?: string[]; pipelineConfig?: ModulePipelineConfig; ownerTeam?: string }): Promise<PortalModule> {
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

export function cancelPipelineRun(pipelineRunId: string, reason = ''): Promise<PipelineRun> {
  return request<PipelineRun>(`/pipeline-runs/${encodeURIComponent(pipelineRunId)}/cancel`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify({ reason }),
  })
}

export function retryPipelineRun(pipelineRunId: string): Promise<PipelineRun> {
  return request<PipelineRun>(`/pipeline-runs/${encodeURIComponent(pipelineRunId)}/retry`, {
    method: 'POST',
    headers: { 'Idempotency-Key': requestId(), 'X-Correlation-Id': requestId() },
  })
}

export function getPipelineStages(pipelineRunId: string): Promise<{ pipelineRunId: string; items: PipelineStage[] }> {
  return request<{ pipelineRunId: string; items: PipelineStage[] }>(`/pipeline-runs/${encodeURIComponent(pipelineRunId)}/stages`)
}

export function cancelDeployment(deploymentId: string, reason = ''): Promise<Deployment> {
  return request<Deployment>(`/deployments/${encodeURIComponent(deploymentId)}/cancel`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify({ reason }),
  })
}

export function rejectProductionRequest(productionRequestId: string, payload: { comment: string }): Promise<ProductionRequest> {
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

export type DcimServer = {
  id: string
  hostname: string
  ipAddress?: string
  environment: Environment
  status?: string
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

export type AuditEvent = {
  id: string
  action: string
  actor: string
  target: string
  applicationId: string
  pipelineRunId: string | null
  deploymentId: string | null
  correlationId: string | null
  details: Record<string, unknown>
  createdAt: string
}

export function listAuditEvents(moduleId: string): Promise<AuditEvent[]> {
  return request<AuditEvent[]>(`/audit-events?moduleId=${encodeURIComponent(moduleId)}`)
}

export function searchDcimServices(query: string): Promise<{ source: string; status: string; items: DcimService[] }> {
  return request<{ source: string; status: string; items: DcimService[] }>(`/dcim/services?query=${encodeURIComponent(query)}`)
}

export function listDcimModules(systemId: string): Promise<{ source: string; status: string; systemId: string; items: DcimModule[] }> {
  return request<{ source: string; status: string; systemId: string; items: DcimModule[] }>(`/dcim/modules?systemId=${encodeURIComponent(systemId)}`)
}

export function listDcimServers(systemId: string, moduleId?: string): Promise<{ source: string; status: string; systemId: string; moduleId: string | null; items: DcimServer[] }> {
  const query = new URLSearchParams({ systemId })
  if (moduleId) query.set('moduleId', moduleId)
  return request<{ source: string; status: string; systemId: string; moduleId: string | null; items: DcimServer[] }>(`/dcim/servers?${query}`)
}

export function listServerInventory(): Promise<ServerInventoryItem[]> {
  return request<ServerInventoryItem[]>('/servers')
}

export function createModuleVersion(moduleId: string, payload: { tag: string; gitTagUrl: string; artifactUrl: string; pipelineRunId: string; artifactDigest: string }): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/modules/${encodeURIComponent(moduleId)}/versions`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function deleteModule(moduleId: string): Promise<void> {
  return request<void>(`/modules/${encodeURIComponent(moduleId)}`, {
    method: 'DELETE',
    headers: { 'X-Correlation-Id': requestId() },
  })
}

export function deleteSystem(systemId: string): Promise<void> {
  return request<void>(`/systems/${encodeURIComponent(systemId)}`, {
    method: 'DELETE',
    headers: { 'X-Correlation-Id': requestId() },
  })
}
