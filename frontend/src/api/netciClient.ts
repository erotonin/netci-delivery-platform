export type Runtime = 'docker' | 'kubernetes' | 'systemd'
export type Environment = 'dev' | 'staging' | 'prod'

export type StageDefinition = {
  id: string
  name: string
  category: string
  enabledByDefault: boolean
}

export type TemplateDefinition = {
  id: string
  name: string
  runtime: Runtime
  stageIds: string[]
}

export type ApplicationCreate = {
  name: string
  repositoryUrl: string
  pipelineTemplate: string
  runtime: Runtime
  defaultEnvironment?: Environment
  stages?: string[]
}

const baseUrl = import.meta.env.VITE_NETCI_API_URL ?? 'http://localhost:8000'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    ...init,
  })
  if (!response.ok) {
    throw new Error(`netCI API ${response.status}: ${await response.text()}`)
  }
  return response.json() as Promise<T>
}

export function getStageCatalog() {
  return request<{ stages: StageDefinition[]; templates: TemplateDefinition[] }>('/stage-catalog')
}

export function createApplication(payload: ApplicationCreate, idempotencyKey = crypto.randomUUID()) {
  return request('/applications', {
    method: 'POST',
    headers: { 'Idempotency-Key': idempotencyKey },
    body: JSON.stringify(payload),
  })
}

export function startPipeline(applicationId: string, commitSha: string, environment: Environment) {
  return request(`/applications/${applicationId}/pipeline-runs`, {
    method: 'POST',
    headers: { 'X-Correlation-Id': crypto.randomUUID() },
    body: JSON.stringify({ commitSha, environment }),
  })
}
