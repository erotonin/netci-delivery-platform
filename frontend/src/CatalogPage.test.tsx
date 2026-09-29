import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    listCatalogServices: vi.fn().mockResolvedValue({
      items: [
        {
          serviceId: 'order-service',
          name: 'Order Management Service',
          description: 'Handles order placement and lifecycle',
          owningTeam: 'orders-team',
          tier: 'tier-1',
          lifecycle: 'active',
          repoUrl: 'https://github.com/org/order-service',
          docsUrl: 'https://docs.org/order-service',
          metadata: {},
          createdAt: '2026-09-04T00:00:00Z',
          updatedAt: '2026-09-04T00:00:00Z',
        },
      ],
      nextCursor: null,
    }),
    getServiceDependencies: vi.fn().mockResolvedValue({
      serviceId: 'order-service',
      nodes: [],
      edges: [
        {
          source: 'order-service',
          target: 'payment-service',
          dependencyType: 'sync',
          description: 'Payment authorization',
        },
      ],
      upstream: ['payment-service'],
      downstream: ['frontend-bff'],
      hasCycle: false,
      cycles: [],
    }),
    registerCatalogTemplate: vi.fn().mockResolvedValue({ id: 'fastapi-service', version: 'v1.0.0' }),
    listCatalogTemplates: vi.fn().mockResolvedValue({
      items: [
        {
          templateId: 'fastapi-service',
          version: '1.0.0',
          name: 'FastAPI Production Service',
          description: 'Standard Python FastAPI service with PostgreSQL',
          category: 'backend',
          parametersSchema: {
            pythonVersion: { type: 'string', default: '3.12' },
          },
          pipelineDefinition: {},
          isDeprecated: false,
          createdAt: '2026-09-04T00:00:00Z',
          updatedAt: '2026-09-04T00:00:00Z',
        },
      ],
    }),
    instantiateCatalogTemplate: vi.fn().mockResolvedValue({
      templateId: 'fastapi-service',
      version: '1.0.0',
      applicationName: 'new-api',
      owningTeam: 'core-team',
      runtime: 'docker',
      stages: ['build', 'test', 'deploy'],
      pipelineConfig: { runtime: 'docker' },
      deploymentConfig: { port: 8000 },
    }),
    listPreviewEnvironments: vi.fn().mockResolvedValue({
      items: [
        {
          previewId: 'prv-12345678',
          applicationId: '550e8400-e29b-41d4-a716-446655440000',
          pullRequestId: '42',
          commitSha: 'a1b2c3d4e5f6',
          namespace: 'prv-order-service-pr42',
          url: 'https://pr42.preview.delivery.corp',
          status: 'active',
          ttlSeconds: 86400,
          expiresAt: '2026-09-05T12:00:00Z',
          createdBy: 'alice',
          createdAt: '2026-09-04T12:00:00Z',
        },
      ],
      count: 1,
    }),
    teardownPreviewEnvironment: vi.fn().mockResolvedValue({
      previewId: 'prv-12345678',
      status: 'destroyed',
    }),
    listSelfServiceResources: vi.fn().mockResolvedValue({
      items: [
        {
          requestId: 'res-req-1234',
          applicationId: '550e8400-e29b-41d4-a716-446655440000',
          teamId: 'payments-team',
          environment: 'preview',
          resourceType: 'postgresql',
          spec: { version: '16' },
          status: 'pending_approval',
          statusReason: 'Awaiting reviewer approval',
          provider: 'local-test-provider',
          outputs: {},
          requestedBy: 'bob',
          approvedBy: null,
          createdAt: '2026-09-04T12:00:00Z',
          updatedAt: '2026-09-04T12:00:00Z',
        },
      ],
      count: 1,
    }),
    approveSelfServiceResource: vi.fn().mockResolvedValue({
      requestId: 'res-req-1234',
      status: 'provisioned',
      approvedBy: 'alice',
    }),
  }
})

import { CatalogPage } from './CatalogPage'
import { PortalFeedbackProvider } from './PortalFeedback'

describe('CatalogPage (Phase 12)', () => {
  const mockSession = {
    token: 'test-token',
    identity: {
      authMode: 'token' as const,
      separationOfDuties: true,
      principal: {
        subject: 'alice',
        displayName: 'Alice Admin',
        email: 'alice@corp.internal',
        roles: ['platform-admin'],
        teams: ['payments-team'],
        method: 'token' as const,
      },
    },
  }

  it('renders services list and displays dependency topology when a service is clicked', async () => {
    render(
      <PortalFeedbackProvider>
        <CatalogPage session={mockSession} />
      </PortalFeedbackProvider>
    )

    // Verify service card renders
    expect(await screen.findByText('Order Management Service')).toBeTruthy()
    expect(screen.getByText('(order-service)')).toBeTruthy()

    // Click service to inspect dependencies
    fireEvent.click(screen.getByText('Order Management Service'))

    // Verify dependency topology opens
    expect(await screen.findByText('Dependency Topology')).toBeTruthy()
    expect(screen.getByText('payment-service')).toBeTruthy()
    expect(screen.getByText('frontend-bff')).toBeTruthy()
  })



  it('navigates to Ephemeral Previews and displays environment with URL', async () => {
    render(
      <PortalFeedbackProvider>
        <CatalogPage session={mockSession} />
      </PortalFeedbackProvider>
    )

    // Switch to Previews tab
    const previewsTab = screen.getByRole('tab', { name: /Ephemeral Preview Environments/i })
    fireEvent.click(previewsTab)

    // Verify preview environment row
    expect(await screen.findByText('prv-order-service-pr42')).toBeTruthy()
    expect(screen.getByText('https://pr42.preview.delivery.corp')).toBeTruthy()
    expect(screen.getByText(/Teardown/i)).toBeTruthy()
  })

  it('navigates to Self-Service Resources and approves a request', async () => {
    render(
      <PortalFeedbackProvider>
        <CatalogPage session={mockSession} />
      </PortalFeedbackProvider>
    )

    // Switch to Resources tab
    const resourcesTab = screen.getByRole('tab', { name: /Self-Service Resources/i })
    fireEvent.click(resourcesTab)

    // Verify resource request row
    expect(await screen.findByText('postgresql')).toBeTruthy()
    expect(screen.getByText('By: bob')).toBeTruthy()

    // Click Approve
    const approveBtn = screen.getByRole('button', { name: /Approve/i })
    fireEvent.click(approveBtn)

    await waitFor(() => {
      expect(screen.getByText(/Resource request approved!/i)).toBeTruthy()
    })
  })
})



