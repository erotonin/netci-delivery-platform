import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    getModule: vi.fn().mockResolvedValue({
      id: 'notification-worker', systemId: 'netChat', name: 'Notification Worker', type: 'Worker', description: 'Notifications', runtime: 'docker', applicationId: 'app-1', versions: [], deploymentEnvironments: [{ displayName: 'Production', environment: 'prod', runtime: 'docker', servers: ['prod-host'], tasks: [], taskSettings: {} }], environments: [], pipelineRuns: [], dora: [],
      pipelineConfig: { runner: 'Jenkins', strategy: 'Trunk-based', pipelines: { CI: { branch: 'main', coverageReportPath: 'coverage/lcov.info', stages: ['checkout', 'unit-test'] }, 'CD Prod': { branch: 'release/*', coverageReportPath: 'coverage/lcov.info', stages: ['checkout', 'deploy', 'health-check'] } } },
    }),
    listModulePipelineRuns: vi.fn().mockResolvedValue({ moduleId: 'notification-worker', items: [] }),
    getModuleGitCommits: vi.fn().mockResolvedValue({ moduleId: 'notification-worker', ref: 'main', error: null, items: [
      { sha: 'b'.repeat(40), subject: 'fix: retry the push', author: 'Dana', committedAt: '2026-09-22T10:00:00Z' },
      { sha: 'c'.repeat(40), subject: 'feat: batch notifications', author: 'Rae', committedAt: '2026-09-21T10:00:00Z' },
    ] }),
    getModuleGitRefs: vi.fn().mockResolvedValue({ moduleId: 'notification-worker', repositoryUrl: 'https://git.example/notification-worker.git', branches: [{ name: 'main', sha: 'a'.repeat(40) }], tags: [], error: null }),
    getDora: vi.fn(),
  }
})

import { getDora } from './api/netciClient'
import { ModulePage } from './ModulePage'
import { PortalFeedbackProvider } from './PortalFeedback'

describe('ModulePage pipeline contract', () => {
  it('renders the branch configuration persisted by the module wizard', async () => {
    const user = userEvent.setup()
    render(<PortalFeedbackProvider><ModulePage moduleId="notification-worker" onSettings={vi.fn()} /></PortalFeedbackProvider>)

    await screen.findByRole('heading', { name: 'Notification Worker' })
    await user.click(screen.getByRole('tab', { name: 'Pipeline' }))

    // One card per configured environment, carrying the branch the wizard configured for it.
    expect(await screen.findByText(/(?:nhánh|branch) release\/\*/)).toBeTruthy()
  })


  it('states how many delivery events the DORA figures came from', async () => {
    const user = userEvent.setup()
    vi.mocked(getDora).mockResolvedValue({
      scope: 'module',
      scopeId: 'notification-worker',
      metrics: [{ key: 'deploymentFrequency', label: 'Deployment Frequency', value: 1.4, unit: '/wk', hint: 'Production deployments per week' }],
      sourceEventCount: 6,
      window: { from: '2026-07-28T00:00:00+00:00', to: '2026-08-27T00:00:00+00:00', days: 30 },
    })

    render(<PortalFeedbackProvider><ModulePage moduleId="notification-worker" onSettings={vi.fn()} /></PortalFeedbackProvider>)
    await screen.findByRole('heading', { name: 'Notification Worker' })
    await user.click(screen.getByRole('tab', { name: 'DORA Metrics' }))

    expect(await screen.findByText(/projected from 6 delivery events/)).toBeTruthy()
  })

  it('says so plainly when no delivery events have been recorded', async () => {
    const user = userEvent.setup()
    vi.mocked(getDora).mockResolvedValue({
      scope: 'module',
      scopeId: 'notification-worker',
      metrics: [{ key: 'deploymentFrequency', label: 'Deployment Frequency', value: 0, unit: '/wk', hint: 'Production deployments per week' }],
      sourceEventCount: 0,
      window: { from: '2026-07-28T00:00:00+00:00', to: '2026-08-27T00:00:00+00:00', days: 30 },
    })

    render(<PortalFeedbackProvider><ModulePage moduleId="notification-worker" onSettings={vi.fn()} /></PortalFeedbackProvider>)
    await screen.findByRole('heading', { name: 'Notification Worker' })
    await user.click(screen.getByRole('tab', { name: 'DORA Metrics' }))

    expect(await screen.findByText(/no delivery events recorded yet/)).toBeTruthy()
  })

  it('lets a commit be chosen by its message, and sends that exact commit', async () => {
    // The dialog offered only the branch tip; anything older had to be pasted as hex.
    const user = userEvent.setup()
    render(<PortalFeedbackProvider><ModulePage moduleId="notification-worker" onSettings={vi.fn()} /></PortalFeedbackProvider>)
    await screen.findByRole('heading', { name: 'Notification Worker' })
    await user.click(screen.getByRole('tab', { name: 'Pipeline' }))
    await user.click((await screen.findAllByRole('button', { name: /^Run / }))[0])

    const picker = await screen.findByTestId('run-commit-picker')
    expect(within(picker).getByText(/feat: batch notifications/)).toBeTruthy()
    await user.selectOptions(picker, 'c'.repeat(40))
    expect((screen.getByPlaceholderText('7–64 hex characters') as HTMLInputElement).value).toBe('c'.repeat(40))
  })
})
