import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    listProductionRequests: vi.fn().mockResolvedValue([
      {
        id: 'req-canary-1',
        modules: [
          { moduleId: 'search-api', moduleName: 'search-api', version: 'v2.0.0', deploymentOrder: 1, dependencies: [], status: 'deploying' },
        ],
        requestedBy: 'alice',
        scheduledFor: '2026-09-04T12:00:00+07:00',
        rollbackStrategy: 'automatic',
        runAutomationTests: true,
        status: 'approved',
        deploymentId: 'dep-1234',
        strategy: 'canary',
        strategyConfig: { steps: [10, 25, 50, 100] },
        releasePlan: {
          totalWaves: 1,
          waves: [{ wave: 1, moduleIds: ['search-api'] }],
        },
      },
    ]),
    getProductionRequestPlan: vi.fn().mockResolvedValue({
      id: 'req-canary-1',
      modules: [
        { moduleId: 'search-api', moduleName: 'search-api', version: 'v2.0.0', deploymentOrder: 1, dependencies: [], status: 'deploying' },
      ],
      requestedBy: 'alice',
      scheduledFor: '2026-09-04T12:00:00+07:00',
      rollbackStrategy: 'automatic',
      runAutomationTests: true,
      status: 'approved',
      deploymentId: 'dep-1234',
      strategy: 'canary',
      strategyConfig: { steps: [10, 25, 50, 100] },
      releasePlan: {
        totalWaves: 1,
        waves: [{ wave: 1, moduleIds: ['search-api'] }],
      },
    }),
    getDeploymentTraffic: vi.fn().mockResolvedValue({
      deploymentId: 'dep-1234',
      strategy: 'canary',
      trafficWeight: 25,
      canaryStep: 2,
      activeColor: null,
    }),
    advanceCanary: vi.fn().mockResolvedValue({
      allowed: true,
      status: 'advanced',
      trafficWeight: 50,
      canaryStep: 3,
      reason: 'Canary advanced to 50%',
      analysed: true,
    }),
    abortCanary: vi.fn().mockResolvedValue({
      message: 'Canary aborted',
      deploymentId: 'dep-1234',
      rolledBack: true,
    }),
    listSystems: vi.fn().mockResolvedValue([]),
    // The page asks the server who is looking, to reflect separation of duties before
    // the click. A default that resolves keeps every other test unaffected.
    whoami: vi.fn().mockResolvedValue({
      principal: { subject: 'viewer', displayName: 'Viewer', email: '', roles: ['viewer'], teams: [], method: 'oidc' },
      authMode: 'oidc',
      separationOfDuties: true,
    }),
  }
})

import { ProductionRequestsPage } from './ProductionRequestsPage'
import { PortalFeedbackProvider } from './PortalFeedback'

