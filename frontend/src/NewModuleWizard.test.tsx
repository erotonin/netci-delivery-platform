import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import type { SharedPipeline } from './api/netciClient'

const { mockPipelines } = vi.hoisted(() => ({ mockPipelines: [
  {
    name: 'standard-ci',
    description: 'Shared CI pipeline for standard microservices',
    createdBy: 'admin',
    createdAt: '2026-09-01T00:00:00Z',
    activeVersion: 2,
    stages: [
      { id: 'build', name: 'Build Image', builtin: true },
      { id: 'test', name: 'Unit Tests', builtin: true },
      { id: 'scan', name: 'Security Scan', builtin: true },
    ],
    pendingVersions: [],
    usedBy: ['payments-service', 'auth-service'],
    versions: [],
  },
  {
    name: 'unapproved-ci',
    description: 'Draft pipeline without active version',
    createdBy: 'dev',
    createdAt: '2026-09-02T00:00:00Z',
    activeVersion: null,
    stages: [
      { id: 'checkout', name: 'Checkout', builtin: true },
    ],
    pendingVersions: [1],
    usedBy: [],
    versions: [],
  },
] as SharedPipeline[] }))

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    listSharedPipelines: vi.fn().mockResolvedValue(mockPipelines),
    getGitInfo: vi.fn().mockResolvedValue({ currentCommitSha: null, currentBranch: null, source: null }),
    listSampleApps: vi.fn().mockResolvedValue({ repositoryBaseConfigured: false, items: [] }),
    listDcimModules: vi.fn().mockResolvedValue({ source: 'dcim', status: 'ready', systemId: 'sys-1', items: [] }),
    listDcimServers: vi.fn().mockResolvedValue({ source: 'dcim', status: 'ready', systemId: 'sys-1', moduleId: null, items: [] }),
  }
})

import { listSharedPipelines } from './api/netciClient'
import { PipelineStep, NewModuleWizard } from './NewModuleWizard'
import { PortalFeedbackProvider } from './PortalFeedback'

describe('PipelineStep in isolation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(listSharedPipelines).mockResolvedValue(mockPipelines)
  })

  it('renders active pipelines as selectable cards and unapproved pipelines as disabled', async () => {
    const onChange = vi.fn()
    render(<PipelineStep value="" onChange={onChange} />)

    expect(await screen.findByText('standard-ci')).toBeTruthy()
    expect(screen.getByText('v2')).toBeTruthy()
    expect(screen.getByText('Build Image')).toBeTruthy()
    expect(screen.getByText('Unit Tests')).toBeTruthy()
    expect(screen.getByText('Security Scan')).toBeTruthy()
    expect(screen.getByText('used by 2 modules')).toBeTruthy()
    expect(screen.getByText('Shared CI pipeline for standard microservices')).toBeTruthy()

    // Active card has role="radio", aria-checked="false"
    const activeRadio = screen.getByRole('radio', { name: /standard-ci/i })
    expect(activeRadio.getAttribute('aria-checked')).toBe('false')
    expect(activeRadio.getAttribute('aria-disabled')).toBeNull()

    // Unapproved card has "pending approval" and aria-disabled="true"
    expect(screen.getByText('unapproved-ci')).toBeTruthy()
    expect(screen.getByText('pending approval')).toBeTruthy()
    const disabledRadio = screen.getByRole('radio', { name: /unapproved-ci/i })
    expect(disabledRadio.getAttribute('aria-disabled')).toBe('true')

    // Clicking unapproved card does not call onChange
    fireEvent.click(disabledRadio)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('calls onChange when active pipeline is clicked or activated via keyboard', async () => {
    const onChange = vi.fn()
    render(<PipelineStep value="" onChange={onChange} />)

    const activeRadio = await screen.findByRole('radio', { name: /standard-ci/i })
    fireEvent.click(activeRadio)
    expect(onChange).toHaveBeenCalledWith('standard-ci')

    fireEvent.keyDown(activeRadio, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('standard-ci')

    fireEvent.keyDown(activeRadio, { key: ' ' })
    expect(onChange).toHaveBeenCalledWith('standard-ci')
  })

  it('marks card with aria-checked="true" when value matches pipeline name', async () => {
    render(<PipelineStep value="standard-ci" onChange={vi.fn()} />)

    const activeRadio = await screen.findByRole('radio', { name: /standard-ci/i })
    expect(activeRadio.getAttribute('aria-checked')).toBe('true')
  })

  it('shows empty state when no pipelines have activeVersion', async () => {
    vi.mocked(listSharedPipelines).mockResolvedValueOnce([mockPipelines[1]])
    render(<PipelineStep value="" onChange={vi.fn()} />)

    expect(await screen.findByText(/No approved pipelines found/i)).toBeTruthy()
    expect(screen.getByText(/An administrator must create and approve a pipeline on the Pipelines page/i)).toBeTruthy()
  })

  it('shows empty state when pipeline list is empty', async () => {
    vi.mocked(listSharedPipelines).mockResolvedValueOnce([])
    render(<PipelineStep value="" onChange={vi.fn()} />)

    expect(await screen.findByText(/No approved pipelines found/i)).toBeTruthy()
  })

  it('calls onManagePipelines when clicking "Manage pipelines"', async () => {
    const onManage = vi.fn()
    render(<PipelineStep value="" onChange={vi.fn()} onManagePipelines={onManage} />)

    const manageBtn = await screen.findByRole('button', { name: /Manage pipelines/i })
    fireEvent.click(manageBtn)
    expect(onManage).toHaveBeenCalledTimes(1)
  })

  it('shows error state when listSharedPipelines fails', async () => {
    vi.mocked(listSharedPipelines).mockRejectedValueOnce(new Error('Connection timed out'))
    render(<PipelineStep value="" onChange={vi.fn()} />)

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText(/Connection timed out/i)).toBeTruthy()
  })
})

