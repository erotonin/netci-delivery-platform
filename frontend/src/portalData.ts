export type PageId = 'dashboard' | 'systems' | 'servers' | 'system' | 'requests' | 'module' | 'new-module'
export type ModuleTab = 'overview' | 'pipeline' | 'version' | 'dora'
export type SettingsTab = 'general' | 'pipelines' | 'pipeline-access' | 'team' | 'activity'

export type PortalModule = {
  id: string
  name: string
  type: string
  description: string
  versions: string[]
  runtime: 'docker' | 'kubernetes' | 'systemd'
}

export type PortalSystem = {
  id: string
  code: string
  unit: string
  description: string
  owner: string
  status: 'healthy' | 'degraded' | 'critical'
  modules: PortalModule[]
  runs: number
  succeeded: number
  failed: number
}

export type PortalServer = {
  id: string
  systemId: string
  ip: string
  environment: 'Dev' | 'Staging' | 'Production'
  status: 'Online' | 'Bảo trì' | 'Offline'
  lastChecked: string
}

export const modules: PortalModule[] = [
  { id: 'hello-container', name: 'Hello Container', type: 'Backend', description: 'Local container delivery application', versions: ['v1.2.0', 'v1.1.0', 'v1.0.0'], runtime: 'docker' },
  { id: 'hello-kubernetes', name: 'Hello Kubernetes', type: 'Workload', description: 'Local Kubernetes deployment application', versions: ['v1.2.0', 'v1.1.0', 'v1.0.0'], runtime: 'kubernetes' },
  { id: 'hello-systemd-go', name: 'Hello Systemd Go', type: 'Backend', description: 'Local systemd service application', versions: ['v1.2.0', 'v1.1.0', 'v1.0.0'], runtime: 'systemd' },
]

export const systems: PortalSystem[] = [
  { id: 'hello-container', code: 'VTN_HELLO_CONTAINER', unit: 'Local Infrastructure', description: 'Local container delivery application', owner: 'Admin', status: 'healthy', modules: [modules[0]], runs: 2, succeeded: 2, failed: 0 },
  { id: 'hello-kubernetes', code: 'VTN_HELLO_KUBERNETES', unit: 'Local Infrastructure', description: 'Local Kubernetes deployment application', owner: 'Admin', status: 'healthy', modules: [modules[1]], runs: 1, succeeded: 1, failed: 0 },
  { id: 'hello-systemd-go', code: 'VTN_HELLO_SYSTEMD', unit: 'Local Infrastructure', description: 'Local systemd service application', owner: 'Admin', status: 'healthy', modules: [modules[2]], runs: 1, succeeded: 1, failed: 0 },
]

export const activity = [
  { day: '22/08', success: 7, failed: 0 },
  { day: '23/08', success: 11, failed: 0 },
  { day: '24/08', success: 8, failed: 0 },
  { day: '25/08', success: 5, failed: 1 },
  { day: '26/08', success: 6, failed: 0 },
  { day: '27/08', success: 2, failed: 0 },
  { day: '28/08', success: 4, failed: 0 },
]

export const servers: PortalServer[] = [
  { id: 'localhost', systemId: 'hello-container', ip: '127.0.0.1', environment: 'Dev', status: 'Online', lastChecked: 'API · vừa xong' },
  { id: 'kind-local', systemId: 'hello-kubernetes', ip: '127.0.0.1', environment: 'Dev', status: 'Online', lastChecked: 'API · vừa xong' },
  { id: 'srv-hello-systemd-go', systemId: 'hello-systemd-go', ip: '127.0.0.1', environment: 'Dev', status: 'Online', lastChecked: 'API · vừa xong' },
]

export const dora = [
  { key: 'frequency', label: 'Deployment Frequency', value: '4.2', unit: '/wk', hint: 'Releases per week', trend: '+5%', tone: 'purple' },
  { key: 'lead', label: 'Lead Time for Changes', value: '1.2', unit: 'h', hint: 'Commit to production', trend: '-15%', tone: 'blue' },
  { key: 'failure', label: 'Change Failure Rate', value: '0.0', unit: '%', hint: 'Deploys causing incidents', trend: '0%', tone: 'green' },
  { key: 'recovery', label: 'Time to Restore Service', value: '0.0', unit: 'h', hint: 'Mean recovery time', trend: '0%', tone: 'green' },
]

export const moduleDora = dora

