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
  { id: 'hello-container', name: 'Hello Container', type: 'Backend', description: 'Local container delivery application', versions: ['v1.0.0'], runtime: 'docker' },
  { id: 'hello-kubernetes', name: 'Hello Kubernetes', type: 'Workload', description: 'Local Kubernetes deployment application', versions: ['v1.0.0'], runtime: 'kubernetes' },
]

export const systems: PortalSystem[] = [
  { id: 'hello-container', code: 'VTN_HELLO_CONTAINER', unit: 'Local Infrastructure', description: 'Local container delivery application', owner: 'Admin', status: 'healthy', modules, runs: 1, succeeded: 1, failed: 0 },
  { id: 'hello-kubernetes', code: 'VTN_HELLO_KUBERNETES', unit: 'Local Infrastructure', description: 'Local Kubernetes deployment application', owner: 'Admin', status: 'healthy', modules: [{ id: 'hello-kubernetes', name: 'Hello Kubernetes', type: 'Workload', description: 'Local Kubernetes deployment application', versions: ['v1.0.0'], runtime: 'kubernetes' }], runs: 0, succeeded: 0, failed: 0 },
  { id: 'hello-systemd-go', code: 'VTN_HELLO_SYSTEMD', unit: 'Local Infrastructure', description: 'Local systemd service application', owner: 'Admin', status: 'healthy', modules: [{ id: 'hello-systemd-go', name: 'Hello Systemd Go', type: 'Backend', description: 'Local systemd service application', versions: ['v1.0.0'], runtime: 'systemd' }], runs: 0, succeeded: 0, failed: 0 },
]

export const activity = [
  { day: '22/04', success: 4, failed: 0 },
  { day: '23/04', success: 0, failed: 1 },
  { day: '24/04', success: 6, failed: 0 },
  { day: '25/04', success: 5, failed: 1 },
  { day: '26/04', success: 8, failed: 0 },
  { day: '27/04', success: 11, failed: 0 },
  { day: '28/04', success: 7, failed: 0 },
]

export const servers: PortalServer[] = [
  { id: 'localhost', systemId: 'hello-container', ip: '127.0.0.1', environment: 'Dev', status: 'Online', lastChecked: '28/04/2025 09:14' },
  { id: 'kind-local', systemId: 'hello-kubernetes', ip: '127.0.0.1', environment: 'Dev', status: 'Online', lastChecked: '28/04/2025 09:14' },
]

export const dora = [
  { key: 'frequency', label: 'Deployment Frequency', value: '8.2', unit: '/wk', hint: 'Releases per week', trend: '+12%', tone: 'purple' },
  { key: 'lead', label: 'Lead Time for Changes', value: '4.5', unit: 'h', hint: 'Commit to production', trend: '-8%', tone: 'blue' },
  { key: 'failure', label: 'Change Failure Rate', value: '3.1', unit: '%', hint: 'Deploys causing incidents', trend: '-1.2%', tone: 'red' },
  { key: 'recovery', label: 'Time to Restore Service', value: '1.2', unit: 'h', hint: 'Mean recovery time', trend: '-18%', tone: 'green' },
]

export const moduleDora = [
  { key: 'frequency', label: 'Deployment Frequency', value: '8.5', unit: '/wk', hint: 'Deploys per week', trend: '↑ 1.1', tone: 'purple' },
  { key: 'lead', label: 'Lead Time for Changes', value: '4.9', unit: 'h', hint: 'Commit to production', trend: '↓ 2.6', tone: 'blue' },
  { key: 'failure', label: 'Change Failure Rate', value: '3.1', unit: '%', hint: 'Deploys causing incidents', trend: '↓ 0.9', tone: 'red' },
  { key: 'recovery', label: 'Time to Restore Service', value: '1.2', unit: 'h', hint: 'Mean recovery time', trend: '↓ 0.5', tone: 'green' },
]

export const pipelines = [
  { id: 'ci', name: 'CI Pipeline', number: '#241', sha: 'a1c4e2f', actor: 'TrungTT', time: '28/04/2025 09:14', duration: '3m 42s', status: 'success', branch: 'main' },
  { id: 'cd-dev', name: 'CD Dev', number: '#118', sha: 'a1c4e2f', actor: 'TrungTT', time: '28/04/2025 09:20', duration: '52s', status: 'success', branch: 'develop' },
  { id: 'cd-staging', name: 'CD Staging', number: '#64', sha: 'a1c4e2f', actor: 'TrungTT', time: '28/04/2025 09:25', duration: '1m 40s', status: 'success', branch: 'release/v2.4.1' },
  { id: 'cd-prod', name: 'CD Prod', number: '#37', sha: '5c88a10', actor: 'HaiNM', time: '27/04/2025 18:02', duration: '3m 05s', status: 'success', branch: 'v2.4.0' },
  { id: 'auto-test', name: 'Automation Test', number: '#12', sha: 'a1c4e2f', actor: 'netAT', time: '28/04/2025 09:40', duration: '4m 10s', status: 'success', branch: 'main' },
]