describe('ProductionRequestsPage (Phase 10)', () => {
  it('renders production requests with strategy pill and details modal with DAG waves and canary controls', async () => {
    render(
      <PortalFeedbackProvider>
        <ProductionRequestsPage systemId="" />
      </PortalFeedbackProvider>
    )

    // 1. Check strategy tag in the table
    expect(await screen.findByText('canary')).toBeTruthy()
    expect(screen.getByText('search-api')).toBeTruthy()

    // 2. Click Eye button to open details modal
    const viewButton = screen.getByLabelText(/xem/i)
    fireEvent.click(viewButton)

    // 3. Verify modal shows DAG release plan waves and canary controls
    expect(await screen.findByText(/DAG Release Plan/i)).toBeTruthy()
    expect(screen.getByText(/Canary Traffic Allocation/i)).toBeTruthy()
    expect(screen.getByText(/Advance Step/i)).toBeTruthy()
    expect(screen.getByText(/Abort Canary/i)).toBeTruthy()
  })

  it('prompts for overrideReason on 409 CANARY_ANALYSIS_UNAVAILABLE and advances with override', async () => {
    const client = await import('./api/netciClient')
    const { NetciApiError } = client
    const error409 = new NetciApiError(
      409,
      {
        code: 'CANARY_ANALYSIS_UNAVAILABLE',
        message: 'module search-api declares no verification queries (pipelineConfig.verification)',
      },
      'unavailable'
    )
    vi.mocked(client.advanceCanary)
      .mockRejectedValueOnce(error409)
      .mockResolvedValueOnce({
        allowed: true,
        status: 'advanced',
        trafficWeight: 50,
        canaryStep: 3,
        reason: 'advanced WITHOUT analysis by alice: manual override for emergency deploy',
        analysed: false,
      })

    const promptSpy = vi.spyOn(window, 'prompt').mockReturnValue('manual override for emergency deploy')

    render(
      <PortalFeedbackProvider>
        <ProductionRequestsPage systemId="" />
      </PortalFeedbackProvider>
    )

    const viewButton = await screen.findByLabelText(/xem/i)
    fireEvent.click(viewButton)

    const advanceButton = await screen.findByText(/Advance Step/i)
    fireEvent.click(advanceButton)

    await waitFor(() => {
      expect(promptSpy).toHaveBeenCalledWith(
        'netCI cannot analyse this canary: module search-api declares no verification queries (pipelineConfig.verification)\nAdvance anyway? Give a reason (at least 10 characters) -- it is recorded as an advance without analysis.'
      )
      expect(client.advanceCanary).toHaveBeenCalledTimes(2)
      expect(client.advanceCanary).toHaveBeenLastCalledWith('req-canary-1', {
        overrideReason: 'manual override for emergency deploy',
      })
    })

    expect(await screen.findByText(/Canary advanced WITHOUT analysis/i)).toBeTruthy()
    promptSpy.mockRestore()
    vi.mocked(client.advanceCanary).mockReset()
    vi.mocked(client.advanceCanary).mockResolvedValue({
      allowed: true,
      status: 'advanced',
      trafficWeight: 50,
      canaryStep: 3,
      reason: 'Canary advanced to 50%',
      analysed: true,
    })
  })
})


describe('separation of duties in the browser', () => {
  // The fixture the whole file shares is raised by `alice` and already approved. These
  // tests need one that is still waiting, so they install their own and put the shared
  // one back afterwards -- otherwise they would decide what the tests above see.
  const waiting = {
    id: 'req-sod-1',
    modules: [
      { moduleId: 'search-api', moduleName: 'search-api', version: 'v2.0.0', deploymentOrder: 1, dependencies: [], status: 'pending' },
    ],
    requestedBy: 'alice',
    scheduledFor: '2026-09-04T12:00:00+07:00',
    rollbackStrategy: 'automatic',
    runAutomationTests: true,
    status: 'waiting_approval',
    strategy: 'rolling',
    strategyConfig: {},
  }

  let restore: unknown
  beforeEach(async () => {
    const client = await import('./api/netciClient')
    restore = vi.mocked(client.listProductionRequests).getMockImplementation()
    vi.mocked(client.listProductionRequests).mockResolvedValue([waiting] as never)
  })
  afterEach(async () => {
    const client = await import('./api/netciClient')
    vi.mocked(client.listProductionRequests).mockReset()
    if (restore) vi.mocked(client.listProductionRequests).mockImplementation(restore as never)
  })

  async function openDetailsAs(subject: string) {
    const client = await import('./api/netciClient')
    // The server decides who we are; the Portal only reflects it.
    vi.mocked(client.whoami).mockResolvedValue({
      principal: { subject, displayName: subject, email: '', roles: ['reviewer'], teams: [], method: 'oidc' },
      authMode: 'oidc',
      separationOfDuties: true,
    } as never)

    render(
      <PortalFeedbackProvider>
        <ProductionRequestsPage systemId="" />
      </PortalFeedbackProvider>
    )
    fireEvent.click(await screen.findByLabelText(/^Xem /))
    return screen.findByTestId('request-approve')
  }

  it('disables Approve for the person who raised the request, and says why', async () => {
    const approve = await openDetailsAs('alice')
    await waitFor(() => expect((approve as HTMLButtonElement).disabled).toBe(true))
    expect(approve.getAttribute('title')).toContain('different reviewer')
  })

  it('leaves Approve enabled for a different reviewer', async () => {
    const approve = await openDetailsAs('rae')
    await waitFor(() => expect((approve as HTMLButtonElement).disabled).toBe(false))
  })
})
