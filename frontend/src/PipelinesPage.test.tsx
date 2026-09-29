import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { NetciApiError, SharedPipeline, PipelineBuildingBlocks, PortalModule } from './api/netciClient'

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    listSharedPipelines: vi.fn(),
    getSharedPipeline: vi.fn(),
    getPipelineBuildingBlocks: vi.fn(),
    createSharedPipeline: vi.fn(),
    proposeSharedPipelineVersion: vi.fn(),
    approveSharedPipelineVersion: vi.fn(),
    rejectSharedPipelineVersion: vi.fn(),
    setModuleSharedPipeline: vi.fn(),
    listModules: vi.fn(),
  }
})

const notify = vi.fn()
vi.mock('./PortalFeedback', () => ({
  usePortalFeedback: () => ({ notify })
}))

import {
  listSharedPipelines,
  getSharedPipeline,
  getPipelineBuildingBlocks,
  createSharedPipeline,
  proposeSharedPipelineVersion,
  approveSharedPipelineVersion,
  rejectSharedPipelineVersion,
  setModuleSharedPipeline,
  listModules,
} from './api/netciClient'
import { PipelinesPage } from './PipelinesPage'

const mockPipelines: SharedPipeline[] = [
  {
    name: 'standard-go',
    description: 'Go service pipeline',
    createdBy: 'alice',
    createdAt: '2025-01-01T00:00:00Z',
    activeVersion: 1,
    stages: [
      { id: 'unit-test', name: 'Unit Tests', builtin: true },
      { id: 'build', name: 'Build', builtin: true },
    ],
    pendingVersions: [2],
    usedBy: ['payments-api', 'users-api'],
    versions: []
  },
  {
    name: 'no-active',
    description: 'No active version pipeline',
    createdBy: 'bob',
    createdAt: '2025-01-01T00:00:00Z',
    activeVersion: null,
    stages: [],
    pendingVersions: [],
    usedBy: [],
    versions: []
  }
]

const mockBuildingBlocks: PipelineBuildingBlocks = {
  builtins: [
    { id: 'build', name: 'Build', required: true, block: '# @stage build "Build" builtin\nnetci-builtin build' },
    { id: 'sbom', name: 'SBOM', required: true, block: '# @stage sbom "SBOM" builtin\nnetci-builtin sbom' },
    { id: 'unit-test', name: 'Unit Tests', required: false, block: '# @stage unit-test "Unit Tests" builtin\nnetci-builtin unit-test' },
  ],
  templates: [
    { id: 'my-template', name: 'Custom Lint', description: 'Run linters', body: 'run-lint', block: '# @stage lint "Custom Lint"\nrun-lint' }
  ],
  order: ['unit-test', 'build', 'sbom'],
  required: ['build', 'sbom']
}

