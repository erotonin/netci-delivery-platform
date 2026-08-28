import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    getStageCatalog: vi.fn().mockResolvedValue({ stages: [{ id: 'verify', name: 'Verify', category: 'verify', enabledByDefault: false }], templates: [] }),
    deleteModule: vi.fn().mockResolvedValue(undefined),
  }
})

import { PortalFeedbackProvider } from './PortalFeedback'
import { ModuleSettings } from './ModuleSettings'
import { deleteModule } from './api/netciClient'

describe('ModuleSettings', () => {
  it('persists General edits under the explicit preview key', async () => {
    const user = userEvent.setup()
    render(<PortalFeedbackProvider><ModuleSettings systemId="netChat" moduleId="backend-api" onClose={vi.fn()} /></PortalFeedbackProvider>)

    const name = screen.getByRole('textbox', { name: 'Display name' })
    await user.clear(name)
    await user.type(name, 'Backend API Preview')
    await user.click(screen.getByRole('button', { name: /Save changes/i }))

    // These edits are still browser-local: the key names that explicitly, so a reviewer
    // can tell at a glance which panels are backed by the API and which are not.
    const stored = JSON.parse(window.sessionStorage.getItem('netci.preview.settings.backend-api.general') ?? '{}')
    expect(stored.name).toBe('Backend API Preview')
    expect(screen.getAllByText(/Đã lưu cấu hình General/i).length).toBeGreaterThan(0)
  })

  it('removes a module through the API and only after an explicit confirmation', async () => {
    const user = userEvent.setup()
    const onDeleted = vi.fn()
    render(<PortalFeedbackProvider><ModuleSettings systemId="netChat" moduleId="backend-api" onClose={vi.fn()} onDeleted={onDeleted} /></PortalFeedbackProvider>)

    await user.click(screen.getByRole('button', { name: /^Remove module$/i }))
    // Opening the dialog must not delete anything on its own.
    expect(deleteModule).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: /Confirm Remove/i }))
    expect(deleteModule).toHaveBeenCalledWith('backend-api')
    await screen.findByText(/Đã xóa module backend-api/i)
    expect(onDeleted).toHaveBeenCalled()
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