describe('NewModuleWizard pipeline step integration', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(listSharedPipelines).mockResolvedValue(mockPipelines)
  })

  it('displays step labels: 1 General, 2 Pipeline (CI), 3 Deployment (CD)', async () => {
    render(
      <PortalFeedbackProvider>
        <NewModuleWizard
          systemId="sys-1"
          onCancel={vi.fn()}
          onCreate={vi.fn()}
        />
      </PortalFeedbackProvider>
    )

    expect(screen.getByText('General')).toBeTruthy()
    expect(screen.getByText('Pipeline (CI)')).toBeTruthy()
    expect(screen.getByText('Deployment (CD)')).toBeTruthy()
  })

  it('disables Next on step 2 until a pipeline is selected, then advances to step 3', async () => {
    render(
      <PortalFeedbackProvider>
        <NewModuleWizard
          systemId="sys-1"
          onCancel={vi.fn()}
          onCreate={vi.fn()}
        />
      </PortalFeedbackProvider>
    )

    // Complete Step 1 inputs
    const codeInput = screen.getByPlaceholderText(/e\.g\. core-api/i)
    fireEvent.change(codeInput, { target: { value: 'payment-api' } })
    const nameInput = screen.getByPlaceholderText(/e\.g\. Core Banking API/i)
    fireEvent.change(nameInput, { target: { value: 'Payment API' } })
    const repoInput = screen.getByPlaceholderText(/https:\/\/github\.com/i)
    fireEvent.change(repoInput, { target: { value: 'https://github.com/org/repo.git' } })

    const nextBtn = screen.getByRole('button', { name: /Next/i })
    expect(nextBtn.hasAttribute('disabled')).toBe(false)
    fireEvent.click(nextBtn)

    // Step 2 is active
    expect(await screen.findByRole('heading', { name: 'Select Shared CI Pipeline' })).toBeTruthy()

    // Next button is disabled because no pipeline is selected
    expect(nextBtn.hasAttribute('disabled')).toBe(true)

    // Select active pipeline card
    const activeRadio = await screen.findByRole('radio', { name: /standard-ci/i })
    fireEvent.click(activeRadio)

    // Next button is now enabled
    expect(nextBtn.hasAttribute('disabled')).toBe(false)
    fireEvent.click(nextBtn)

    // Advances to Step 3 (Deployment)
    expect(await screen.findByText(/Target Runtime Environment/i)).toBeTruthy()
  })
})
