import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { ProductionRequest } from './api/netciClient'

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
})
