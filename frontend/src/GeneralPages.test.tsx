import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    getPortalDashboard: vi.fn().mockResolvedValue({
      kpis: { systems: 3, modules: 6, pipelineRuns: 2, successRate: 50, failureRate: 50 },
      pipelineActivity: [{ date: '2026-08-27', succeeded: 1, failed: 1 }],
      systems: [{ id: 'netChat', unit: 'Platform', description: 'Chat', owner: 'Admin', status: 'healthy', moduleCount: 3, pipelineRuns: 2, failedRuns: 1, modules: [] }],
    }),
  }
})

import { DashboardPage } from './GeneralPages'

describe('DashboardPage', () => {
  it('replaces artifact fallback values with the API projection', async () => {
    render(<DashboardPage navigate={vi.fn()} />)

    expect(await screen.findByText(/6 modules?/)).toBeTruthy()
    expect(screen.getByText('2', { selector: '.kpi-card strong' })).toBeTruthy()
    expect(screen.getByText('3 modules')).toBeTruthy()
  })
})
