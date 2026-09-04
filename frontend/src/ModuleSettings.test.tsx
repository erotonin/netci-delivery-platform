import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    getStageCatalog: vi.fn().mockResolvedValue({ stages: [{ id: 'verify', name: 'Verify', category: 'verify', enabledByDefault: false }], templates: [] }),
    getModule: vi.fn().mockResolvedValue({ id: 'backend-api', name: 'Backend API', type: 'Backend', description: 'API', runtime: 'docker', versions: [], pipelineRuns: [], pipelineConfig: {} }),
    updateModule: vi.fn().mockResolvedValue({ id: 'backend-api' }),
    listAuditEvents: vi.fn().mockResolvedValue([]),
    deleteModule: vi.fn().mockResolvedValue(undefined),
  }
})

import { PortalFeedbackProvider } from './PortalFeedback'
import { ModuleSettings } from './ModuleSettings'
import { deleteModule, updateModule } from './api/netciClient'

describe('ModuleSettings', () => {
  it('persists General edits through the API', async () => {
    const user = userEvent.setup()
    render(<PortalFeedbackProvider><ModuleSettings systemId="netChat" moduleId="backend-api" onClose={vi.fn()} /></PortalFeedbackProvider>)

    const name = await screen.findByDisplayValue('Backend API')
    await user.clear(name)
    await user.type(name, 'Backend API Preview')
    await user.click(screen.getByRole('button', { name: /Save changes/i }))

    expect(updateModule).toHaveBeenCalledWith('backend-api', {
      displayName: 'Backend API Preview',
      moduleType: 'Backend',
      description: 'API',
    })
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

  it('does not expose browser-only access settings as production controls', async () => {
    render(<PortalFeedbackProvider><ModuleSettings systemId="netChat" moduleId="backend-api" onClose={vi.fn()} /></PortalFeedbackProvider>)

    expect(screen.queryByRole('button', { name: /Team & Access/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /Pipeline Access/i })).toBeNull()
  })
})
