export type PageId = 'dashboard' | 'systems' | 'servers' | 'system' | 'requests' | 'module' | 'new-module' | 'catalog' | 'calendar' | 'architecture'
export type ModuleTab = 'overview' | 'pipeline' | 'version' | 'config' | 'dora'
export type SettingsTab = 'general' | 'pipeline' | 'activity'

export type PortalModuleView = {
  id: string
  name: string
  type: string
  description: string
  versions: string[]
  runtime: 'docker' | 'kubernetes' | 'systemd'
  environments: Array<{ name: 'dev' | 'staging' | 'prod'; status: string }>
}

export type PortalSystemView = {
  id: string
  code: string
  unit: string
  description: string
  owner: string
  status: 'unknown' | 'healthy' | 'degraded' | 'critical'
  modules: PortalModuleView[]
  runs: number
  succeeded: number
  failed: number
}

export type PortalServer = {
  id: string
  systemId: string
  ip: string
  environment: 'Dev' | 'Staging' | 'Production'
  status: 'Online' | 'Maintenance' | 'Offline' | 'Unknown'
  lastChecked: string
  usedBy: Array<{ systemId: string; moduleId: string; environment: string }>
  dcim: { status: string; valid: boolean; message: string; netboxUrl?: string | null } | null
  agent: { replicaId: string; lastSeenAt: string; stale: boolean } | null
  telemetry: { cpuPercent: number; memPercent: number; diskPercent: number; observedAt: string } | null
}

export type DoraCardMetric = {
  key: string
  label: string
  value: string
  unit: string
  hint: string
  trend?: string
  tone?: string
}
