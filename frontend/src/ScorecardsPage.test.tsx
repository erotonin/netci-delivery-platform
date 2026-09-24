import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import type { ScorecardListItem } from './api/netciClient'

const { mockItems } = vi.hoisted(() => {
  const items: ScorecardListItem[] = [
    {
      moduleId: 'payments-api',
      systemId: 'payments',
      name: 'Payments API',
      score: { passed: 4, known: 5, total: 6 },
    },
    {
      moduleId: 'auth-service',
      systemId: 'core',
      name: 'Auth Service',
      score: { passed: 0, known: 0, total: 3 },
    },
  ]
  return { mockItems: items }
})

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    listScorecards: vi.fn().mockResolvedValue(mockItems),
  }
})

import { listScorecards } from './api/netciClient'
import { ScorecardsPage } from './ScorecardsPage'

describe('ScorecardsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(listScorecards).mockResolvedValue(mockItems)
  })

  it('renders one row per module with its score', async () => {
    render(<ScorecardsPage navigate={vi.fn()} />)

    expect(await screen.findByTestId('scorecards-table')).toBeTruthy()
    expect(screen.getByText('Payments API')).toBeTruthy()
    expect(screen.getByText('payments')).toBeTruthy()
    expect(screen.getByText('4/5 (6)')).toBeTruthy()

    expect(screen.getByText('Auth Service')).toBeTruthy()
    expect(screen.getByText('core')).toBeTruthy()
    expect(screen.getByText('0/0 (3)')).toBeTruthy()
  })

  it('clicking a row navigates to that module', async () => {
    const navigate = vi.fn()
    render(<ScorecardsPage navigate={navigate} />)

    await screen.findByTestId('scorecards-table')
    fireEvent.click(screen.getByText('Payments API'))

    expect(navigate).toHaveBeenCalledWith('module', { systemId: 'payments', moduleId: 'payments-api' })
  })

  it('an empty list is shown plainly', async () => {
    vi.mocked(listScorecards).mockResolvedValueOnce([])
    render(<ScorecardsPage navigate={vi.fn()} />)

    expect(await screen.findByText('No module has a scorecard yet')).toBeTruthy()
  })

  it('a module with no known checks reads "0/0", never as a pass', async () => {
    render(<ScorecardsPage navigate={vi.fn()} />)

    // auth-service has 0 known checks: the ratio must read "0/0", never look like a pass,
    // and its bar must not read as filled.
    expect(await screen.findByText('0/0 (3)')).toBeTruthy()
  })

  it('shows an error message when the list fails to load', async () => {
    vi.mocked(listScorecards).mockRejectedValueOnce(new Error('scorecards unavailable: 500'))
    render(<ScorecardsPage navigate={vi.fn()} />)

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain('scorecards unavailable: 500')
  })
})
