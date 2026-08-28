import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { DemoBadge, DemoNote } from './DemoMode'

describe('demo mode labels', () => {
  it('says which panels are fixtures rather than letting them read as features', () => {
    // A demo that reads as a working feature is worse than a missing feature: nobody
    // files a bug against it.
    render(<DemoBadge reason="fixture" />)
    expect(screen.getByRole('note').textContent).toMatch(/Dữ liệu mẫu/i)
    expect(screen.getByRole('note').getAttribute('title')).toMatch(/chưa có endpoint/i)
  })

  it('distinguishes "not implemented" from "not saved"', () => {
    // Two different promises to the reader: one says the feature does not exist yet, the
    // other says their edit is real but local. Collapsing them misleads in both directions.
    render(<DemoNote reason="browser-only" />)
    expect(screen.getByRole('note').textContent).toMatch(/mất khi tải lại/i)
  })

  it('lets a panel explain its own case', () => {
    render(<DemoNote reason="fixture" detail="Audit trail thật nằm ở GET /audit-events." />)
    expect(screen.getByRole('note').textContent).toContain('GET /audit-events')
  })
})
