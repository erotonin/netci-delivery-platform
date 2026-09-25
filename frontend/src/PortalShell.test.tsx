import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import type { AuthSession } from './LoginPage'

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return { ...original, listSystems: vi.fn().mockResolvedValue([]) }
})

import { PortalShell } from './PortalShell'

function session(kind?: string): AuthSession {
  return {
    token: 't',
    identity: {
      principal: { subject: 'bot-1', displayName: 'Build Bot', email: 'bot@example.test', roles: ['developer'], teams: [], method: 'oidc', ...(kind === undefined ? {} : { kind }) },
      authMode: 'oidc',
      separationOfDuties: true,
    },
  }
}

function renderShell(value: AuthSession, navigate = vi.fn()) {
  render(
    <PortalShell page="dashboard" systemId="" moduleId="" session={value} navigate={navigate} onSettings={vi.fn()} onLogout={vi.fn()}>
      <div />
    </PortalShell>,
  )
  return navigate
}

describe('PortalShell principal kind (ADR-052)', () => {
  it('shows an agent badge for an agent', async () => {
    renderShell(session('agent'))
    // Sidebar and top bar both name the signed-in principal.
    expect((await screen.findAllByTestId('agent-badge')).length).toBe(2)
  })

  it('shows nothing for a human', () => {
    renderShell(session('human'))
    expect(screen.queryByTestId('agent-badge')).toBeNull()
  })

  it('shows nothing when the server sent no kind', () => {
    renderShell(session())
    expect(screen.queryByTestId('agent-badge')).toBeNull()
  })
})

describe('PortalShell navigation', () => {
  it('has a Production Requests entry', () => {
    const navigate = renderShell(session('human'))
    fireEvent.click(screen.getByRole('button', { name: 'Production Requests' }))
    expect(navigate).toHaveBeenCalledWith('requests', undefined)
  })
})
