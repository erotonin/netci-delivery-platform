import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return { ...original, simulateReleasePlan: vi.fn() }
})

import { NetciApiError, simulateReleasePlan } from './api/netciClient'
import { ReleasePlanPage } from './ReleasePlanPage'

describe('ReleasePlanPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the waves the API returned as columns', async () => {
    vi.mocked(simulateReleasePlan).mockResolvedValue({
      valid: true,
      waves: [['db-migration', 'redis'], ['auth', 'payment'], ['api-gateway', 'frontend']],
      order: ['db-migration', 'redis', 'auth', 'payment', 'api-gateway', 'frontend'],
      groupedBy: 'dependencies',
    })
    render(<ReleasePlanPage />)

    fireEvent.click(screen.getByRole('button', { name: 'Valid plan' }))

    const panel = await screen.findByTestId('release-plan-waves')
    expect(within(panel).getByText('3 waves')).toBeTruthy()
    const wave1 = within(panel).getByLabelText('Wave 1')
    expect(within(wave1).getByText('db-migration')).toBeTruthy()
    expect(within(wave1).getByText('redis')).toBeTruthy()
    const wave2 = within(panel).getByLabelText('Wave 2')
    expect(within(wave2).getByText('auth')).toBeTruthy()
    expect(within(wave2).getByText('depends on db-migration, redis')).toBeTruthy()
    const wave3 = within(panel).getByLabelText('Wave 3')
    expect(within(wave3).getByText('frontend')).toBeTruthy()
    expect(within(wave3).getByText('depends on auth, payment')).toBeTruthy()
    expect(screen.queryByTestId('release-plan-blocked')).toBeNull()

    // The page sends the example's modules and parsed dependencies, nothing else.
    expect(simulateReleasePlan).toHaveBeenCalledWith([
      { moduleId: 'db-migration', dependencies: [] },
      { moduleId: 'redis', dependencies: [] },
      { moduleId: 'auth', dependencies: ['db-migration', 'redis'] },
      { moduleId: 'payment', dependencies: ['db-migration'] },
      { moduleId: 'frontend', dependencies: ['auth', 'payment'] },
      { moduleId: 'api-gateway', dependencies: ['auth'] },
    ])
  })

  it('renders the blocked panel with the cycle path for an invalid plan', async () => {
    vi.mocked(simulateReleasePlan).mockResolvedValue({
      valid: false,
      code: 'CYCLIC_DEPENDENCY',
      message: 'Cyclic dependency detected in module release graph',
      cycle: ['a', 'b', 'c'],
    })
    render(<ReleasePlanPage />)

    fireEvent.click(screen.getByRole('button', { name: 'Cycle' }))

    const panel = await screen.findByTestId('release-plan-blocked')
    expect(within(panel).getByText('Cyclic dependency detected -- release blocked')).toBeTruthy()
    expect(within(panel).getByText('a → b → c → a')).toBeTruthy()
    expect(screen.queryByTestId('release-plan-waves')).toBeNull()
    expect(simulateReleasePlan).toHaveBeenCalledWith([
      { moduleId: 'a', dependencies: ['b'] },
      { moduleId: 'b', dependencies: ['c'] },
      { moduleId: 'c', dependencies: ['a'] },
    ])
  })

  it('names a refusal that is not a cycle without inventing a loop', async () => {
    vi.mocked(simulateReleasePlan).mockResolvedValue({
      valid: false,
      code: 'INVALID_DEPENDENCY',
      message: 'Module auth depends on unknown module db not in this request',
      cycle: null,
    })
    render(<ReleasePlanPage />)

    fireEvent.click(screen.getByRole('button', { name: 'Simulate' }))

    const panel = await screen.findByTestId('release-plan-blocked')
    expect(within(panel).getByText('Plan refused -- release blocked')).toBeTruthy()
    expect(within(panel).getByText('INVALID_DEPENDENCY')).toBeTruthy()
    expect(within(panel).queryByText(/→/)).toBeNull()
  })

  it('shows a request error instead of a result', async () => {
    vi.mocked(simulateReleasePlan).mockRejectedValue(
      new NetciApiError(422, { code: 'VALIDATION_ERROR', message: 'request validation failed' }, ''),
    )
    render(<ReleasePlanPage />)

    fireEvent.click(screen.getByRole('button', { name: 'Simulate' }))

    expect((await screen.findByRole('alert')).textContent).toContain('request validation failed')
    expect(screen.queryByTestId('release-plan-waves')).toBeNull()
  })
})
