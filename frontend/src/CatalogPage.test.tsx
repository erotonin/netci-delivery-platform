import { describe, expect, it, vi } from 'vitest'
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

  it('navigates to Golden Path Templates and instantiates a template', async () => {
    render(
      <PortalFeedbackProvider>
        <CatalogPage session={mockSession} />
      </PortalFeedbackProvider>
    )

    // Switch to Golden Path Templates tab
    const templatesTab = screen.getByRole('tab', { name: /Golden Path Templates/i })
    fireEvent.click(templatesTab)

    // Verify template card
    expect(await screen.findByText('FastAPI Production Service')).toBeTruthy()

    // Click 1-click instantiate
    fireEvent.click(screen.getByText(/1-Click Instantiate/i))

    // Fill instantiation form
    expect(await screen.findByPlaceholderText(/e\.g\. order-api/i)).toBeTruthy()
    fireEvent.change(screen.getByPlaceholderText(/e\.g\. order-api/i), {
      target: { value: 'new-api' },
    })

    // Submit instantiate form
    fireEvent.click(screen.getByRole('button', { name: /Generate Application Plan/i }))

    // Verify plan is rendered
    expect(await screen.findByText(/Instantiated Configuration Plan Ready!/i)).toBeTruthy()
    expect(screen.getByText(/Generated Pipeline Config/i)).toBeTruthy()
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
