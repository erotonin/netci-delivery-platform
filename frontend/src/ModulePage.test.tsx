import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    getModule: vi.fn().mockResolvedValue({
      id: 'notification-worker', systemId: 'netChat', name: 'Notification Worker', type: 'Worker', description: 'Notifications', runtime: 'docker', applicationId: 'app-1', versions: [], deploymentEnvironments: [], environments: [], pipelineRuns: [], dora: [],
      pipelineConfig: { runner: 'on-prem', strategy: 'Trunk-based', pipelines: { CI: { branch: 'main', coverageReportPath: 'coverage/lcov.info', stages: ['checkout', 'unit-test'] }, 'CD Prod': { branch: 'release/*', coverageReportPath: 'coverage/lcov.info', stages: ['checkout', 'deploy', 'health-check'] } } },
    }),
    listModulePipelineRuns: vi.fn().mockResolvedValue({ moduleId: 'notification-worker', items: [] }),
  }
})

import { ModulePage } from './ModulePage'
import { PortalFeedbackProvider } from './PortalFeedback'

describe('ModulePage pipeline contract', () => {
  it('renders the branch configuration persisted by the module wizard', async () => {
    const user = userEvent.setup()
    render(<PortalFeedbackProvider><ModulePage moduleId="notification-worker" onSettings={vi.fn()} /></PortalFeedbackProvider>)

    await screen.findByRole('heading', { name: 'Notification Worker' })
    await user.click(screen.getByRole('tab', { name: 'Pipeline' }))

    expect(await screen.findByText(/Jenkins · release\/\*/)).toBeTruthy()
  })
})
