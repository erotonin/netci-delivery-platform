import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import type { CiCost, CiCostCounters } from './api/netciClient'

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return { ...original, getCiCost: vi.fn() }
})

import { getCiCost } from './api/netciClient'
import { CiCostPage, NO_ESTIMATE, formatSeconds } from './CiCostPage'

const METHOD =
  "superseded-before-admission runs x median runner seconds of the application's succeeded runs in the window"

function counters(overrides: Partial<CiCostCounters> = {}): CiCostCounters {
  return {
    runs: 12,
    succeeded: 2,
    failed: 0,
    cancelled: 10,
    superseded: 10,
    supersededBeforeAdmission: 9,
    supersededWhileBuilding: 1,
    ciSeconds: 3725,
    runsWithoutCiTiming: 0,
    stageSeconds: 1800,
    stagesWithoutDuration: 0,
    queueSeconds: 90,
    p50QueueSeconds: 4,
    p95QueueSeconds: 44.9,
    estimatedAvoidedRunnerSeconds: 1800,
    cost: null,
    ...overrides,
  }
}

function response(overrides: Partial<CiCost> = {}): CiCost {
  return {
    window: { days: 30, from: '2026-08-26T00:00:00+00:00', to: '2026-09-25T00:00:00+00:00' },
    method: { ciSeconds: 'dispatch to Jenkins until the run left CI', stageSeconds: 'sum of recorded stage durations', estimatedAvoidedRunnerSeconds: METHOD },
    applications: [
      { ...counters(), applicationId: 'app-payments', name: 'payments-api' },
      {
        ...counters({ runs: 3, succeeded: 0, failed: 3, cancelled: 0, superseded: 0, supersededBeforeAdmission: 0, supersededWhileBuilding: 0, ciSeconds: 42, estimatedAvoidedRunnerSeconds: null, p50QueueSeconds: null, p95QueueSeconds: null }),
        applicationId: 'app-ledger',
        name: 'ledger',
      },
    ],
    total: { ...counters({ runs: 15, ciSeconds: 3767 }), estimatedAvoidedIncomplete: false },
    ...overrides,
  }
}

