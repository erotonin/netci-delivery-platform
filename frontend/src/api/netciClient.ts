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
  category: 'source' | 'test' | 'build' | 'security' | 'publish' | 'deploy' | 'verify' | 'custom'
  kind: 'builtin' | 'custom'
  description?: string
  // Custom stages: a repository-relative script run after `afterStage`.
  script?: string | null
  afterStage?: string | null
  // Cannot be removed from a module's pipeline (checkout, build, SBOM, scan, sign, publish).
  required: boolean
  enabledByDefault: boolean
  position: number
  // Custom stages: `proposed` until a second administrator approves; only `active` ones run.
  status?: 'proposed' | 'active' | 'rejected'
  approvedBy?: string | null
  createdBy?: string
  parameters?: StageParameterDeclaration[]
}

export type StageParameterDeclaration = { name: string; default?: string; description?: string }

export type CustomStageCreate = {
  id: string
  name: string
  description?: string
  category?: StageDefinition['category']
  script: string
  afterStage: 'checkout' | 'unit-test' | 'build' | 'sbom' | 'vulnerability-scan' | 'sign' | 'publish'
  parameters?: StageParameterDeclaration[]
}

export type ModuleStages = {
  moduleId: string
  applicationId: string
  stages: string[]
  stageParameters?: Record<string, Record<string, string>>
  pipelineTemplate?: string
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
  /** false: build, test, sign and publish without deploying (ADR-043). */
  deploy?: boolean
}

/** What started a run, as the server recorded it. */
export type RunTrigger = {
  event?: 'push' | 'tag' | 'pull_request' | 'manual'
  ref?: string
  branch?: string
  tag?: string | null
  pullRequest?: number | null
  baseBranch?: string | null
  fromFork?: boolean
  sender?: string
  rule?: number | null
  reason?: string
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
  configRevisionId?: string | null
  startedBy: string | null
  deployAfterBuild?: boolean
  publishArtifact?: boolean
  releaseTag?: string | null
  trigger?: RunTrigger
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
  configRevisionId?: string | null
  createdAt?: string
  updatedAt?: string
}

export type ConfigRevision = {
  id: string
  revisionNumber: number
  status: 'draft' | 'pending_approval' | 'active' | 'superseded' | 'rejected'
  active: boolean
  changeSummary: string
  createdBy: string
  createdAt: string
  approvedBy: string | null
  approvedAt: string | null
  rejectionReason: string | null
  pipelineConfig: Record<string, unknown>
  deploymentConfig: Array<Record<string, unknown>>
}

export type ConfigRevisionList = {
  moduleId: string
  activeRevisionId: string | null
  configVersion: number
  items: ConfigRevision[]
}

export type ConfigDiffChange = {
  path: string
  from: unknown
  to: unknown
}

export type ConfigRevisionDiff = {
  moduleId: string
  from: number
  to: number
  changeCount: number
  changes: ConfigDiffChange[]
}

export type ConfigDriftItem = {
  environment: string
  deploymentId?: string
  status?: string
  runningConfigRevisionId?: string | null
  desiredConfigRevisionId?: string | null
  server?: string
  dcimStatus?: string
  message?: string
  drifted: boolean
  reason?: string
}

export type ConfigDriftReport = {
  moduleId: string
  activeRevisionId: string | null
  configVersion: number
  hasDrift: boolean
  deploymentDrift: ConfigDriftItem[]
  dcimDrift: ConfigDriftItem[]
}