describe('Catalog explanations, intro panel and contextual hints', () => {
  beforeEach(() => {
    try {
      window.localStorage.clear()
    } catch {
      // ignore
    }
  })

  it('renders collapsible intro panel explaining the service catalog and the three tabs', async () => {
    render(
      <PortalFeedbackProvider>
        <CatalogPage />
      </PortalFeedbackProvider>
    )

    // Intro panel header & lead paragraph
    expect(await screen.findByRole('heading', { level: 2, name: /Giới thiệu Service Catalog/i })).toBeTruthy()
    expect(
      screen.getByText(/Service Catalog là danh bạ của mọi service\/module: ai sở hữu, mức độ quan trọng \(tier\), vòng đời, phụ thuộc giữa các service/i)
    ).toBeTruthy()
    expect(
      screen.getByText(/Nó là nguồn sự thật cho câu hỏi “service này của ai, gọi tới ai, có được deploy không”/i)
    ).toBeTruthy()

    // 3 tab explanation cards
    expect(screen.getByRole('heading', { level: 3, name: /^Services$/i })).toBeTruthy()
    expect(screen.getByText(/Danh bạ định danh mọi service\/module: quản lý team sở hữu/i)).toBeTruthy()

    expect(screen.getByRole('heading', { level: 3, name: /^Previews$/i })).toBeTruthy()
    expect(screen.getByText(/Môi trường preview tạm thời và cô lập cho một merge request/i)).toBeTruthy()

    expect(screen.getByRole('heading', { level: 3, name: /^Resources$/i })).toBeTruthy()
    expect(screen.getByText(/Cổng tự phục vụ yêu cầu tài nguyên đám mây/i)).toBeTruthy()

    // Each card specifies what a user can try right now ("Demo được gì")
    const demoLabels = screen.getAllByText('Demo được gì:')
    expect(demoLabels.length).toBe(3)
  })

  it('toggles collapsible intro panel, hides/shows explanation and updates localStorage', async () => {
    render(
      <PortalFeedbackProvider>
        <CatalogPage />
      </PortalFeedbackProvider>
    )

    // Initially open by default
    const toggleButton = await screen.findByRole('button', { name: 'Ẩn giải thích' })
    expect(toggleButton).toBeTruthy()
    expect(screen.getByText(/Service Catalog là danh bạ của mọi service\/module/i)).toBeTruthy()

    // Click toggle to collapse
    fireEvent.click(toggleButton)

    // Button label switches and content is hidden
    expect(screen.getByRole('button', { name: 'Xem giải thích' })).toBeTruthy()
    expect(screen.queryByText(/Service Catalog là danh bạ của mọi service\/module/i)).toBeNull()
    expect(window.localStorage.getItem('netci.catalog.introHidden')).toBe('true')

    // Click toggle again to expand
    fireEvent.click(screen.getByRole('button', { name: 'Xem giải thích' }))
    expect(screen.getByRole('button', { name: 'Ẩn giải thích' })).toBeTruthy()
    expect(screen.getByText(/Service Catalog là danh bạ của mọi service\/module/i)).toBeTruthy()
    expect(window.localStorage.getItem('netci.catalog.introHidden')).toBe('false')
  })

  it('respects initial introHidden state stored in localStorage', async () => {
    window.localStorage.setItem('netci.catalog.introHidden', 'true')
    render(
      <PortalFeedbackProvider>
        <CatalogPage />
      </PortalFeedbackProvider>
    )

    // Should start collapsed
    expect(await screen.findByRole('button', { name: 'Xem giải thích' })).toBeTruthy()
    expect(screen.queryByText(/Service Catalog là danh bạ của mọi service\/module/i)).toBeNull()
  })

  it('shows contextual hint on each tab above its content', async () => {
    render(
      <PortalFeedbackProvider>
        <CatalogPage />
      </PortalFeedbackProvider>
    )

    // Tab 1: Services (active by default)
    expect(
      await screen.findByText(/Services: xem owner\/tier\/lifecycle, đồ thị phụ thuộc, đánh dấu deprecated và kiểm tra chu trình phụ thuộc\./i)
    ).toBeTruthy()

    // Tab 2: Previews
    fireEvent.click(screen.getByRole('tab', { name: /Ephemeral Preview Environments/i }))
    expect(
      await screen.findByText(/Previews: môi trường preview tạm thời cho một merge request, tự huỷ khi hết hạn\./i)
    ).toBeTruthy()

    // Tab 3: Resources
    fireEvent.click(screen.getByRole('tab', { name: /Self-Service Resources/i }))
    expect(
      await screen.findByText(/Resources: yêu cầu tài nguyên \(DB, bucket…\) qua phê duyệt; provider chưa cấu hình thì trạng thái.*fail-closed.*chứ không giả lập\./i)
    ).toBeTruthy()
  })
})


