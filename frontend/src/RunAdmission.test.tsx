import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { PipelineRun } from './api/netciClient'

const run = (over: Partial<PipelineRun>): PipelineRun => ({
  id: 'r1', applicationId: 'app-1', status: 'succeeded', commitSha: 'a'.repeat(40), branch: 'main',
  environment: 'dev', parameters: {}, jenkinsRunId: null, workflowId: null, artifactDigest: null,
  consoleUrl: null, retryOf: null, startedBy: 'dev1', createdAt: '2026-09-25T00:00:00Z', updatedAt: '2026-09-25T00:00:00Z',
  admittedAt: '2026-09-25T00:00:01Z', concurrencyGroup: null, supersededBy: null,
  ...over,
})

// Dev: one run written but not admitted -- its quota scope is full (ADR-050).
const waiting = run({
  id: '11111111-0000-4000-8000-000000000001', status: 'queued', admittedAt: null,
  concurrencyGroup: 'app-1:branch/main', createdAt: '2026-09-25T00:05:00Z',
})
// Staging: an older run superseded by a newer one of the same pull request.
const superseder = run({
  id: '22222222-0000-4000-8000-000000000002', environment: 'staging', status: 'running',
  jenkinsRunId: 'jenkins-a:netci-x#7', concurrencyGroup: 'app-1:pr/12', createdAt: '2026-09-25T00:04:00Z',
})
const superseded = run({
  id: '33333333-0000-4000-8000-000000000003', environment: 'staging', status: 'cancelled',
  concurrencyGroup: 'app-1:pr/12', supersededBy: superseder.id, admittedAt: null, createdAt: '2026-09-25T00:03:00Z',
})

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    getModule: vi.fn().mockResolvedValue({
      id: 'payments-api', systemId: 'pay', name: 'Payments API', type: 'Backend', description: 'Payments', runtime: 'docker', applicationId: 'app-1', versions: [],
      deploymentEnvironments: [
        { displayName: 'Development', environment: 'dev', runtime: 'docker', servers: ['dev-host'], tasks: [], taskSettings: {} },
        { displayName: 'Staging', environment: 'staging', runtime: 'docker', servers: ['stg-host'], tasks: [], taskSettings: {} },
      ],
      environments: [], pipelineRuns: [], dora: [],
      pipelineConfig: { runner: 'Jenkins', strategy: 'Trunk-based', pipelines: { CI: { branch: 'main', coverageReportPath: '', stages: ['checkout', 'build'] } } },
    }),
    listModulePipelineRuns: vi.fn(async () => ({ moduleId: 'payments-api', items: [waiting, superseder, superseded] })),
    getPipelineLogs: vi.fn().mockResolvedValue({ lines: [] }),
    getPipelineStages: vi.fn().mockResolvedValue({ items: [] }),
    getModuleGitCommits: vi.fn().mockResolvedValue({ moduleId: 'payments-api', ref: 'main', error: null, items: [] }),
    getModuleGitRefs: vi.fn().mockResolvedValue({ moduleId: 'payments-api', repositoryUrl: 'https://git.example/payments-api.git', branches: [{ name: 'main', sha: 'a'.repeat(40) }], tags: [], error: null }),
    getModuleOverview: vi.fn().mockRejectedValue(new Error('not under test')),
    getModuleDeliveryRules: vi.fn().mockResolvedValue({
      moduleId: 'payments-api', defaulted: false, forkPullRequests: 'ignore',
      triggers: [
        { on: 'push', branches: ['main'], deployTo: 'dev', cancelInProgress: false, paths: ['src/**', 'Dockerfile'] },
        { on: 'pull_request', branches: ['**'], cancelInProgress: true, pathsIgnore: ['docs/**'] },
        { on: 'tag', tags: ['v*'], registerVersion: true },
      ],
      promotion: {},
    }),
  }
})

import { ModulePage, RunStatus } from './ModulePage'
import { PortalFeedbackProvider } from './PortalFeedback'

const openPipelineTab = async () => {
  const user = userEvent.setup()
  render(<PortalFeedbackProvider><ModulePage moduleId="payments-api" onSettings={vi.fn()} /></PortalFeedbackProvider>)
  await screen.findByRole('heading', { name: 'Payments API' })
  await user.click(screen.getByRole('tab', { name: 'Pipeline' }))
  return user
}

const card = async (name: string) => (await screen.findByRole('heading', { name, level: 3 })).closest('article') as HTMLElement

describe('admission and supersession in the portal (ADR-050)', () => {
  it('shows a queued run without admittedAt as waiting, never as queued for CI or running', () => {
    const { container } = render(<RunStatus run={waiting} />)
    expect(container.textContent).toBe('chờ tới lượt')
    expect(container.textContent).not.toMatch(/running|queued/i)
  })

  it('reads a run as waiting only when the server said admittedAt is null', () => {
    // An API that does not send the field has not said the run is waiting.
    const { container: absent } = render(<RunStatus run={run({ status: 'queued', admittedAt: undefined })} />)
    expect(absent.textContent).toBe('queued')
    const { container: admitted } = render(<RunStatus run={run({ status: 'queued' })} />)
    expect(admitted.textContent).toBe('queued')
  })

  it('labels the waiting run on its environment card and not as running', async () => {
    await openPipelineTab()
    const dev = await card('Development')
    expect(await within(dev).findByText('chờ tới lượt')).toBeTruthy()
    expect(dev.textContent).not.toMatch(/running/i)
  })

  it('explains the wait on the run page', async () => {
    const user = await openPipelineTab()
    const dev = await card('Development')
    await user.click(await within(dev).findByText('chờ tới lượt'))
    const notice = await screen.findByTestId('run-admission-notice')
    expect(notice.textContent).toContain('chưa được gửi tới Jenkins')
    expect(notice.textContent).toContain('app-1:branch/main')
    expect(screen.queryByText(/^running$/i)).toBeNull()
  })

  it('names the run that superseded a cancelled one, and opens it', async () => {
    const user = await openPipelineTab()
    await user.click(screen.getByRole('button', { name: 'Open history Staging' }))
    const row = (await screen.findByText('superseded by #22222222')).closest('button') as HTMLElement
    expect(row).toBeTruthy()
    await user.click(row)

    const notice = await screen.findByTestId('run-superseded-notice')
    expect(notice.textContent).toContain('Superseded by #22222222')
    expect(notice.textContent).toContain('app-1:pr/12')
    await user.click(within(notice).getByRole('button', { name: '#22222222' }))

    expect(await screen.findByRole('heading', { name: /Staging · build #7/ })).toBeTruthy()
    expect(screen.queryByTestId('run-superseded-notice')).toBeNull()
  })

  it('states each trigger rule\'s cancelInProgress and path filters when set', async () => {
    await openPipelineTab()
    const panel = await screen.findByTestId('delivery-flow')
    const items = panel.querySelectorAll('li')
    expect(items[0].textContent).toBe('push main → build → deploy dev · cancelInProgress: false · paths: src/**, Dockerfile')
    expect(items[1].textContent).toBe('pull_request ** → build only · cancelInProgress: true · pathsIgnore: docs/**')
    expect(items[2].textContent).toBe('tag v* → build only + register version')
    expect(panel.textContent).toContain('only when the SCM reported every changed file')
  })
})
