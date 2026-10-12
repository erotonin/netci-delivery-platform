import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { NetciApiError, type StageCatalog, type StageDefinition } from './api/netciClient'
import type { AuthSession } from './LoginPage'
import { PortalFeedbackProvider } from './PortalFeedback'
import { StageCatalogPage } from './StageCatalogPage'

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    getStageCatalog: vi.fn(),
    registerCustomStage: vi.fn(),
    approveCustomStage: vi.fn(),
    removeCustomStage: vi.fn(),
  }
})

import { approveCustomStage, getStageCatalog, registerCustomStage } from './api/netciClient'

const builtin = (id: string, name: string, category: StageDefinition['category'], position: number, required = false): StageDefinition => ({
  id, name, category, kind: 'builtin', position, required, enabledByDefault: true, description: `${name} description`, status: 'active',
})

const catalog: StageCatalog = {
  templates: [],
  stages: [
    // Deliberately out of pipeline order: the page orders by category, then position.
    builtin('health-check', 'Health check', 'verify', 90),
    builtin('checkout', 'Checkout source', 'source', 10, true),
    builtin('unit-test', 'Unit tests', 'test', 20),
    builtin('build', 'Build artifact/image', 'build', 30, true),
    builtin('sbom', 'Generate SBOM', 'security', 40, true),
    {
      id: 'lint', name: 'Lint', category: 'custom', kind: 'custom', position: 21, required: false, enabledByDefault: false,
      description: 'Lint the sources', script: 'ci/lint.sh', afterStage: 'unit-test', status: 'proposed', createdBy: 'alice',
      approvedBy: null, parameters: [{ name: 'LEVEL', default: 'strict', description: 'lint strictness' }],
    },
  ],
}

const session = (roles: string[], subject = 'bob'): AuthSession => ({
  token: 't',
  identity: { principal: { subject, displayName: subject, email: `${subject}@example.test`, roles, teams: [], method: 'token' }, authMode: 'token', separationOfDuties: true },
})

const renderPage = (who: AuthSession) => render(<PortalFeedbackProvider><StageCatalogPage session={who} /></PortalFeedbackProvider>)

describe('StageCatalogPage', () => {
  beforeEach(() => {
    vi.mocked(getStageCatalog).mockReset().mockResolvedValue(catalog)
    vi.mocked(registerCustomStage).mockReset()
    vi.mocked(approveCustomStage).mockReset()
  })

  it('groups stages by category in pipeline order and locks the required ones', async () => {
    renderPage(session(['viewer']))
    const headings = await screen.findAllByRole('heading', { level: 2 })
    expect(headings.map((h) => h.textContent)).toEqual(['Source', 'Test', 'Build', 'Security', 'Verify', 'Custom'])

    const checkout = screen.getByRole('listitem', { name: 'Checkout source' })
    expect(within(checkout).getByText('Required by policy — cannot be disabled')).toBeTruthy()
    expect(within(checkout).getByText('Built-in')).toBeTruthy()
    const unitTests = screen.getByRole('listitem', { name: 'Unit tests' })
    expect(within(unitTests).queryByText(/Required by policy/)).toBeNull()
    expect(screen.getAllByText('Required by policy — cannot be disabled')).toHaveLength(3)
  })

  it('shows a pending custom stage as not yet usable, with its script, anchor and parameters', async () => {
    renderPage(session(['viewer']))
    const lint = await screen.findByRole('listitem', { name: 'Lint' })
    expect(within(lint).getByText('Custom')).toBeTruthy()
    expect(within(lint).getByText('Pending approval — not yet usable')).toBeTruthy()
    expect(within(lint).getByText('ci/lint.sh')).toBeTruthy()
    expect(within(lint).getByText('Runs after Unit tests')).toBeTruthy()
    expect(within(lint).getByText('LEVEL')).toBeTruthy()
  })

  it('offers a read-only catalog to a non-admin', async () => {
    renderPage(session(['developer']))
    await screen.findByRole('listitem', { name: 'Lint' })
    expect(screen.queryByRole('button', { name: /Register custom stage/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Approve Lint' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Delete Lint' })).toBeNull()
  })

  it('offers register, approve and delete to a platform admin, and approving reloads the catalog', async () => {
    vi.mocked(approveCustomStage).mockResolvedValue({ ...catalog.stages[5], status: 'active', approvedBy: 'bob' })
    renderPage(session(['platform-admin']))
    await screen.findByRole('listitem', { name: 'Lint' })
    expect(screen.getByRole('button', { name: /Register custom stage/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Delete Lint' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Approve Lint' }))
    await waitFor(() => expect(approveCustomStage).toHaveBeenCalledWith('lint'))
    await waitFor(() => expect(getStageCatalog).toHaveBeenCalledTimes(2))
  })

  it('does not offer the proposer the approval separation of duties would refuse', async () => {
    renderPage(session(['platform-admin'], 'alice'))
    await screen.findByRole('listitem', { name: 'Lint' })
    expect(screen.queryByRole('button', { name: 'Approve Lint' })).toBeNull()
    expect(screen.getByText(/a different administrator must approve it/)).toBeTruthy()
  })

  it("shows the server's 422 message when a registration is refused", async () => {
    vi.mocked(registerCustomStage).mockRejectedValue(new NetciApiError(422, {
      detail: { code: 'INVALID_STAGE_SCRIPT', message: "script must be a repository-relative path ending in .sh, without '..' or a leading slash" },
    } as never, 'refused'))
    renderPage(session(['platform-admin']))
    fireEvent.click(await screen.findByRole('button', { name: /Register custom stage/ }))
    fireEvent.change(screen.getByLabelText('Stage id'), { target: { value: 'lint2' } })
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Lint 2' } })
    fireEvent.change(screen.getByLabelText('Script (repository path)'), { target: { value: '/etc/passwd.sh' } })
    // The client-side rule is only a hint: it warns but the server decides.
    expect(screen.getByText(/netCI will most likely refuse it/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Register stage' }))
    await waitFor(() => expect(registerCustomStage).toHaveBeenCalledWith(expect.objectContaining({ id: 'lint2', script: '/etc/passwd.sh', afterStage: 'unit-test', category: 'custom' })))
    expect(await screen.findByText(/script must be a repository-relative path ending in \.sh/)).toBeTruthy()
  })

  it('shows a load failure instead of an empty catalog', async () => {
    vi.mocked(getStageCatalog).mockReset().mockRejectedValue(new NetciApiError(503, null, 'down'))
    renderPage(session(['viewer']))
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.queryByRole('heading', { level: 2 })).toBeNull()
  })
})
