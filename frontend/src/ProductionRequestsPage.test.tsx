import { describe, expect, it, vi } from 'vitest'
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
      message: 'Canary advanced',
      deploymentId: 'dep-1234',
      step: 3,
      trafficWeight: 50,
    }),
    abortCanary: vi.fn().mockResolvedValue({
      message: 'Canary aborted',
      deploymentId: 'dep-1234',
      rolledBack: true,
    }),
    listSystems: vi.fn().mockResolvedValue([]),
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
})