export const pipelineStages = [
  ['Checkout', 'source'], ['Install Deps', 'source'], ['Lint', 'test'], ['Unit Test', 'test'], ['Type Check', 'test'], ['Code Analysis', 'security'], ['Security Scan', 'security'], ['Build', 'build'], ['Build Docker Image', 'build'], ['Generate Docs', 'build'], ['Integration Test', 'test'], ['Package Artifact', 'publish'], ['Publish Artifact', 'publish'],
] as const

export const versions = [
  { tag: 'v2.4.1', date: '28/04/2025 09:14', user: 'TrungTT', commit: 'a1c4e2f', coverage: 87, dev: 'Deployed', staging: 'Deployed', prod: 'Deployed', autoTest: 'Passed' },
  { tag: 'v2.4.0', date: '10/04/2025 14:22', user: 'HaiNM', commit: '7bd11ca', coverage: 84, dev: 'Deployed', staging: 'Deployed', prod: 'Superseded', autoTest: 'Passed' },
  { tag: 'v2.3.8', date: '01/03/2025 11:08', user: 'DungLV', commit: 'cc7101e', coverage: 82, dev: 'Deployed', staging: 'Not deployed', prod: 'Not deployed', autoTest: 'Failed' },
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
  { id: 'PR-2025-0033', modules: ['Web Client · v1.9.2', 'Backend API · v2.4.1'], requestedBy: 'LinhPT', scheduled: '30/04/2025 03:00', sr: 'SR-88400', cr: 'CR-44265', status: 'Pending checks' },
  { id: 'PR-2025-0031', modules: ['Backend API · v2.4.0', 'Web Client · v1.9.1'], requestedBy: 'TrungTT', scheduled: '29/04/2025 02:00', sr: 'SR-88213', cr: 'CR-44210', status: 'Success' },
  { id: 'PR-2025-0028', modules: ['Backend API · v2.3.8'], requestedBy: 'DungLV', scheduled: '25/04/2025 22:00', sr: 'SR-87990', cr: 'CR-44177', status: 'Rolled back' },
]

export const auditEvents = [
  { action: 'Triggered pipeline', user: 'TrungTT', pipeline: 'CI Pipeline', detail: 'Run #241 · main · a1c4e2f', time: '28/04/2025 09:14' },
  { action: 'Updated pipeline', user: 'Admin', pipeline: 'CD Staging', detail: 'Changed target environment and runner', time: '27/04/2025 16:42' },
  { action: 'Granted access', user: 'Admin', pipeline: 'CI Pipeline', detail: 'Maintainer access granted to LinhPT', time: '26/04/2025 11:05' },
  { action: 'Published CI report', user: 'netCI Pipeline', pipeline: 'CI Pipeline', detail: 'v2.4.1 · coverage 87% · SAST passed', time: '25/04/2025 19:21' },
]

export const dcimModules = [
  { id: 'backend-api', name: 'Backend API', code: 'NETCHAT_BE', type: 'Backend', repo: 'https://git.example.net/netchat/backend-api', registered: true },
  { id: 'web-client', name: 'Web Client', code: 'NETCHAT_WEB', type: 'Frontend', repo: 'https://git.example.net/netchat/web-client', registered: true },
  { id: 'notification-worker', name: 'Notification Worker', code: 'NETCHAT_NOTIFY', type: 'Worker', repo: 'https://git.example.net/netchat/notification-worker', registered: false },
  { id: 'media-service', name: 'Media Service', code: 'NETCHAT_MEDIA', type: 'Backend', repo: 'https://git.example.net/netchat/media-service', registered: false },
  { id: 'edge-gateway', name: 'Edge Gateway', code: 'NETCHAT_EDGE', type: 'Gateway', repo: 'https://git.example.net/netchat/edge-gateway', registered: false },
]

export const deploymentTasks = [
  ['Restart service', 'Restart a systemd service safely'], ['Pull Docker image', 'Pull an immutable image digest'], ['Recreate container', 'Stop and recreate a Docker container'], ['Copy artifact', 'Copy release artifact to target'], ['Copy env file', 'Upload managed environment file'], ['Run migrations', 'Execute database migration command'], ['Health check', 'Verify the service endpoint'], ['Clear cache', 'Clear application cache after deploy'],
]