export const pipelines = [
  { id: 'ci', name: 'CI Pipeline', number: '#101', sha: 'a1b2c3d', actor: 'Operator', time: '28/08/2026 09:14', duration: '1m 20s', status: 'success', branch: 'main' },
  { id: 'cd-dev', name: 'CD Dev', number: '#102', sha: 'a1b2c3d', actor: 'Operator', time: '28/08/2026 09:20', duration: '45s', status: 'success', branch: 'main' },
  { id: 'cd-staging', name: 'CD Staging', number: '#103', sha: 'a1b2c3d', actor: 'Operator', time: '28/08/2026 09:25', duration: '1m 10s', status: 'success', branch: 'main' },
  { id: 'cd-prod', name: 'CD Prod', number: '#104', sha: 'a1b2c3d', actor: 'Operator', time: '28/08/2026 09:30', duration: '2m 05s', status: 'success', branch: 'main' },
]

export const pipelineStages = [
  ['Checkout', 'source'], ['Install Deps', 'source'], ['Lint', 'test'], ['Unit Test', 'test'], ['Type Check', 'test'], ['Code Analysis', 'security'], ['Security Scan', 'security'], ['Build', 'build'], ['Generate Docs', 'build'], ['Integration Test', 'test'], ['Package Artifact', 'publish'], ['Publish Artifact', 'publish'],
] as const

export const versions = [
  { tag: 'v1.2.0', date: '28/08/2026 09:14', user: 'Operator', commit: 'a1b2c3d', coverage: 94, dev: 'Deployed', staging: 'Deployed', prod: 'Deployed', autoTest: 'Passed' },
  { tag: 'v1.1.0', date: '20/08/2026 14:22', user: 'Operator', commit: '7bd11ca', coverage: 92, dev: 'Deployed', staging: 'Deployed', prod: 'Superseded', autoTest: 'Passed' },
  { tag: 'v1.0.0', date: '10/08/2026 11:08', user: 'Operator', commit: 'cc7101e', coverage: 90, dev: 'Deployed', staging: 'Not deployed', prod: 'Not deployed', autoTest: 'Passed' },
]

export type ProductionRequestItem = {
  id: string
  modules: string[]
  requestedBy: string
  scheduled: string
  sr: string
  cr: string
  status: 'Pending checks' | 'Success' | 'Rolled back'
}

export const productionRequests: ProductionRequestItem[] = [
  { id: 'PR-2026-0001', modules: ['Hello Container · v1.2.0', 'Hello Kubernetes · v1.2.0'], requestedBy: 'Lead Engineer', scheduled: '30/08/2026 03:00', sr: 'SR-88400', cr: 'CR-44265', status: 'Pending checks' },
  { id: 'PR-2026-0002', modules: ['Hello Systemd Go · v1.2.0'], requestedBy: 'DevOps Engine', scheduled: '29/08/2026 02:00', sr: 'SR-88213', cr: 'CR-44210', status: 'Success' },
]

export const auditEvents = [
  { action: 'Triggered pipeline', user: 'Operator', pipeline: 'CI Pipeline', detail: 'Run #101 · main · a1b2c3d', time: '28/08/2026 09:14' },
  { action: 'Updated pipeline', user: 'Admin', pipeline: 'CD Staging', detail: 'Changed target environment and runner', time: '27/08/2026 16:42' },
  { action: 'Published CI report', user: 'netCI Pipeline', pipeline: 'CI Pipeline', detail: 'v1.2.0 · coverage 94% · SAST passed', time: '25/08/2026 19:21' },
]

export const dcimModules = [
  { id: 'hello-container', name: 'Hello Container', code: 'VTN_HELLO_CONTAINER', type: 'Backend', repo: 'https://github.com/example/hello-container', registered: true },
  { id: 'hello-kubernetes', name: 'Hello Kubernetes', code: 'VTN_HELLO_KUBERNETES', type: 'Workload', repo: 'https://github.com/example/hello-kubernetes', registered: true },
  { id: 'hello-systemd-go', name: 'Hello Systemd Go', code: 'VTN_HELLO_SYSTEMD', type: 'Backend', repo: 'https://github.com/example/hello-systemd-go', registered: true },
]

export const deploymentTasks = [
  ['Restart service', 'Restart a systemd service safely'], ['Pull Docker image', 'Pull an immutable image digest'], ['Recreate container', 'Stop and recreate a Docker container'], ['Copy artifact', 'Copy release artifact to target'], ['Copy env file', 'Upload managed environment file'], ['Run migrations', 'Execute database migration command'], ['Health check', 'Verify the service endpoint'], ['Clear cache', 'Clear application cache after deploy'],
]
