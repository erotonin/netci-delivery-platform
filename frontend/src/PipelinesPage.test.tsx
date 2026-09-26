import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import {
  NetciApiError,
  type ModulePipeline,
  type ModulePipelineProposalResult,
  type ModulePipelineStageCode,
  type PortalModule,
} from './api/netciClient'

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    listModules: vi.fn(),
    getModulePipeline: vi.fn(),
    getModulePipelineStageCode: vi.fn(),
    createModulePipelineProposal: vi.fn(),
  }
})

import {
  createModulePipelineProposal,
  getModulePipeline,
  getModulePipelineStageCode,
  listModules,
} from './api/netciClient'
import { PipelinesPage } from './PipelinesPage'

const mockModules: PortalModule[] = [
  {
    id: 'payments-api',
    systemId: 'payments',
    name: 'Payments API',
    type: 'service',
    description: 'Payments service module',
    runtime: 'docker',
    applicationId: 'payments-app',
    versions: ['1.0.0'],
    deploymentEnvironments: [],
    pipelineConfig: {},
    environments: [],
    pipelineRuns: [],
    dora: [],
  },
  {
    id: 'auth-service',
    systemId: 'core',
    name: 'Auth Service',
    type: 'service',
    description: 'Auth service module',
    runtime: 'kubernetes',
    applicationId: 'auth-app',
    versions: ['1.0.0'],
    deploymentEnvironments: [],
    pipelineConfig: {},
    environments: [],
    pipelineRuns: [],
    dora: [],
  },
]

const mockPipelinePayments: ModulePipeline = {
  moduleId: 'payments-api',
  template: 'docker-service',
  stages: [
    {
      id: 'checkout',
      name: 'Checkout source',
      category: 'source',
      kind: 'builtin',
      required: true,
      after: null,
      script: null,
    },
    {
      id: 'build',
      name: 'Build image',
      category: 'build',
      kind: 'builtin',
      required: true,
      after: null,
      script: null,
    },
  ],
  catalog: [
    {
      id: 'checkout',
      name: 'Checkout source',
      category: 'source',
      kind: 'builtin',
      required: true,
      description: 'Checkout Git source code',
    },
    {
      id: 'unit-test',
      name: 'Unit tests',
      category: 'test',
      kind: 'builtin',
      required: false,
      description: 'Run unit test suite',
    },
    {
      id: 'build',
      name: 'Build image',
      category: 'build',
      kind: 'builtin',
      required: true,
      description: 'Build Docker image',
    },
    {
      id: 'publish',
      name: 'Publish image',
      category: 'publish',
      kind: 'builtin',
      required: false,
      description: 'Push image to registry',
    },
  ],
  repository: {
    provider: 'gitlab',
    identity: 'group/payments-api',
    supportsProposals: true,
  },
}

const mockPipelineAuth: ModulePipeline = {
  moduleId: 'auth-service',
  template: 'k8s-service',
  stages: [
    {
      id: 'checkout',
      name: 'Checkout source',
      category: 'source',
      kind: 'builtin',
      required: true,
      after: null,
      script: null,
    },
    {
      id: 'unit-test',
      name: 'Unit tests',
      category: 'test',
      kind: 'builtin',
      required: false,
      after: null,
      script: null,
    },
  ],
  catalog: [
    { id: 'checkout', name: 'Checkout source', category: 'source', kind: 'builtin', required: true },
    { id: 'unit-test', name: 'Unit tests', category: 'test', kind: 'builtin', required: false },
  ],
  repository: {
    provider: 'gitlab',
    identity: 'group/auth-service',
    supportsProposals: true,
  },
}

const mockCheckoutCode: ModulePipelineStageCode = {
  stageId: 'checkout',
  language: 'bash',
  editable: false,
  path: 'scripts/ci/checkout.sh',
  content: '#!/usr/bin/env bash\necho "Running checkout..."',
}

const mockUnitTestCode: ModulePipelineStageCode = {
  stageId: 'unit-test',
  language: 'bash',
  editable: false,
  path: 'scripts/ci/test.sh',
  content: '#!/usr/bin/env bash\necho "Running unit tests..."',
}

