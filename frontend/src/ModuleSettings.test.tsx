import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    getStageCatalog: vi.fn().mockResolvedValue({
      stages: [
        { id: 'checkout', name: 'Checkout source', category: 'source', kind: 'builtin', required: true, enabledByDefault: true, position: 10 },
        { id: 'unit-test', name: 'Unit tests', category: 'test', kind: 'builtin', required: false, enabledByDefault: true, position: 20 },
        { id: 'lint', name: 'Lint', category: 'custom', kind: 'custom', script: 'ci/lint.sh', afterStage: 'unit-test', required: false, enabledByDefault: false, position: 25, status: 'active', parameters: [{ name: 'LEVEL', default: 'basic' }] },
        { id: 'build', name: 'Build artifact/image', category: 'build', kind: 'builtin', required: true, enabledByDefault: true, position: 30 },
      ],
      templates: [{ id: 'container-ci-cd-v1', name: 'container-ci-cd-v1', runtime: 'docker', stageIds: ['checkout', 'unit-test', 'build'] }],
    }),
    getModuleStages: vi.fn().mockResolvedValue({ moduleId: 'backend-api', applicationId: 'a1', stages: ['checkout', 'unit-test', 'build'], pipelineTemplate: 'container-ci-cd-v1' }),
    setModuleStages: vi.fn().mockImplementation(async (_id: string, stages: string[], stageParameters = {}) => ({ moduleId: 'backend-api', applicationId: 'a1', stages, stageParameters })),
    whoami: vi.fn().mockResolvedValue({ principal: { subject: 'dana', displayName: 'Dana', roles: ['developer'], teams: [], method: 'token' }, authMode: 'token', separationOfDuties: true }),
    getModule: vi.fn().mockResolvedValue({ id: 'backend-api', name: 'Backend API', type: 'Backend', description: 'API', runtime: 'docker', versions: [], pipelineRuns: [], pipelineConfig: {} }),
    updateModule: vi.fn().mockResolvedValue({ id: 'backend-api' }),
    listAuditEvents: vi.fn().mockResolvedValue([]),
    deleteModule: vi.fn().mockResolvedValue(undefined),
  }
})

import { PortalFeedbackProvider } from './PortalFeedback'
import { ModuleSettings } from './ModuleSettings'
import { deleteModule, setModuleStages, updateModule } from './api/netciClient'

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
    await screen.findByText(/(?:Deleted module backend-api|Đã xóa module backend-api)/i)
    expect(onDeleted).toHaveBeenCalled()
  })

  it('does not expose browser-only access settings as production controls', async () => {
    render(<PortalFeedbackProvider><ModuleSettings systemId="netChat" moduleId="backend-api" onClose={vi.fn()} /></PortalFeedbackProvider>)

    expect(screen.queryByRole('button', { name: /Team & Access/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /Pipeline Access/i })).toBeNull()
  })

  it('lets a module choose catalog stages, keeps required ones locked, and saves the list', async () => {
    const user = userEvent.setup()
    render(<PortalFeedbackProvider><ModuleSettings systemId="netChat" moduleId="backend-api" onClose={vi.fn()} /></PortalFeedbackProvider>)
    await user.click(await screen.findByRole('button', { name: /Pipeline stages/i }))

    const checkout = await screen.findByRole('checkbox', { name: 'Checkout source' })
    expect((checkout as HTMLInputElement).disabled).toBe(true)
    expect((checkout as HTMLInputElement).checked).toBe(true)
    // Custom stage anchored after unit-test: selectable because unit-test is on.
    await user.click(screen.getByRole('checkbox', { name: 'Lint' }))
    expect(screen.getByText(/checkout → unit-test → lint → build/)).toBeTruthy()
    // No admin form for a developer.
    expect(screen.queryByRole('button', { name: /Register stage/i })).toBeNull()

    // The custom stage's declared parameter is editable once the stage is selected.
    const level = screen.getByRole('textbox', { name: 'lint LEVEL' })
    await user.clear(level)
    await user.type(level, 'strict')
    await user.click(screen.getByRole('button', { name: /Save stages/i }))
    expect(setModuleStages).toHaveBeenCalledWith('backend-api', ['checkout', 'unit-test', 'build', 'lint'], { lint: { LEVEL: 'strict' } })
  })
})
