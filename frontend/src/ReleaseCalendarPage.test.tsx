import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import type { ChangeFreeze, ProductionRequest } from './api/netciClient'

// Hoisted with vi.mock, which runs before anything else in the file.
const { request } = vi.hoisted(() => ({
  request: (id: string, moduleId: string, scheduledFor: string, status = 'waiting_approval') => ({
    id, requestedBy: 'dana', scheduledFor, status, rollbackStrategy: 'automatic', runAutomationTests: true,
    modules: [{ moduleId, moduleName: moduleId, version: 'v1.0.0', deploymentOrder: 1 }],
  } as unknown as ProductionRequest),
}))

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    listProductionRequests: vi.fn().mockResolvedValue([
      request('r1', 'payments-api', '2026-09-24T09:00:00+07:00'),
      request('r2', 'payments-api', '2026-09-24T15:00:00+07:00'),
      request('r3', 'shop-api', '2026-09-25T09:00:00+07:00', 'approved'),
    ]),
    listServersMaintenance: vi.fn().mockResolvedValue([
      { serverName: 'shop-prod-01', inMaintenance: true, reason: 'disk replacement', updatedBy: 'pat', updatedAt: '2026-09-23T00:00:00Z' },
    ]),
    listChangeFreezes: vi.fn().mockResolvedValue([]),
    createChangeFreeze: vi.fn(),
    cancelChangeFreeze: vi.fn(),
    getModule: vi.fn().mockImplementation(async (id: string) => ({
      id, deploymentEnvironments: [{ environment: 'prod', servers: id === 'shop-api' ? ['shop-prod-01'] : ['pay-prod-01'] }],
    })),
  }
})

import { ReleaseCalendarPage, calendarWarnings } from './ReleaseCalendarPage'

describe('release calendar warnings, from records netCI holds', () => {
  const now = new Date('2026-09-23T12:00:00+07:00')

  it('flags two releases of one module on the same day', () => {
    const w = calendarWarnings([request('a', 'm', '2026-09-24T09:00:00+07:00'), request('b', 'm', '2026-09-24T18:00:00+07:00')], [], {}, now)
    expect(w.filter((x) => x.kind === 'same-day').map((x) => x.requestId).sort()).toEqual(['a', 'b'])
  })

  it('flags a production target that is in maintenance now, and names the reason', () => {
    const w = calendarWarnings([request('a', 'm', '2026-09-24T09:00:00+07:00')],
      [{ serverName: 'h1', inMaintenance: true, reason: 'kernel patch', updatedBy: 'pat', updatedAt: '' }], { m: ['h1'] }, now)
    expect(w).toEqual([expect.objectContaining({ kind: 'maintenance', message: expect.stringContaining('kernel patch') })])
  })

  it('flags a release whose time has passed while it still waits for approval', () => {
    const w = calendarWarnings([request('a', 'm', '2026-09-22T09:00:00+07:00')], [], {}, now)
    expect(w.map((x) => x.kind)).toEqual(['overdue'])
  })

  it('does not warn about releases that are already settled', () => {
    const settled = [request('a', 'm', '2026-09-24T09:00:00+07:00', 'succeeded'), request('b', 'm', '2026-09-24T10:00:00+07:00', 'cancelled')]
    expect(calendarWarnings(settled, [], {}, now)).toEqual([])
  })

  it('calendarWarnings flags requests inside [startsAt, endsAt) for prod freezes, not after endsAt or staging-only', () => {
    const makeFreeze = (id: string, startsAt: string, endsAt: string, environments: string[] = ['prod']): ChangeFreeze => ({
      id,
      name: 'Q3 Freeze',
      startsAt,
      endsAt,
      environments,
      systemId: null,
      moduleId: null,
      reason: 'Quarter-end freeze',
      createdBy: 'dana',
      createdAt: '2026-09-20T00:00:00Z',
      cancelledAt: null,
      cancelledBy: null,
    })

    const freeze = makeFreeze('f1', '2026-09-24T08:00:00+07:00', '2026-09-24T18:00:00+07:00')
    const reqInside = [request('r-in', 'm', '2026-09-24T12:00:00+07:00')]

    // inside -> warning
    const wInside = calendarWarnings(reqInside, [], {}, now, [freeze])
    expect(wInside).toEqual([
      expect.objectContaining({
        requestId: 'r-in',
        kind: 'freeze',
        message: `scheduled inside the freeze "Q3 Freeze" (until ${new Date(freeze.endsAt).toLocaleTimeString()})`,
      }),
    ])

    // after endsAt -> none
    const reqAfter = [request('r-after', 'm', '2026-09-24T19:00:00+07:00')]
    const wAfter = calendarWarnings(reqAfter, [], {}, now, [freeze])
    expect(wAfter.filter((w) => w.kind === 'freeze')).toEqual([])

    // staging-only freeze -> none
    const freezeStaging = makeFreeze('f-stage', '2026-09-24T08:00:00+07:00', '2026-09-24T18:00:00+07:00', ['staging'])
    const wStage = calendarWarnings(reqInside, [], {}, now, [freezeStaging])
    expect(wStage.filter((w) => w.kind === 'freeze')).toEqual([])
  })
})