const mockCustomLintCode: ModulePipelineStageCode = {
  stageId: 'custom-lint',
  language: 'bash',
  editable: true,
  path: '.netci/stages/custom-lint.sh',
  content: '#!/usr/bin/env bash\n./lint.sh',
}

describe('PipelinesPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  // 1. list renders chips
  it('list renders chips', async () => {
    const navigate = vi.fn()
    vi.mocked(listModules).mockResolvedValue(mockModules)
    vi.mocked(getModulePipeline).mockImplementation(async (modId) => {
      if (modId === 'payments-api') return mockPipelinePayments
      if (modId === 'auth-service') return mockPipelineAuth
      throw new Error(`Module ${modId} not found`)
    })

    render(<PipelinesPage navigate={navigate} />)

    // Wait for the modules table to be displayed
    await screen.findByTestId('pipelines-table')

    // Module names and system IDs are displayed
    expect(screen.getByText('Payments API')).toBeTruthy()
    expect(screen.getByText('Auth Service')).toBeTruthy()
    expect(screen.getByText('payments')).toBeTruthy()
    expect(screen.getByText('core')).toBeTruthy()

    // Current stage sequence chips are rendered from getModulePipeline
    await waitFor(() => {
      expect(screen.getAllByText('Checkout source').length).toBe(2)
    })
    expect(screen.getByText('Build image')).toBeTruthy()
    expect(screen.getByText('Unit tests')).toBeTruthy()

    // "Thiết kế" button is present and navigates to the designer for that module
    const designButtons = screen.getAllByRole('button', { name: 'Thiết kế' })
    expect(designButtons).toHaveLength(2)
    fireEvent.click(designButtons[0])
    expect(navigate).toHaveBeenCalledWith('pipelines', { moduleId: 'payments-api' })
  })

  // 2. clicking a catalog item appends it in template position
  it('clicking a catalog item appends it in template position', async () => {
    const navigate = vi.fn()
    vi.mocked(getModulePipeline).mockResolvedValue(mockPipelinePayments)
    vi.mocked(getModulePipelineStageCode).mockResolvedValue(mockCheckoutCode)

    render(<PipelinesPage moduleId="payments-api" navigate={navigate} />)
    await screen.findByText('Thiết kế Pipeline: payments-api')

    // Initially, pipeline has [Checkout source, Build image]
    const stageCardsPanel = screen
      .getByText('Checkout source', { selector: '.stage-card strong' })
      .closest('.stage-cards-panel')!
    const initialCards = Array.from(stageCardsPanel.querySelectorAll('.stage-card strong')).map(
      (el) => el.textContent
    )
    expect(initialCards).toEqual(['Checkout source', 'Build image'])

    // Click "Unit tests" in the catalog
    const catalogPanel = screen.getByText('Catalog Stages').closest('.catalog-panel')!
    const unitTestBtn = within(catalogPanel).getByRole('button', { name: /Unit tests/i })
    fireEvent.click(unitTestBtn)

    // Unit tests must be inserted at its template position (after Checkout source, before Build image)
    const updatedCards = Array.from(stageCardsPanel.querySelectorAll('.stage-card strong')).map(
      (el) => el.textContent
    )
    expect(updatedCards).toEqual(['Checkout source', 'Unit tests', 'Build image'])

    // Clicking an already present stage selects it without duplicating it
    fireEvent.click(unitTestBtn)
    const cardsAfterDuplicateClick = Array.from(
      stageCardsPanel.querySelectorAll('.stage-card strong')
    ).map((el) => el.textContent)
    expect(cardsAfterDuplicateClick).toEqual(['Checkout source', 'Unit tests', 'Build image'])
  })

  // 3. required stage has no remove button
  it('required stage has no remove button', async () => {
    const navigate = vi.fn()
    const pipelineWithOptional: ModulePipeline = {
      ...mockPipelinePayments,
      stages: [
        {
          id: 'checkout',
          name: 'Checkout source',
          category: 'source',
          kind: 'builtin',
          required: true,
          after: null,
          script: null,
        },
        {
          id: 'unit-test',
          name: 'Unit tests',
          category: 'test',
          kind: 'builtin',
          required: false,
          after: null,
          script: null,
        },
      ],
    }
    vi.mocked(getModulePipeline).mockResolvedValue(pipelineWithOptional)
    vi.mocked(getModulePipelineStageCode).mockResolvedValue(mockCheckoutCode)

    render(<PipelinesPage moduleId="payments-api" navigate={navigate} />)
    await screen.findByText('Thiết kế Pipeline: payments-api')

    const stageCardsPanel = screen
      .getByText('Checkout source', { selector: '.stage-card strong' })
      .closest('.stage-cards-panel')!

    // Required stage (Checkout source) has no remove button
    expect(within(stageCardsPanel).queryByRole('button', { name: 'Xóa stage Checkout source' })).toBeNull()

    // Optional stage (Unit tests) has a remove button
    const removeBtn = within(stageCardsPanel).getByRole('button', { name: 'Xóa stage Unit tests' })
    expect(removeBtn).toBeTruthy()

    // Clicking remove deletes the optional stage from the pipeline
    fireEvent.click(removeBtn)
    expect(within(stageCardsPanel).queryByText('Unit tests')).toBeNull()
  })

  // 4. built-in code is read-only and custom editable
  it('built-in code is read-only and custom editable', async () => {
    const navigate = vi.fn()
    const pipelineWithCustom: ModulePipeline = {
      ...mockPipelinePayments,
      stages: [
        {
          id: 'checkout',
          name: 'Checkout source',
          category: 'source',
          kind: 'builtin',
          required: true,
          after: null,
          script: null,
        },
        {
          id: 'custom-lint',
          name: 'Custom Lint',
          category: 'custom',
          kind: 'custom',
          required: false,
          after: 'checkout',
          script: '.netci/stages/custom-lint.sh',
        },
      ],
    }
    vi.mocked(getModulePipeline).mockResolvedValue(pipelineWithCustom)
    vi.mocked(getModulePipelineStageCode).mockImplementation(async (_modId, stageId) => {
      if (stageId === 'checkout') return mockCheckoutCode
      if (stageId === 'custom-lint') return mockCustomLintCode
      throw new Error(`Stage ${stageId} not found`)
    })

    render(<PipelinesPage moduleId="payments-api" navigate={navigate} />)
    await screen.findByText('Thiết kế Pipeline: payments-api')

    // 1. Built-in stage (Checkout) is selected initially
    await waitFor(() => {
      expect(screen.getByText('Stage có sẵn: code do shared library quản lý')).toBeTruthy()
    })
    const builtinTextarea = screen.getByLabelText('Mã nguồn stage') as HTMLTextAreaElement
    expect(builtinTextarea.readOnly).toBe(true)
    expect(builtinTextarea.value).toBe('#!/usr/bin/env bash\necho "Running checkout..."')

    // 2. Click on the custom stage card to select it
    const stageCardsPanel = screen
      .getByText('Checkout source', { selector: '.stage-card strong' })
      .closest('.stage-cards-panel')!
    const customCard = within(stageCardsPanel).getByText('Custom Lint').closest('.stage-card')!
    fireEvent.click(customCard)

    // Wait for custom stage code to load
    await waitFor(() => {
      expect(screen.getByText('.netci/stages/custom-lint.sh')).toBeTruthy()
    })
    // Note for shared-library is hidden for custom stages
    expect(screen.queryByText('Stage có sẵn: code do shared library quản lý')).toBeNull()

    // Custom stage textarea is editable
    const customTextarea = screen.getByLabelText('Mã nguồn stage') as HTMLTextAreaElement
    expect(customTextarea.readOnly).toBe(false)
    expect(customTextarea.value).toBe('#!/usr/bin/env bash\n./lint.sh')

    // Edit code in custom stage
    fireEvent.change(customTextarea, {
      target: { value: '#!/usr/bin/env bash\necho "Custom linting active"' },
    })
    expect(customTextarea.value).toBe('#!/usr/bin/env bash\necho "Custom linting active"')

    // Switch back to checkout: shared library note reappears and code is read-only
    const checkoutCard = within(stageCardsPanel).getByText('Checkout source').closest('.stage-card')!
    fireEvent.click(checkoutCard)
    await waitFor(() => {
      expect(screen.getByText('Stage có sẵn: code do shared library quản lý')).toBeTruthy()
    })

    // Switch back to custom stage: unsaved edits are preserved per stage
    fireEvent.click(customCard)
    const preservedTextarea = screen.getByLabelText('Mã nguồn stage') as HTMLTextAreaElement
    expect(preservedTextarea.value).toBe('#!/usr/bin/env bash\necho "Custom linting active"')
  })

  // 5. proposal body contains ordered stages and custom code
  it('proposal body contains ordered stages and custom code', async () => {
    const navigate = vi.fn()
    const pipelineWithCustom: ModulePipeline = {
      ...mockPipelinePayments,
      stages: [
        {
          id: 'checkout',
          name: 'Checkout source',
          category: 'source',
          kind: 'builtin',
          required: true,
          after: null,
          script: null,
        },
        {
          id: 'custom-lint',
          name: 'Custom Lint',
          category: 'custom',
          kind: 'custom',
          required: false,
          after: 'checkout',
          script: '.netci/stages/custom-lint.sh',
        },
      ],
    }
    vi.mocked(getModulePipeline).mockResolvedValue(pipelineWithCustom)
    vi.mocked(getModulePipelineStageCode).mockImplementation(async (_modId, stageId) => {
      if (stageId === 'checkout') return mockCheckoutCode
      if (stageId === 'custom-lint') return mockCustomLintCode
      throw new Error(`Stage ${stageId} not found`)
    })

    const mockProposalResult: ModulePipelineProposalResult = {
      mergeRequestUrl: 'https://gitlab.example.test/group/payments-api/-/merge_requests/42',
      branch: 'netci/pipeline-1a2b3c4d',
      iid: 42,
      stages: [
        { id: 'checkout', name: 'Checkout source', after: null, code: null },
        { id: 'custom-lint', name: 'Custom Lint', after: 'checkout', code: '#!/bin/bash\necho "Edited code"' },
      ],
    }
    vi.mocked(createModulePipelineProposal).mockResolvedValue(mockProposalResult)

    render(<PipelinesPage moduleId="payments-api" navigate={navigate} />)
    await screen.findByText('Thiết kế Pipeline: payments-api')

    // Select custom stage and modify its code
    const stageCardsPanel = screen
      .getByText('Checkout source', { selector: '.stage-card strong' })
      .closest('.stage-cards-panel')!
    const customCard = within(stageCardsPanel).getByText('Custom Lint').closest('.stage-card')!
    fireEvent.click(customCard)

    await waitFor(() => {
      expect(screen.getByText('.netci/stages/custom-lint.sh')).toBeTruthy()
    })

    const textarea = screen.getByLabelText('Mã nguồn stage')
    fireEvent.change(textarea, { target: { value: '#!/bin/bash\necho "Edited code"' } })

    // Click "Tạo merge request"
    const proposeBtn = screen.getByRole('button', { name: 'Tạo merge request' })
    fireEvent.click(proposeBtn)

    // Verifies proposal body contains ordered stages with after+code for custom ones
    await waitFor(() => {
      expect(createModulePipelineProposal).toHaveBeenCalledWith('payments-api', {
        stages: [
          {
            id: 'checkout',
            name: 'Checkout source',
            after: null,
            code: null,
          },
          {
            id: 'custom-lint',
            name: 'Custom Lint',
            after: 'checkout',
            code: '#!/bin/bash\necho "Edited code"',
          },
        ],
      })
    })

    // On 201, show MR link and branch
    await screen.findByText('Tạo merge request thành công!')
    expect(screen.getByText('netci/pipeline-1a2b3c4d')).toBeTruthy()
    const link = screen.getByRole('link', {
      name: 'https://gitlab.example.test/group/payments-api/-/merge_requests/42',
    })
    expect(link.getAttribute('href')).toBe(
      'https://gitlab.example.test/group/payments-api/-/merge_requests/42'
    )
  })

  // 6. server 422 message is shown
  it('server 422 message is shown', async () => {
    const navigate = vi.fn()
    vi.mocked(getModulePipeline).mockResolvedValue(mockPipelinePayments)
    vi.mocked(getModulePipelineStageCode).mockResolvedValue(mockCheckoutCode)
    vi.mocked(createModulePipelineProposal).mockRejectedValue(
      new NetciApiError(
        422,
        {
          code: 'PIPELINE_ORDER_INVALID',
          message: 'Built-in stages must preserve template order',
        },
        'Unprocessable Entity'
      )
    )

    render(<PipelinesPage moduleId="payments-api" navigate={navigate} />)
    await screen.findByText('Thiết kế Pipeline: payments-api')

    const proposeBtn = screen.getByRole('button', { name: 'Tạo merge request' })
    fireEvent.click(proposeBtn)

    // Server error message is shown verbatim in alert box
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Built-in stages must preserve template order')
  })

  // 7. unsupported repository disables the button
  it('unsupported repository disables the button', async () => {
    const navigate = vi.fn()
    const unsupportedPipeline: ModulePipeline = {
      ...mockPipelinePayments,
      repository: {
        provider: 'github',
        identity: 'org/payments-api',
        supportsProposals: false,
      },
    }
    vi.mocked(getModulePipeline).mockResolvedValue(unsupportedPipeline)
    vi.mocked(getModulePipelineStageCode).mockResolvedValue(mockCheckoutCode)

    render(<PipelinesPage moduleId="payments-api" navigate={navigate} />)
    await screen.findByText('Thiết kế Pipeline: payments-api')

    const proposeBtn = screen.getByRole('button', { name: 'Tạo merge request' })
    expect(proposeBtn).toBeDisabled()
    expect(
      screen.getByText('Kho lưu trữ không hỗ trợ tạo merge request (chỉ hỗ trợ GitLab).')
    ).toBeTruthy()
  })

  // 8. Custom stage creation with ID validation
  it('validates custom stage id and adds custom stage after anchor', async () => {
    const navigate = vi.fn()
    vi.mocked(getModulePipeline).mockResolvedValue(mockPipelinePayments)
    vi.mocked(getModulePipelineStageCode).mockResolvedValue(mockCheckoutCode)

    render(<PipelinesPage moduleId="payments-api" navigate={navigate} />)
    await screen.findByText('Thiết kế Pipeline: payments-api')

    // Open custom stage modal
    const openModalBtn = screen.getByRole('button', { name: /\+ Script tùy chỉnh/i })
    fireEvent.click(openModalBtn)

    expect(screen.getByRole('heading', { name: 'Thêm Script tùy chỉnh' })).toBeTruthy()

    const idInput = screen.getByLabelText('Mã stage')
    const nameInput = screen.getByLabelText('Tên hiển thị stage')
    const submitBtn = screen.getByRole('button', { name: 'Thêm stage' })

    // Invalid id: contains capital letters
    fireEvent.change(idInput, { target: { value: 'Bad_Stage_Id' } })
    fireEvent.change(nameInput, { target: { value: 'My Custom Lint' } })
    fireEvent.click(submitBtn)

    expect(
      screen.getByText(/Mã stage \(id\) phải bắt đầu bằng chữ thường/)
    ).toBeTruthy()

    // Valid id: lowercase with hyphens
    fireEvent.change(idInput, { target: { value: 'my-custom-lint' } })
    fireEvent.click(submitBtn)

    // Modal closes and new stage is appended to stage cards
    expect(screen.queryByRole('heading', { name: 'Thêm Script tùy chỉnh' })).toBeNull()

    const stageCardsPanel = screen
      .getByText('Checkout source', { selector: '.stage-card strong' })
      .closest('.stage-cards-panel')!
    expect(within(stageCardsPanel).getByText('My Custom Lint')).toBeTruthy()
  })

  // 9. Catalog search filtering
  it('filters catalog stages using the search input', async () => {
    const navigate = vi.fn()
    vi.mocked(getModulePipeline).mockResolvedValue(mockPipelinePayments)
    vi.mocked(getModulePipelineStageCode).mockResolvedValue(mockCheckoutCode)

    render(<PipelinesPage moduleId="payments-api" navigate={navigate} />)
    await screen.findByText('Thiết kế Pipeline: payments-api')

    const searchInput = screen.getByLabelText('Tìm kiếm stage trong catalog')
    fireEvent.change(searchInput, { target: { value: 'unit' } })

    const catalogPanel = screen.getByText('Catalog Stages').closest('.catalog-panel')!
    expect(within(catalogPanel).getByText('Unit tests')).toBeTruthy()
    expect(within(catalogPanel).queryByText('Publish image')).toBeNull()
  })
})