describe('PipelinesPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('list renders pipelines with active version, pending badge, used-by', async () => {
    vi.mocked(listSharedPipelines).mockResolvedValue(mockPipelines)

    render(<PipelinesPage navigate={vi.fn()} />)

    await screen.findByText('standard-go')
    
    // Check pending badge
    expect(screen.getByText('pending approval v2')).toBeTruthy()
    
    // Check used-by count
    expect(screen.getByText('Used by: 2 modules')).toBeTruthy()
    
    // Check active version formatting
    expect(screen.getByText('v1')).toBeTruthy()
    
    // Check pipeline without active version
    expect(screen.getByText('no-active')).toBeTruthy()
    expect(screen.getByText('no approved version')).toBeTruthy()
    expect(screen.getByText('Used by: 0 modules')).toBeTruthy()
  })

  it('designer: new pipeline prefilled with required builtin blocks; clicking a template appends its block to the textarea; a builtin already present is disabled', async () => {
    vi.mocked(listSharedPipelines).mockResolvedValue(mockPipelines)
    vi.mocked(getPipelineBuildingBlocks).mockResolvedValue(mockBuildingBlocks)

    render(<PipelinesPage navigate={vi.fn()} />)

    // Go to designer
    fireEvent.click(await screen.findByRole('button', { name: 'New pipeline' }))
    
    // Wait for designer to load
    await screen.findByText('Create New Pipeline')
    
    // Check textarea is prefilled with required blocks (build and sbom)
    const textarea = screen.getByLabelText('Pipeline script') as HTMLTextAreaElement
    expect(textarea.value).toContain('# @stage build "Build" builtin')
    expect(textarea.value).toContain('# @stage sbom "SBOM" builtin')
    expect(textarea.value).not.toContain('Unit Tests') // not required
    
    // Check that required builtins are disabled because they are present
    const buildBtn = screen.getByRole('button', { name: /Build/ })
    expect(buildBtn).toHaveProperty('disabled', true)
    expect(within(buildBtn).getByText('added')).toBeTruthy()
    
    // Unit test is not present, so it is enabled
    const testBtn = screen.getByRole('button', { name: /Unit Tests/ })
    expect(testBtn).toHaveProperty('disabled', false)
    
    // Click template appends its block
    const templateBtn = screen.getByRole('button', { name: /Custom Lint/ })
    fireEvent.click(templateBtn)
    
    expect(textarea.value.endsWith('# @stage lint "Custom Lint"\nrun-lint\n')).toBe(true)
  })

  it('submit calls createSharedPipeline with {name, description, script}; a rejected promise shows its message', async () => {
    vi.mocked(listSharedPipelines).mockResolvedValue(mockPipelines)
    vi.mocked(getPipelineBuildingBlocks).mockResolvedValue(mockBuildingBlocks)
    
    const errorMsg = 'Invalid pipeline structure'
    vi.mocked(createSharedPipeline).mockRejectedValue(new NetciApiError(422, { code: 'ERR', message: errorMsg }, 'Fallback'))

    render(<PipelinesPage navigate={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'New pipeline' }))
    
    await screen.findByText('Create New Pipeline')
    
    const nameInput = screen.getByPlaceholderText('Pipeline name (e.g. my-pipeline)')
    const descInput = screen.getByPlaceholderText('Short description')
    
    fireEvent.change(nameInput, { target: { value: 'my-pipe' } })
    fireEvent.change(descInput, { target: { value: 'cool pipe' } })
    
    fireEvent.click(screen.getByRole('button', { name: 'Submit for approval' }))
    
    await waitFor(() => {
      expect(createSharedPipeline).toHaveBeenCalledWith({
        name: 'my-pipe',
        description: 'cool pipe',
        script: expect.any(String)
      })
    })
    
    await screen.findByText(`Error: ${errorMsg}`)
  })

  it('detail: approve calls approveSharedPipelineVersion; reject requires a reason and calls rejectSharedPipelineVersion', async () => {
    vi.mocked(listSharedPipelines).mockResolvedValue(mockPipelines)
    vi.mocked(getSharedPipeline).mockResolvedValue({
      ...mockPipelines[0],
      versions: [
        { version: 1, status: 'active', sha256: 'abc', stages: [], createdBy: 'alice', createdAt: '2025', decidedBy: 'bob', decidedAt: '2025', rejectionReason: null, script: 'echo 1' },
        { version: 2, status: 'proposed', sha256: 'def', stages: [], createdBy: 'charlie', createdAt: '2025', decidedBy: null, decidedAt: null, rejectionReason: null, script: 'echo 2' }
      ]
    })
    
    render(<PipelinesPage navigate={vi.fn()} />)
    
    // Go to detail
    fireEvent.click(await screen.findByText('standard-go'))
    
    // Wait for detail view
    await screen.findByText('Version History')
    
    // Approve
    const approveBtn = screen.getByRole('button', { name: 'Approve' })
    fireEvent.click(approveBtn)
    await waitFor(() => {
      expect(approveSharedPipelineVersion).toHaveBeenCalledWith('standard-go', 2)
    })
    
    // Reject
    const rejectBtn = screen.getByRole('button', { name: 'Reject' })
    fireEvent.click(rejectBtn)
    
    const rejectReasonInput = screen.getByPlaceholderText('Rejection reason')
    const rejectSubmitBtn = screen.getByRole('button', { name: 'Submit' })
    
    // initially disabled
    expect(rejectSubmitBtn).toHaveProperty('disabled', true)
    
    fireEvent.change(rejectReasonInput, { target: { value: 'bad code' } })
    expect(rejectSubmitBtn).toHaveProperty('disabled', false)
    
    fireEvent.click(rejectSubmitBtn)
    
    await waitFor(() => {
      expect(rejectSharedPipelineVersion).toHaveBeenCalledWith('standard-go', 2, 'bad code')
    })
  })

  it('moduleId panel calls setModuleSharedPipeline', async () => {
    vi.mocked(listSharedPipelines).mockResolvedValue(mockPipelines)
    const mockMods: PortalModule[] = [{
      id: 'payments-api', name: 'Payments', pipeline: 'standard-go',
      systemId: '', type: '', description: '', runtime: 'docker', applicationId: null, versions: [], deploymentEnvironments: [], pipelineConfig: {}, environments: [], pipelineRuns: [], dora: []
    }]
    vi.mocked(listModules).mockResolvedValue(mockMods)

    render(<PipelinesPage moduleId="payments-api" navigate={vi.fn()} />)
    
    await screen.findByText('Module payments-api shared pipeline:')
    
    const select = screen.getByRole('combobox') as HTMLSelectElement
    expect(select.value).toBe('standard-go') // populated from module

    // Select <None>
    fireEvent.change(select, { target: { value: '' } })
    
    const saveBtn = screen.getByRole('button', { name: 'Save' })
    fireEvent.click(saveBtn)
    
    await waitFor(() => {
      expect(setModuleSharedPipeline).toHaveBeenCalledWith('payments-api', null)
    })
  })

  it('a refused approval tells the user why instead of failing silently', async () => {
    vi.mocked(listSharedPipelines).mockResolvedValue(mockPipelines)
    vi.mocked(getSharedPipeline).mockResolvedValue({
      ...mockPipelines[0],
      versions: [
        { version: 2, status: 'proposed', sha256: 'def', stages: [], createdBy: 'alice', createdAt: '2025', decidedBy: null, decidedAt: null, rejectionReason: null, script: 'echo 2' },
      ],
    })
    vi.mocked(approveSharedPipelineVersion).mockRejectedValue(
      new NetciApiError(403, { code: 'SEPARATION_OF_DUTIES', message: 'a pipeline version must be approved by someone other than its author' }, 'x'))

    render(<PipelinesPage navigate={vi.fn()} />)
    fireEvent.click(await screen.findByText('standard-go'))
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(notify).toHaveBeenCalledWith('a pipeline version must be approved by someone other than its author', 'error'))
  })
})