describe('CiCostPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(getCiCost).mockResolvedValue(response())
  })

  it('formats seconds as h/m/s and keeps a sub-minute tenth', () => {
    expect(formatSeconds(3725)).toBe('1h 2m 5s')
    expect(formatSeconds(125)).toBe('2m 5s')
    expect(formatSeconds(44.9)).toBe('44.9s')
    expect(formatSeconds(0.4)).toBe('0.4s')
  })

  it('asks for 30 days by default and refetches for the chosen window', async () => {
    render(<CiCostPage />)
    await screen.findByTestId('ci-cost-table')
    expect(getCiCost).toHaveBeenCalledWith(30)

    fireEvent.click(screen.getByRole('button', { name: '7 days' }))
    await screen.findByTestId('ci-cost-table')
    expect(getCiCost).toHaveBeenLastCalledWith(7)

    fireEvent.click(screen.getByRole('button', { name: '90 days' }))
    await screen.findByTestId('ci-cost-table')
    expect(getCiCost).toHaveBeenLastCalledWith(90)
  })

  it('labels runner time measured and the avoided time as an estimate, with the method verbatim', async () => {
    render(<CiCostPage />)
    const row = await screen.findByTestId('ci-cost-row-app-payments')

    const runner = within(row).getByText(/1h 2m 5s/)
    expect(within(runner).getByText('measured')).toBeTruthy()

    const estimate = within(row).getByText(/≈ 30m 0s/)
    expect(estimate.className).toContain('estimated-value')
    expect(estimate.getAttribute('title')).toBe(METHOD)
    const tag = within(estimate).getByText('estimate')
    expect(tag.getAttribute('title')).toBe(METHOD)
    // The measured cell never carries the estimate's marks.
    expect(runner.textContent).not.toContain('≈')

    expect(screen.getByTestId('ci-cost-method').textContent).toContain(METHOD)
  })

  it('splits superseded into never-reached-CI and while-building', async () => {
    render(<CiCostPage />)
    const row = await screen.findByTestId('ci-cost-row-app-payments')
    expect(within(row).getByText('before admission -- never reached CI: 9')).toBeTruthy()
    expect(within(row).getByText('while building: 1')).toBeTruthy()
    expect(within(row).getByText('4s / 44.9s')).toBeTruthy()
  })

  it('a null estimate says why, never 0', async () => {
    render(<CiCostPage />)
    const row = await screen.findByTestId('ci-cost-row-app-ledger')
    expect(within(row).getByText(NO_ESTIMATE)).toBeTruthy()
    expect(within(row).queryByText(/≈/)).toBeNull()
    // No admitted run: the queue percentiles are missing, not a zero wait.
    expect(within(row).getByText('no sample / no sample')).toBeTruthy()
  })

  it('a null total estimate also says why', async () => {
    vi.mocked(getCiCost).mockResolvedValueOnce(response({
      total: { ...counters({ estimatedAvoidedRunnerSeconds: null }), estimatedAvoidedIncomplete: false },
    }))
    render(<CiCostPage />)
    const total = await screen.findByTestId('ci-cost-total')
    expect(within(total).getByText(NO_ESTIMATE)).toBeTruthy()
  })

  it('warns when the total estimate leaves applications out, and only then', async () => {
    const { unmount } = render(<CiCostPage />)
    await screen.findByTestId('ci-cost-table')
    expect(screen.queryByTestId('ci-cost-incomplete')).toBeNull()
    unmount()

    vi.mocked(getCiCost).mockResolvedValueOnce(response({
      total: { ...counters(), estimatedAvoidedIncomplete: true },
    }))
    render(<CiCostPage />)
    const warning = await screen.findByTestId('ci-cost-incomplete')
    expect(warning.textContent).toContain('leaves some applications out')
  })

  it('without a price there is no cost column and no 0, only how to configure it', async () => {
    render(<CiCostPage />)
    await screen.findByTestId('ci-cost-table')
    expect(screen.getByTestId('ci-cost-no-price').textContent).toBe('Set NETCI_CI_PRICE_PER_RUNNER_HOUR to show cost.')
    expect(screen.queryByRole('columnheader', { name: 'Cost' })).toBeNull()
    expect(screen.queryByText(/USD|EUR|0\.00/)).toBeNull()
  })

  it('with a price shows cost with its currency, the avoided part as an estimate', async () => {
    const priced = { currency: 'EUR', measured: '3.10', estimatedAvoided: '1.50' }
    const data = response()
    data.applications[0] = { ...data.applications[0], cost: priced }
    data.applications[1] = { ...data.applications[1], cost: { currency: 'EUR', measured: '0.04', estimatedAvoided: null } }
    data.total = { ...data.total, cost: { currency: 'EUR', measured: '3.14', estimatedAvoided: '1.50' } }
    vi.mocked(getCiCost).mockResolvedValueOnce(data)

    render(<CiCostPage />)
    await screen.findByTestId('ci-cost-table')
    expect(screen.getByRole('columnheader', { name: 'Cost' })).toBeTruthy()
    expect(screen.queryByTestId('ci-cost-no-price')).toBeNull()

    const row = screen.getByTestId('ci-cost-row-app-payments')
    expect(within(row).getByText(/3\.10 EUR/)).toBeTruthy()
    const avoided = within(row).getByText(/avoided ≈ 1\.50 EUR/)
    expect(avoided.className).toContain('estimated-value')

    const ledger = screen.getByTestId('ci-cost-row-app-ledger')
    expect(within(ledger).getByText(`avoided: ${NO_ESTIMATE}`)).toBeTruthy()

    expect(within(screen.getByTestId('ci-cost-total')).getByText(/3\.14 EUR/)).toBeTruthy()
  })

  it('notes stages without a duration, and only when there are some', async () => {
    const { unmount } = render(<CiCostPage />)
    await screen.findByTestId('ci-cost-table')
    expect(screen.queryByTestId('ci-cost-stages-without-duration')).toBeNull()
    unmount()

    vi.mocked(getCiCost).mockResolvedValueOnce(response({
      total: { ...counters({ stagesWithoutDuration: 3 }), estimatedAvoidedIncomplete: false },
    }))
    render(<CiCostPage />)
    const note = await screen.findByTestId('ci-cost-stages-without-duration')
    expect(note.textContent).toContain('3 stage(s) have no recorded duration and are not counted')
  })

  it('an empty window is shown plainly', async () => {
    vi.mocked(getCiCost).mockResolvedValueOnce(response({ applications: [] }))
    render(<CiCostPage />)
    expect(await screen.findByText('No application you can see has a run in this window')).toBeTruthy()
  })

  it('shows an error when the projection fails to load', async () => {
    vi.mocked(getCiCost).mockRejectedValueOnce(new Error('finops unavailable: 500'))
    render(<CiCostPage />)
    expect((await screen.findByRole('alert')).textContent).toContain('finops unavailable: 500')
  })
})