describe('ReleaseCalendarPage', () => {
  it('shows scheduled releases by day with their conflicts, and says what it cannot know', async () => {
    render(<ReleaseCalendarPage now={new Date('2026-09-23T12:00:00+07:00')} />)
    expect(await screen.findByTestId('calendar-summary')).toBeTruthy()
    expect(screen.getByText(/3 release\(s\) need attention/)).toBeTruthy()
    expect(screen.getAllByText(/payments-api is in 2 releases scheduled for the same day/)).toHaveLength(2)
    expect(screen.getByText(/shop-prod-01 is in maintenance now \(disk replacement\)/)).toBeTruthy()
    expect(screen.getByText(/not future maintenance windows/)).toBeTruthy()
  })

  it('renders a freeze on the days it covers', async () => {
    const { listChangeFreezes } = await import('./api/netciClient')
    const freeze: ChangeFreeze = {
      id: 'freeze-oct',
      name: 'Maintenance Freeze',
      startsAt: '2026-09-24T10:00:00+07:00',
      endsAt: '2026-09-25T18:00:00+07:00',
      environments: ['prod', 'staging'],
      systemId: null,
      moduleId: null,
      reason: 'Core switch migration',
      createdBy: 'pat',
      createdAt: '2026-09-23T00:00:00Z',
      cancelledAt: null,
      cancelledBy: null,
    }
    vi.mocked(listChangeFreezes).mockResolvedValueOnce([freeze])

    render(<ReleaseCalendarPage now={new Date('2026-09-23T12:00:00+07:00')} />)
    expect(await screen.findByTestId('calendar-summary')).toBeTruthy()

    const day1 = screen.getByTestId('calendar-day-2026-09-24')
    const day2 = screen.getByTestId('calendar-day-2026-09-25')

    expect(day1.querySelector('[data-testid="calendar-freeze-freeze-oct"]')).toBeTruthy()
    expect(day2.querySelector('[data-testid="calendar-freeze-freeze-oct"]')).toBeTruthy()

    const freezeCards = screen.getAllByTestId('calendar-freeze-freeze-oct')
    expect(freezeCards).toHaveLength(2)
    expect(freezeCards[0].textContent).toContain('Maintenance Freeze')
    expect(freezeCards[0].textContent).toContain('prod, staging')
    expect(freezeCards[0].textContent).toContain('all modules')
    expect(freezeCards[0].textContent).toContain('Core switch migration')
    expect(freezeCards[0].querySelector('button')?.textContent).toContain('Cancel freeze')
  })

  it('shows a freeze warning for a request scheduled inside a prod freeze', async () => {
    const { listChangeFreezes } = await import('./api/netciClient')
    const freeze: ChangeFreeze = {
      id: 'freeze-prod',
      name: 'Emergency Freeze',
      startsAt: '2026-09-24T08:00:00+07:00',
      endsAt: '2026-09-24T12:00:00+07:00',
      environments: ['prod'],
      systemId: null,
      moduleId: null,
      reason: 'Major incident',
      createdBy: 'ops',
      createdAt: '2026-09-24T08:00:00Z',
      cancelledAt: null,
      cancelledBy: null,
    }
    vi.mocked(listChangeFreezes).mockResolvedValueOnce([freeze])

    render(<ReleaseCalendarPage now={new Date('2026-09-23T12:00:00+07:00')} />)
    expect(await screen.findByTestId('calendar-summary')).toBeTruthy()
    expect(
      await screen.findByText(new RegExp(`scheduled inside the freeze "Emergency Freeze" \\(until ${new Date(freeze.endsAt).toLocaleTimeString()}\\)`))
    ).toBeTruthy()
  })

  it('shows the error in role="alert" on a failed create', async () => {
    const { createChangeFreeze } = await import('./api/netciClient')
    vi.mocked(createChangeFreeze).mockRejectedValueOnce(new Error('Reviewer permission required'))

    render(<ReleaseCalendarPage now={new Date('2026-09-23T12:00:00+07:00')} />)
    expect(await screen.findByTestId('calendar-summary')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /new freeze/i }))
    expect(await screen.findByTestId('freeze-form')).toBeTruthy()

    fireEvent.change(screen.getByLabelText(/name/i), { target: { value: 'Test Freeze' } })
    fireEvent.change(screen.getByLabelText(/start/i), { target: { value: '2026-09-24T10:00' } })
    fireEvent.change(screen.getByLabelText(/end/i), { target: { value: '2026-09-24T18:00' } })
    fireEvent.change(screen.getByLabelText(/reason/i), { target: { value: 'Test reason' } })

    fireEvent.click(screen.getByRole('button', { name: /create freeze/i }))

    const alert = await screen.findByRole('alert')
    expect(alert).toBeTruthy()
    expect(alert.textContent).toContain('Reviewer permission required')
  })
})