export type ServerHealthRecord = {
  serverName: string
  status: string
  source: string
  freshnessSeconds: number
  details: Record<string, unknown>
  observedAt: string
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

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
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

export function registerCustomStage(payload: CustomStageCreate): Promise<StageDefinition> {
  return request<StageDefinition>('/stage-catalog', { method: 'POST', body: JSON.stringify(payload) })
}

export function removeCustomStage(stageId: string): Promise<void> {
  return request<void>(`/stage-catalog/${encodeURIComponent(stageId)}`, { method: 'DELETE' })
}

export function getModuleStages(moduleId: string): Promise<ModuleStages> {
  return request<ModuleStages>(`/modules/${encodeURIComponent(moduleId)}/stages`)
}

export function setModuleStages(moduleId: string, stages: string[], stageParameters: Record<string, Record<string, string>> = {}): Promise<ModuleStages> {
  return request<ModuleStages>(`/modules/${encodeURIComponent(moduleId)}/stages`, { method: 'PUT', body: JSON.stringify({ stages, stageParameters }) })
}

export function approveCustomStage(stageId: string): Promise<StageDefinition> {
  return request<StageDefinition>(`/stage-catalog/${encodeURIComponent(stageId)}/approve`, { method: 'POST' })
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
  runtimeSettings?: RuntimeSettings | null
}

// How the checked-in playbook lays the service out on the target. Reviewed config,
// never a per-run parameter (the API refuses these names as build inputs).
export type RuntimeSettings = {
  appRoot?: string | null
  hostPort?: number | null
  containerPort?: number | null
  networkMode?: 'bridge' | 'host' | null
  appPort?: number | null
  systemdScope?: 'system' | 'user' | null
  become?: boolean | null
  imagePullHost?: string | null
}

export type ModulePipelineTabConfig = {
  branch: string
  coverageReportPath?: string
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
  ownerTeam?: string | null
  repositoryUrl?: string | null
  pipelineTemplate?: string | null
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
  systemId?: string | null
  version: string
  deploymentOrder: number
  dependencies?: string[]
  status?: string
  deploymentId?: string | null
  startedAt?: string | null
  completedAt?: string | null
  errorMessage?: string | null
}

export type ReleasePlanWave = {
  wave: number
  moduleIds: string[]
}

export type ReleasePlan = {
  totalWaves: number
  waves: ReleasePlanWave[]
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
  createdAt?: string | null
  strategy?: 'rolling' | 'canary' | 'blue_green'
  strategyConfig?: Record<string, unknown>
  canaryRules?: { header_name?: string; header_value?: string; cookie?: string }
  releasePlan?: ReleasePlan | null
  policyDecision?: { id: string; allowed: boolean; riskScore: number; reason: string; checks: Record<string, unknown> } | null
}

export type ProductionRequestCreate = {
  modules: Array<{
    moduleId: string
    version: string
    deploymentOrder?: number
    dependencies?: string[]
  }>
  // No requestedBy: the server records the authenticated caller. It is one half of the
  // separation-of-duties check, so a value the browser chose would defeat the control.
  scheduledFor: string
  rollbackStrategy: 'automatic' | 'manual'
  runAutomationTests: boolean
  strategy?: 'rolling' | 'canary' | 'blue_green'
  strategyConfig?: Record<string, unknown>
  canaryRules?: { header_name?: string; header_value?: string; cookie?: string }
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

/** Platform-admin only: hand the module to another identity-provider team. */
export function setModuleOwner(moduleId: string, ownerTeam: string | null): Promise<PortalModule> {
  return request<PortalModule>(`/modules/${encodeURIComponent(moduleId)}/owner`, {
    method: 'PUT',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify({ ownerTeam }),
  })
}

export function updateModule(moduleId: string, payload: { displayName: string; moduleType: string; description: string }): Promise<PortalModule> {
  return request<PortalModule>(`/modules/${encodeURIComponent(moduleId)}`, {
    method: 'PATCH',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export type EnvironmentState = {
  environment: Environment
  status: string
  deploymentId?: string
  artifactDigest?: string | null
  version?: string | null
  pipelineRunId?: string | null
  commitSha?: string | null
  strategy?: string | null
  approvedBy?: string | null
  updatedAt?: string
}

export type RecentDeployment = {
  id: string
  environment: Environment
  status: string
  artifactDigest: string | null
  version: string | null
  strategy: string | null
  approvedBy: string | null
  createdAt: string
  updatedAt: string
  pipelineRunId: string | null
}

export type ArtifactQuality = {
  source: { pipelineRunId: string; commitSha: string; artifactDigest: string; at: string } | null
  decision?: string | null
  sbom?: { present: boolean; format: string | null; generatedBy: string | null }
  scan?: { scanner: string | null; status: string | null; critical: number | null; high: number | null }
  signature?: { provider: string | null; verified: boolean }
  ciReport?: { autoTest?: string; testsRun?: number; runner?: string; coverage?: number; coveragePercentage?: number } | null
}

export type ModuleOverview = {
  deployments: Array<{ environment: Environment; status: string }>
  environments: EnvironmentState[]
  recentRuns: PipelineRun[]
  recentDeployments: RecentDeployment[]
  quality: ArtifactQuality
  recentReleases: Array<{ version: string; status: string; testStatus: string }>
  trends: { testCoverage: number | null; automationPassRate: number | null; securityFindings: number | null }
}

export type GitRefs = {
  moduleId: string
  repositoryUrl: string
  branches: Array<{ name: string; sha: string }>
  tags: Array<{ name: string; sha: string }>
  error: string | null
}

export function getModuleGitRefs(moduleId: string): Promise<GitRefs> {
  return request<GitRefs>(`/modules/${encodeURIComponent(moduleId)}/git-refs`)
}

export type GitCommit = { sha: string; subject: string; author: string; committedAt: string }
export type GitCommits = { moduleId: string; ref: string; items: GitCommit[]; error: string | null }

/** Recent commits on one branch or tag, read by the server from the module's own repository. */
export function getModuleGitCommits(moduleId: string, ref: string, limit = 15): Promise<GitCommits> {
  const query = new URLSearchParams({ ref, limit: String(limit) })
  return request<GitCommits>(`/modules/${encodeURIComponent(moduleId)}/git-commits?${query}`)
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
  ciReport?: { coveragePercentage?: number; coverage?: number; automationPassRate?: number; autoTest?: string; commit?: string; testsRun?: number; runner?: string; source?: string } | null
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

export type DeliveryTrigger = {
  on: 'push' | 'tag' | 'pull_request'
  branches?: string[]
  tags?: string[]
  deployTo?: 'dev' | 'staging'
  registerVersion?: boolean
}

export type DeliveryRules = {
  moduleId: string
  triggers: DeliveryTrigger[]
  forkPullRequests: 'ignore' | 'verify'
  promotion: Record<string, { requireHealthyIn: Environment | null; minSoakMinutes: number }>
  defaulted: boolean
}

export function getModuleDeliveryRules(moduleId: string): Promise<DeliveryRules> {
  return request<DeliveryRules>(`/modules/${encodeURIComponent(moduleId)}/delivery-rules`)
}

export type Promotion = {
  moduleId: string
  environment: Environment
  sourcePipelineRunId: string
  artifactDigest: string
  deploymentId: string
  status: string
  evidence: string | null
}

export function promoteModuleArtifact(moduleId: string, payload: { pipelineRunId: string; environment: Environment }): Promise<Promotion> {
  return request<Promotion>(`/modules/${encodeURIComponent(moduleId)}/promotions`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
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

export type GitSample = {
  id: string
  name: string
  runtime: Runtime
  pipelineTemplate: string
  path: string
  // null when NETCI_SAMPLE_APPS_REPOSITORY_BASE is not configured. The server does not
  // invent a URL Jenkins could not clone; the wizard says so instead.
  repositoryUrl: string | null
  hasTests: boolean
}

export type SampleApps = {
  repositoryBaseConfigured: boolean
  items: GitSample[]
}

export type GitInfo = {
  currentCommitSha: string | null
  currentBranch: string | null
  source: 'environment' | 'git' | null
}

export function getGitInfo(): Promise<GitInfo> {
  return request<GitInfo>('/git/info')
}

export function listSampleApps(): Promise<SampleApps> {
  return request<SampleApps>('/sample-apps')
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

export function autoProvisionDcimTargets(
  systemId: string,
  moduleId: string,
  runtime = 'docker'
): Promise<{ status: string; provisioned: number; devices: string[] }> {
  return request<{ status: string; provisioned: number; devices: string[] }>('/dcim/auto-provision', {
    method: 'POST',
    body: JSON.stringify({ systemId, moduleId, runtime }),
  })
}


export function getPipelineStages(pipelineRunId: string): Promise<{ pipelineRunId: string; items: PipelineStage[] }> {
  return request<{ pipelineRunId: string; items: PipelineStage[] }>(`/pipeline-runs/${encodeURIComponent(pipelineRunId)}/stages`)
}

export function approvePipelineRun(pipelineRunId: string, comment = ''): Promise<Deployment> {
  return request<Deployment>(`/pipeline-runs/${encodeURIComponent(pipelineRunId)}/approve`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify({ comment }),
  })
}

export function cancelDeployment(deploymentId: string, reason = ''): Promise<Deployment> {
  return request<Deployment>(`/deployments/${encodeURIComponent(deploymentId)}/cancel`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify({ reason }),
  })
}

export function approveDeployment(deploymentId: string, comment = ''): Promise<Deployment> {
  return request<Deployment>(`/deployments/${encodeURIComponent(deploymentId)}/approve`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify({ comment }),
  })
}

export function rejectProductionRequest(productionRequestId: string, payload: { comment: string }): Promise<ProductionRequest> {
  return request<ProductionRequest>(`/production-requests/${encodeURIComponent(productionRequestId)}/reject`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function getProductionRequestPlan(productionRequestId: string): Promise<ProductionRequest> {
  return request<ProductionRequest>(`/production-requests/${encodeURIComponent(productionRequestId)}/plan`)
}

export function advanceCanary(
  productionRequestId: string,
  options?: { overrideReason?: string },
): Promise<{ allowed: boolean; status: string; trafficWeight: number; canaryStep: number; reason: string; analysed: boolean }> {
  const body = options?.overrideReason !== undefined ? { overrideReason: options.overrideReason } : {}
  return request(`/production-requests/${encodeURIComponent(productionRequestId)}/canary/advance`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(body),
  })
}

export function abortCanary(
  productionRequestId: string,
  reason?: string,
): Promise<{ message: string; deploymentId: string; rolledBack: boolean }> {
  return request(`/production-requests/${encodeURIComponent(productionRequestId)}/canary/abort`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify({ reason }),
  })
}

export function getDeploymentTraffic(
  deploymentId: string,
): Promise<{ deploymentId: string; strategy: string; trafficWeight: number; activeColor: string | null; canaryStep: number }> {
  return request(`/deployments/${encodeURIComponent(deploymentId)}/traffic`)
}

export type DcimService = {
  id: string
  name: string
  code: string
  tenant: string
  tier: string
  description: string
}

// What the DCIM catalog knows about a module: a NetBox device role under the tenant.
// Type and repository are netCI's to ask the user for; the inventory does not hold them.
export type DcimModule = {
  id: string
  name: string
  systemId?: string
  description?: string
  source?: string
  registered: boolean
  code?: string
  type?: string
  repositoryUrl?: string
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
  moduleId?: string
  ipAddress: string | null
  environment: Environment
  status: string
  kind: string
  runtime?: Runtime
  usedBy?: Array<{ systemId: string; moduleId: string; environment: Environment }>
  dcim?: { status: string; valid: boolean; message: string; netboxUrl?: string | null; site?: string | null } | null
  agent?: { replicaId: string; lastSeenAt: string; stale: boolean } | null
  telemetry?: { cpuPercent: number; memPercent: number; diskPercent: number; observedAt: string } | null
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

export function createModuleVersion(moduleId: string, payload: { tag: string; gitTagUrl?: string; artifactUrl?: string; pipelineRunId: string; artifactDigest: string }): Promise<Record<string, unknown>> {
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

export function listConfigRevisions(moduleId: string): Promise<ConfigRevisionList> {
  return request<ConfigRevisionList>(`/modules/${encodeURIComponent(moduleId)}/config-revisions`)
}

export function proposeConfigRevision(
  moduleId: string,
  payload: { changeSummary: string; pipelineConfig?: Record<string, unknown>; deploymentConfig?: Array<Record<string, unknown>> }
): Promise<ConfigRevision & { requiresApproval?: boolean }> {
  return request<ConfigRevision & { requiresApproval?: boolean }>(`/modules/${encodeURIComponent(moduleId)}/config-revisions`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function diffConfigRevisions(moduleId: string, fromRev: number, toRev: number): Promise<ConfigRevisionDiff> {
  return request<ConfigRevisionDiff>(`/modules/${encodeURIComponent(moduleId)}/config-revisions/diff?fromRev=${fromRev}&toRev=${toRev}`)
}

export function approveConfigRevision(moduleId: string, revisionId: string): Promise<ConfigRevision> {
  return request<ConfigRevision>(`/modules/${encodeURIComponent(moduleId)}/config-revisions/${encodeURIComponent(revisionId)}/approve`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
  })
}

export function rejectConfigRevision(moduleId: string, revisionId: string, reason: string): Promise<ConfigRevision> {
  return request<ConfigRevision>(`/modules/${encodeURIComponent(moduleId)}/config-revisions/${encodeURIComponent(revisionId)}/reject`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify({ reason }),
  })
}

export function rollbackConfigRevision(moduleId: string, revisionNumber: number): Promise<ConfigRevision & { rolledBackTo: number; requiresApproval: boolean }> {
  return request<ConfigRevision & { rolledBackTo: number; requiresApproval: boolean }>(`/modules/${encodeURIComponent(moduleId)}/config-revisions/${revisionNumber}/rollback`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
  })
}

export function detectDrift(moduleId: string): Promise<ConfigDriftReport> {
  return request<ConfigDriftReport>(`/modules/${encodeURIComponent(moduleId)}/drift`)
}

export function listServersHealth(serverName?: string): Promise<{ count: number; items: ServerHealthRecord[] }> {
  const query = serverName ? `?serverName=${encodeURIComponent(serverName)}` : ''
  return request<{ count: number; items: ServerHealthRecord[] }>(`/servers/health${query}`)
}

// ----------------------------------------------------------- governance & policy

export type PolicyDecision = {
  id: string
  scope: string
  targetType: string
  targetId: string
  allowed: boolean
  reason: string
  riskScore: number
  checks: Record<string, unknown>
  rulesEvaluated: string[]
  evaluator: string
  evaluatedAt: string
  metadata: Record<string, unknown>
}

export type SecurityException = {
  id: string
  cve: string
  artifactDigest: string
  owner: string
  reason: string
  approvedBy: string
  status: string
  createdAt: string
  expiresAt: string
  revokedAt?: string | null
  revokedBy?: string | null
}

export type BreakGlassRecord = {
  id: string
  targetType: string
  targetId: string
  requestedBy: string
  reason: string
  incidentTicket: string
  status: string
  approvedBy?: string | null
  createdAt: string
  approvedAt?: string | null
  expiresAt?: string | null
}

export type ResourceQuota = {
  id: string
  scope: string
  scopeId: string
  maxConcurrentPipelines: number
  maxConcurrentDeployments: number
  maxProductionRequestsPerDay: number
  createdAt: string
  updatedAt: string
}

export function listPolicyDecisions(
  params?: { scope?: string; targetType?: string; targetId?: string; limit?: number; cursor?: string }
): Promise<{ items: PolicyDecision[]; nextCursor?: string | null; hasMore: boolean }> {
  const q = new URLSearchParams()
  if (params?.scope) q.set('scope', params.scope)
  if (params?.targetType) q.set('targetType', params.targetType)
  if (params?.targetId) q.set('targetId', params.targetId)
  if (params?.limit) q.set('limit', String(params.limit))
  if (params?.cursor) q.set('cursor', params.cursor)
  const qs = q.toString() ? `?${q.toString()}` : ''
  return request<{ items: PolicyDecision[]; nextCursor?: string | null; hasMore: boolean }>(`/policy/decisions${qs}`)
}

export function listSecurityExceptions(activeOnly: boolean = false): Promise<SecurityException[]> {
  return request<SecurityException[]>(`/security-exceptions?activeOnly=${activeOnly}`)
}

export function createSecurityException(payload: {
  cve: string
  artifactDigest: string
  owner: string
  reason: string
  expiresAt: string
}): Promise<SecurityException> {
  return request<SecurityException>('/security-exceptions', {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function revokeSecurityException(exceptionId: string): Promise<{ id: string; status: string; revokedBy: string }> {
  return request<{ id: string; status: string; revokedBy: string }>(`/security-exceptions/${encodeURIComponent(exceptionId)}/revoke`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
  })
}

export function createBreakGlassRequest(payload: {
  targetType: string
  targetId: string
  reason: string
  incidentTicket: string
}): Promise<BreakGlassRecord> {
  return request<BreakGlassRecord>('/break-glass/requests', {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function approveBreakGlassRequest(
  requestIdParam: string,
  payload?: { ttlMinutes?: number }
): Promise<BreakGlassRecord> {
  return request<BreakGlassRecord>(`/break-glass/requests/${encodeURIComponent(requestIdParam)}/approve`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: payload ? JSON.stringify(payload) : undefined,
  })
}

export function getActiveBreakGlass(targetType: string, targetId: string): Promise<BreakGlassRecord> {
  return request<BreakGlassRecord>(`/break-glass/active?targetType=${encodeURIComponent(targetType)}&targetId=${encodeURIComponent(targetId)}`)
}

export function getResourceQuota(scope: string, scopeId: string): Promise<ResourceQuota> {
  return request<ResourceQuota>(`/quotas/${encodeURIComponent(scope)}/${encodeURIComponent(scopeId)}`)
}

export function setResourceQuota(
  scope: string,
  scopeId: string,
  payload: { maxConcurrentPipelines: number; maxConcurrentDeployments: number; maxProductionRequestsPerDay: number }
): Promise<ResourceQuota> {
  return request<ResourceQuota>(`/quotas/${encodeURIComponent(scope)}/${encodeURIComponent(scopeId)}`, {
    method: 'PUT',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

// ------------------------------------------------------------- Phase 12: Catalog & Self-Service

export type CatalogService = {
  serviceId: string
  name: string
  description: string
  owningTeam: string
  tier: string
  lifecycle: string
  repoUrl: string
  docsUrl: string
  metadata: Record<string, unknown>
  createdAt: string
  updatedAt: string
}

export type CatalogServiceCreate = {
  serviceId: string
  name: string
  description?: string
  owningTeam: string
  tier?: string
  lifecycle?: string
  repoUrl?: string
  docsUrl?: string
  metadata?: Record<string, unknown>
}

export type CatalogServiceUpdate = {
  name?: string | null
  description?: string | null
  owningTeam?: string | null
  tier?: string | null
  lifecycle?: string | null
  repoUrl?: string | null
  docsUrl?: string | null
  metadata?: Record<string, unknown> | null
}

export type ServiceDependency = {
  dependencyId: string
  sourceServiceId: string
  targetServiceId: string
  dependencyType: string
  description: string
  createdAt: string
}

export type ServiceDependencyCreate = {
  targetServiceId: string
  dependencyType?: string
  description?: string
}

export type ServiceDependencyGraph = {
  serviceId: string
  nodes: CatalogService[]
  edges: {
    source: string
    target: string
    dependencyType: string
    description?: string
  }[]
  upstream: string[]
  downstream: string[]
  hasCycle: boolean
  cycles: string[][]
}

export type CatalogTemplate = {
  templateId: string
  version: string
  name: string
  description: string
  category: string
  parametersSchema: Record<string, unknown>
  pipelineDefinition: Record<string, unknown>
  isDeprecated: boolean
  createdAt: string
  updatedAt: string
}

export type CatalogTemplateCreate = {
  templateId: string
  version: string
  name: string
  description?: string
  category?: string
  parametersSchema?: Record<string, unknown>
  pipelineDefinition?: Record<string, unknown>
  isDeprecated?: boolean
}

export type TemplateInstantiateRequest = {
  version?: string | null
  applicationName: string
  owningTeam: string
  parameters?: Record<string, unknown>
}

export type TemplateInstantiatedPlan = {
  templateId: string
  version: string
  applicationName: string
  owningTeam: string
  runtime: string
  stages: string[]
  pipelineConfig: Record<string, unknown>
  deploymentConfig: Record<string, unknown>
}

export type PreviewEnvironment = {
  previewId: string
  applicationId: string
  pullRequestId: string
  commitSha: string
  namespace: string
  url: string
  status: string
  ttlSeconds: number
  expiresAt: string
  createdBy: string
  createdAt: string
  destroyedAt?: string | null
}

export type PreviewEnvironmentCreate = {
  applicationId: string
  pullRequestId: string
  commitSha: string
  ttlSeconds?: number
  createdBy?: string | null
}

export type ResourceRequest = {
  requestId: string
  applicationId: string
  teamId: string
  environment: string
  resourceType: string
  spec: Record<string, unknown>
  status: string
  statusReason: string
  provider: string
  outputs: Record<string, unknown>
  requestedBy: string
  approvedBy?: string | null
  createdAt: string
  updatedAt: string
}

export type ResourceRequestCreate = {
  applicationId: string
  teamId: string
  environment?: string
  resourceType: string
  spec?: Record<string, unknown>
  requestedBy?: string | null
}

export function listCatalogServices(
  team?: string,
  tier?: string,
  lifecycle?: string,
  cursor?: string
): Promise<{ items: CatalogService[]; nextCursor: string | null }> {
  const q = new URLSearchParams()
  if (team) q.set('team', team)
  if (tier) q.set('tier', tier)
  if (lifecycle) q.set('lifecycle', lifecycle)
  if (cursor) q.set('cursor', cursor)
  const qs = q.toString() ? `?${q.toString()}` : ''
  return request<{ items: CatalogService[]; nextCursor: string | null }>(`/catalog/services${qs}`)
}

export function getCatalogService(serviceId: string): Promise<CatalogService> {
  return request<CatalogService>(`/catalog/services/${encodeURIComponent(serviceId)}`)
}

export function registerCatalogService(payload: CatalogServiceCreate): Promise<CatalogService> {
  return request<CatalogService>('/catalog/services', {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function updateCatalogService(serviceId: string, payload: CatalogServiceUpdate): Promise<CatalogService> {
  return request<CatalogService>(`/catalog/services/${encodeURIComponent(serviceId)}`, {
    method: 'PATCH',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function deleteCatalogService(serviceId: string): Promise<{ status: string; serviceId: string }> {
  return request<{ status: string; serviceId: string }>(`/catalog/services/${encodeURIComponent(serviceId)}`, {
    method: 'DELETE',
    headers: { 'X-Correlation-Id': requestId() },
  })
}

export function getServiceDependencies(serviceId: string): Promise<ServiceDependencyGraph> {
  return request<ServiceDependencyGraph>(`/catalog/services/${encodeURIComponent(serviceId)}/dependencies`)
}

export function addServiceDependency(
  serviceId: string,
  payload: ServiceDependencyCreate
): Promise<ServiceDependency> {
  return request<ServiceDependency>(`/catalog/services/${encodeURIComponent(serviceId)}/dependencies`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function removeServiceDependency(serviceId: string, targetServiceId: string): Promise<void> {
  return request<void>(`/catalog/services/${encodeURIComponent(serviceId)}/dependencies/${encodeURIComponent(targetServiceId)}`, {
    method: 'DELETE',
    headers: { 'X-Correlation-Id': requestId() },
  })
}

export function listCatalogTemplates(
  category?: string,
  includeDeprecated: boolean = false
): Promise<{ items: CatalogTemplate[] }> {
  const q = new URLSearchParams()
  if (category) q.set('category', category)
  if (includeDeprecated) q.set('includeDeprecated', 'true')
  const qs = q.toString() ? `?${q.toString()}` : ''
  return request<{ items: CatalogTemplate[] }>(`/catalog/templates${qs}`)
}

export function getCatalogTemplate(templateId: string, version?: string): Promise<CatalogTemplate> {
  const q = version ? `?version=${encodeURIComponent(version)}` : ''
  return request<CatalogTemplate>(`/catalog/templates/${encodeURIComponent(templateId)}${q}`)
}

export function registerCatalogTemplate(payload: CatalogTemplateCreate): Promise<CatalogTemplate> {
  return request<CatalogTemplate>('/catalog/templates', {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function instantiateCatalogTemplate(
  templateId: string,
  payload: TemplateInstantiateRequest
): Promise<TemplateInstantiatedPlan> {
  return request<TemplateInstantiatedPlan>(`/catalog/templates/${encodeURIComponent(templateId)}/instantiate`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function listPreviewEnvironments(
  applicationId?: string,
  status?: string
): Promise<{ items: PreviewEnvironment[]; count: number }> {
  const q = new URLSearchParams()
  if (applicationId) q.set('applicationId', applicationId)
  if (status) q.set('status', status)
  const qs = q.toString() ? `?${q.toString()}` : ''
  return request<{ items: PreviewEnvironment[]; count: number }>(`/preview-environments${qs}`)
}

export function getPreviewEnvironment(previewId: string): Promise<PreviewEnvironment> {
  return request<PreviewEnvironment>(`/preview-environments/${encodeURIComponent(previewId)}`)
}

export function createPreviewEnvironment(payload: PreviewEnvironmentCreate): Promise<PreviewEnvironment> {
  return request<PreviewEnvironment>('/preview-environments', {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function teardownPreviewEnvironment(previewId: string): Promise<PreviewEnvironment> {
  return request<PreviewEnvironment>(`/preview-environments/${encodeURIComponent(previewId)}/teardown`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
  })
}

export function listSelfServiceResources(
  applicationId?: string,
  teamId?: string,
  environment?: string,
  status?: string
): Promise<{ items: ResourceRequest[]; count: number }> {
  const q = new URLSearchParams()
  if (applicationId) q.set('applicationId', applicationId)
  if (teamId) q.set('teamId', teamId)
  if (environment) q.set('environment', environment)
  if (status) q.set('status', status)
  const qs = q.toString() ? `?${q.toString()}` : ''
  return request<{ items: ResourceRequest[]; count: number }>(`/self-service/resources${qs}`)
}

export function getResourceRequest(requestIdParam: string): Promise<ResourceRequest> {
  return request<ResourceRequest>(`/self-service/resources/${encodeURIComponent(requestIdParam)}`)
}

export function requestSelfServiceResource(payload: ResourceRequestCreate): Promise<ResourceRequest> {
  return request<ResourceRequest>('/self-service/resources', {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function approveSelfServiceResource(
  requestIdParam: string,
  payload?: { approvedBy?: string }
): Promise<ResourceRequest> {
  return request<ResourceRequest>(`/self-service/resources/${encodeURIComponent(requestIdParam)}/approve`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: payload ? JSON.stringify(payload) : undefined,
  })
}

export function deprovisionSelfServiceResource(requestIdParam: string): Promise<ResourceRequest> {
  return request<ResourceRequest>(`/self-service/resources/${encodeURIComponent(requestIdParam)}/deprovision`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
  })
}

export type SecurityWaiver = {
  id: string
  cveId: string
  moduleId?: string | null
  reason: string
  approvedBy: string
  status: 'active' | 'expired' | 'revoked'
  expiresAt: string
  createdAt: string
  isValid: boolean
}

export type SecurityWaiverCreate = {
  cveId: string
  moduleId?: string | null
  reason: string
  expiresAt: string
}

export type ServerMaintenanceState = {
  serverName: string
  inMaintenance: boolean
  reason: string
  updatedBy: string
  updatedAt: string
}

export type ServerTelemetry = {
  serverName: string
  cpuPercent: number
  memPercent: number
  diskPercent: number
  status: 'normal' | 'critical' | 'stale'
  isStale?: boolean
  ageSeconds?: number
  observedAt: string
}

export function listSecurityWaivers(moduleId?: string, activeOnly: boolean = true): Promise<SecurityWaiver[]> {
  const q = new URLSearchParams()
  if (moduleId) q.set('moduleId', moduleId)
  if (activeOnly) q.set('activeOnly', 'true')
  const qs = q.toString() ? `?${q.toString()}` : ''
  return request<SecurityWaiver[]>(`/api/v1/security/waivers${qs}`)
}

export function createSecurityWaiver(payload: SecurityWaiverCreate): Promise<SecurityWaiver> {
  return request<SecurityWaiver>('/api/v1/security/waivers', {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function revokeSecurityWaiver(waiverId: string): Promise<{ id: string; status: string }> {
  return request<{ id: string; status: string }>(`/api/v1/security/waivers/${encodeURIComponent(waiverId)}/revoke`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
  })
}

export function listServersMaintenance(): Promise<ServerMaintenanceState[]> {
  return request<ServerMaintenanceState[]>('/api/v1/servers/maintenance')
}

export function toggleServerMaintenance(
  serverName: string,
  inMaintenance: boolean,
  reason: string = ''
): Promise<ServerMaintenanceState> {
  // The actor is the authenticated principal; the server decides, never the browser.
  return request<ServerMaintenanceState>(`/api/v1/servers/${encodeURIComponent(serverName)}/maintenance`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify({ inMaintenance, reason }),
  })
}

export function getServerTelemetry(serverName: string): Promise<ServerTelemetry> {
  return request<ServerTelemetry>(`/api/v1/servers/${encodeURIComponent(serverName)}/telemetry`)
}

export type ConfigApplyRequest = {
  environment: Environment
  revisionId?: string
}

// Applying a revision starts a real deployment of the last built artifact. The status
// is what the platform has established (pending_approval | deploying); "healthy" only
// ever comes from the worker, later, on the deployment itself.
export type ConfigApplyResponse = {
  deploymentId: string
  moduleId: string
  environment: Environment
  revisionNumber: number
  status: 'pending_approval' | 'deploying'
  artifactDigest: string
  sourcePipelineRunId: string
  fencingToken?: number | null
  configBypassedCi?: boolean
  riskLevel?: string | null
  riskReasons?: string[]
  message: string
}

export function applyModuleConfig(
  moduleId: string,
  payload: ConfigApplyRequest
): Promise<ConfigApplyResponse> {
  return request<ConfigApplyResponse>(`/modules/${encodeURIComponent(moduleId)}/config/apply`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export type AgentStatusItem = {
  hostname: string
  agentId: string
  replicaId: string
  connectedAt: string
  lastSeenAt: string
  stale: boolean
  local: boolean
  telemetry: { cpuPercent: number; memPercent: number; diskPercent: number; observedAt: string } | null
}

export type AgentStatusResponse = {
  replicaId: string
  count: number
  connectedAgents: number
  staleAgents: number
  items: AgentStatusItem[]
  agents: AgentStatusItem[]
}

export function getAgentStatus(): Promise<AgentStatusResponse> {
  return request<AgentStatusResponse>('/api/v1/agents/status')
}

export type AgentExecuteRequest = {
  hostname: string
  command: string
  timeout?: number
}

export type AgentExecuteResponse = {
  taskId: string
  hostname: string
  command: string
  exitCode: number
  stdout: string
  stderr: string
  durationMs: number
}

export function executeAgentCommand(
  payload: AgentExecuteRequest
): Promise<AgentExecuteResponse> {
  return request<AgentExecuteResponse>('/api/v1/agents/execute', {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export type ExposureFinding = {
  moduleId: string
  systemId: string
  environment: string
  artifactDigest: string
  pipelineRunId: string
  vulnerabilityId: string
  severity: string
  package: string
  installedVersion: string
  fixedVersion: string
  sources: string[]
  firstSeenAt: string
}

export type ExposureCoverage = {
  inService: number
  withSbom: number
  rescanned: number
  rescanFailed: number
  oldestRescanAt: string | null
  notCovered: { moduleId: string; environment: string; artifactDigest: string; reason: string }[]
}

export type VulnerabilityExposure = {
  vulnerabilityId: string | null
  minSeverity: string | null
  affected: ExposureFinding[]
  coverage: ExposureCoverage
}

export type RescanResult = {
  scanned: string[]
  failed: { artifactDigest: string; error: string }[]
  skippedNoSbom: string[]
  scanner: string
}

// Live exposure queries correlate running artifacts with recorded SBOMs and scan findings (ADR-045).
export function getRunningVulnerabilities(
  minSeverity: 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW' = 'HIGH'
): Promise<VulnerabilityExposure> {
  return request<VulnerabilityExposure>(`/vulnerabilities/exposure?minSeverity=${encodeURIComponent(minSeverity)}`)
}

export function getVulnerabilityExposure(id: string): Promise<VulnerabilityExposure> {
  return request<VulnerabilityExposure>(`/vulnerabilities/${encodeURIComponent(id)}/exposure`)
}

export function rescanVulnerabilities(): Promise<RescanResult> {
  return request<RescanResult>('/vulnerabilities/rescan', {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
  })
}

export type ChangeFreeze = {
  id: string
  name: string
  startsAt: string
  endsAt: string
  environments: string[]
  systemId: string | null
  moduleId: string | null
  reason: string
  createdBy: string
  createdAt: string
  cancelledAt: string | null
  cancelledBy: string | null
}

export type ChangeFreezeCreate = {
  name: string
  startsAt: string
  endsAt: string
  environments: ('dev' | 'staging' | 'prod')[]
  reason: string
  systemId?: string | null
  moduleId?: string | null
}

export async function listChangeFreezes(): Promise<ChangeFreeze[]> {
  const response = await request<{ items: ChangeFreeze[] }>('/change-freezes')
  return response.items
}

export function createChangeFreeze(payload: ChangeFreezeCreate): Promise<ChangeFreeze> {
  return request<ChangeFreeze>('/change-freezes', {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
    body: JSON.stringify(payload),
  })
}

export function cancelChangeFreeze(id: string): Promise<ChangeFreeze> {
  return request<ChangeFreeze>(`/change-freezes/${encodeURIComponent(id)}/cancel`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': requestId() },
  })
}

export type ScoreSummary = { passed: number; known: number; total: number }

export type ScorecardCheck = {
  id: string
  title: string
  // null: netCI could not evaluate this check -- shown as "unknown", never as a pass.
  passed: boolean | null
  detail: string
}

export type ModuleScorecard = {
  moduleId: string
  score: ScoreSummary
  checks: ScorecardCheck[]
}

export type ScorecardListItem = {
  moduleId: string
  systemId: string
  name: string
  score: ScoreSummary
}

export function getModuleScorecard(moduleId: string): Promise<ModuleScorecard> {
  return request<ModuleScorecard>(`/modules/${encodeURIComponent(moduleId)}/scorecard`)
}

export async function listScorecards(): Promise<ScorecardListItem[]> {
  const response = await request<{ items: ScorecardListItem[] }>('/scorecards')
  return response.items
}

export type ModuleInsights = {
  failures: {
    total: number
    byClass: Record<string, number>
    recent: { pipelineRunId: string; class: string; stage: string; at: string }[]
  }
  flaky: {
    count: number
    commits: { commitSha: string; failedRunId: string; passedRunId: string }[]
  }
  queueTime: { samples: number; p50Seconds: number; p95Seconds: number }
  leadTime: { samples: number; meanSeconds: number }
}

export function getModuleInsights(moduleId: string, days = 30): Promise<ModuleInsights> {
  return request<ModuleInsights>(`/modules/${encodeURIComponent(moduleId)}/insights?days=${encodeURIComponent(String(days))}`)
}
