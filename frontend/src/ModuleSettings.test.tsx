import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    getStageCatalog: vi.fn().mockResolvedValue({ stages: [{ id: 'verify', name: 'Verify', category: 'verify', enabledByDefault: false }], templates: [] }),
  }
})

import { PortalFeedbackProvider } from './PortalFeedback'
import { ModuleSettings } from './ModuleSettings'

describe('ModuleSettings', () => {
  it('persists General edits under the explicit Windows preview key', async () => {
    const user = userEvent.setup()
    render(<PortalFeedbackProvider><ModuleSettings systemId="netChat" moduleId="backend-api" onClose={vi.fn()} /></PortalFeedbackProvider>)

    const name = screen.getByRole('textbox', { name: 'Display name' })
    await user.clear(name)
    await user.type(name, 'Backend API Preview')
    await user.click(screen.getByRole('button', { name: /Save changes/i }))

    const stored = JSON.parse(window.sessionStorage.getItem('netci.preview.settings.backend-api.general') ?? '{}')
    expect(stored.name).toBe('Backend API Preview')
    expect(screen.getAllByText(/Windows preview/i).length).toBeGreaterThan(0)
  })

  it('adds a team member and keeps the access action functional', async () => {
    const user = userEvent.setup()
    render(<PortalFeedbackProvider><ModuleSettings systemId="netChat" moduleId="backend-api" onClose={vi.fn()} /></PortalFeedbackProvider>)

    await user.click(screen.getByRole('button', { name: /Team & Access/i }))
    await user.click(screen.getByRole('button', { name: /Add member/i }))
    const dialog = screen.getByRole('dialog')
    await user.type(within(dialog).getByRole('textbox', { name: 'Name' }), 'Mentor VDT')
    await user.type(within(dialog).getByRole('textbox', { name: 'Email' }), 'mentor@example.net')
    await user.click(within(dialog).getByRole('button', { name: /^Add member$/i }))

    expect(screen.getByText('mentor@example.net')).toBeTruthy()
    const stored = JSON.parse(window.sessionStorage.getItem('netci.preview.settings.backend-api.team') ?? '[]')
    expect(stored.some((member: { email: string }) => member.email === 'mentor@example.net')).toBe(true)
  })
})
