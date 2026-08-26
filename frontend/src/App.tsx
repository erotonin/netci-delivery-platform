import { useEffect, useState } from 'react'
import { getPortalDashboard } from './api/netciClient'
import { DashboardPage, ServersPage, SystemPage, SystemsPage } from './GeneralPages'
import { ModulePage } from './ModulePage'
import { ModuleSettings } from './ModuleSettings'
import { NewModuleWizard } from './NewModuleWizard'
import { PortalShell, type Navigate } from './PortalShell'
import { ProductionRequestsPage } from './ProductionRequestsPage'
import type { PageId } from './portalData'
import './styles.css'

function App() {
  const [page, setPage] = useState<PageId>('dashboard')
  const [systemId, setSystemId] = useState('netChat')
  const [moduleId, setModuleId] = useState('backend-api')
  const [settingsOpen, setSettingsOpen] = useState(false)

  useEffect(() => {
    getPortalDashboard().catch(() => undefined)
  }, [])

  const navigate: Navigate = (nextPage, options) => {
    if (options?.systemId) setSystemId(options.systemId)
    if (options?.moduleId) setModuleId(options.moduleId)
    setSettingsOpen(false)
    setPage(nextPage)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  return <PortalShell page={page} systemId={systemId} moduleId={moduleId} navigate={navigate} onSettings={() => { setSystemId('netChat'); setModuleId('backend-api'); setPage('module'); setSettingsOpen(true) }}>
    {settingsOpen ? <ModuleSettings onClose={() => setSettingsOpen(false)} /> : <>
      {page === 'dashboard' && <DashboardPage navigate={navigate} />}
      {page === 'systems' && <SystemsPage navigate={navigate} />}
      {page === 'servers' && <ServersPage />}
      {page === 'system' && <SystemPage systemId={systemId} navigate={navigate} />}
      {page === 'requests' && <ProductionRequestsPage />}
      {page === 'module' && <ModulePage moduleId={moduleId} onSettings={() => setSettingsOpen(true)} />}
      {page === 'new-module' && <NewModuleWizard onCancel={() => navigate('system', { systemId })} onCreate={() => navigate('module', { systemId, moduleId: 'backend-api' })} />}
    </>}
  </PortalShell>
}

export default App
