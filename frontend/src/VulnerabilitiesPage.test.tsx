import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { VulnerabilityExposure } from './api/netciClient'

const { mockExposure, mockEmptyExposure } = vi.hoisted(() => {
  const baseExposure: VulnerabilityExposure = {
    vulnerabilityId: null,
    minSeverity: 'HIGH',
    affected: [
      {
        moduleId: 'payments-api',
        systemId: 'payments',
        environment: 'prod',
        artifactDigest: 'sha256:1111',
        pipelineRunId: 'pr-1',
        vulnerabilityId: 'CVE-2024-9999',
        severity: 'CRITICAL',
        package: 'libssl3',
        installedVersion: '3.0.2',
        fixedVersion: '3.0.3',
        sources: ['ci', 'rescan'],
        firstSeenAt: '2026-09-20T00:00:00Z',
      },
      {
        moduleId: 'auth-service',
        systemId: 'core',
        environment: 'staging',
        artifactDigest: 'sha256:2222',
        pipelineRunId: 'pr-2',
        vulnerabilityId: 'CVE-2024-8888',
        severity: 'HIGH',
        package: 'curl',
        installedVersion: '7.88.1',
        fixedVersion: '',
        sources: ['rescan'],
        firstSeenAt: '2026-09-21T00:00:00Z',
      },
    ],
    coverage: {
      inService: 3,
      withSbom: 2,
      rescanned: 1,
      rescanFailed: 0,
      oldestRescanAt: '2026-09-22T08:00:00Z',
      notCovered: [
        {
          moduleId: 'legacy-app',
          environment: 'prod',
          artifactDigest: 'sha256:9999',
          reason: 'no SBOM recorded',
        },
      ],
    },
  }

  const emptyExposure: VulnerabilityExposure = {
    ...baseExposure,
    affected: [],
  }

  return { mockExposure: baseExposure, mockEmptyExposure: emptyExposure }
})

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    getRunningVulnerabilities: vi.fn().mockResolvedValue(mockExposure),
    getVulnerabilityExposure: vi.fn().mockResolvedValue(mockExposure),
    rescanVulnerabilities: vi.fn().mockResolvedValue({
      scanned: ['sha256:1111'],
      failed: [],
      skippedNoSbom: [],
      scanner: 'trivy',
    }),
  }
})

import {
  getRunningVulnerabilities,
  getVulnerabilityExposure,
  rescanVulnerabilities,
} from './api/netciClient'
import { VulnerabilitiesPage } from './VulnerabilitiesPage'

describe('VulnerabilitiesPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(getRunningVulnerabilities).mockResolvedValue(mockExposure)
    vi.mocked(getVulnerabilityExposure).mockResolvedValue(mockExposure)
    vi.mocked(rescanVulnerabilities).mockResolvedValue({
      scanned: ['sha256:1111'],
      failed: [],
      skippedNoSbom: [],
      scanner: 'trivy',
    })
  })

  it('renders affected rows from a mocked response', async () => {
    render(<VulnerabilitiesPage />)

    expect(await screen.findByTestId('vuln-affected')).toBeTruthy()
    expect(screen.getByText('payments-api')).toBeTruthy()
    expect(screen.getByText('prod')).toBeTruthy()
    expect(screen.getByText('CVE-2024-9999')).toBeTruthy()
    // The severity select offers CRITICAL too; the pill that matters is the one in the table.
    expect(within(screen.getByTestId('vuln-affected')).getByText('CRITICAL')).toBeTruthy()
    expect(screen.getByText('libssl3 3.0.2 → 3.0.3')).toBeTruthy()
    expect(screen.getByText('build scan, netCI rescan')).toBeTruthy()

    expect(screen.getByText('auth-service')).toBeTruthy()
    expect(screen.getByText('staging')).toBeTruthy()
    expect(screen.getByText('CVE-2024-8888')).toBeTruthy()
    expect(within(screen.getByTestId('vuln-affected')).getByText('HIGH')).toBeTruthy()
    expect(screen.getByText('curl 7.88.1 → no fix yet')).toBeTruthy()
    expect(screen.getByText('netCI rescan')).toBeTruthy()
  })

  it('an empty affected list shows the "No running artifact" text and never the words "safe" or "clean"', async () => {
    vi.mocked(getRunningVulnerabilities).mockResolvedValueOnce(mockEmptyExposure)
    const { container } = render(<VulnerabilitiesPage />)

    expect(await screen.findByText(/No running artifact is known to carry it/)).toBeTruthy()
    expect(screen.queryByText(/\bsafe\b/i)).toBeNull()
    expect(screen.queryByText(/\bclean\b/i)).toBeNull()
    expect(container.textContent).not.toMatch(/\bsafe\b/i)
    expect(container.textContent).not.toMatch(/\bclean\b/i)
  })

  it('notCovered entries are listed with the "not covered" sentence', async () => {
    render(<VulnerabilitiesPage />)

    expect(await screen.findByTestId('vuln-coverage')).toBeTruthy()
    expect(
      screen.getByText('These are not covered: the answer above says nothing about them.')
    ).toBeTruthy()
    expect(screen.getByText(/legacy-app · prod · no SBOM recorded/)).toBeTruthy()
    expect(
      screen.getByText(/3 running artifacts · 2 with SBOM · 1 rescanned · 0 rescans failed/)
    ).toBeTruthy()
    expect(screen.getByText(/last full rescan:/)).toBeTruthy()
  })

  it('searching an id calls getVulnerabilityExposure with it', async () => {
    const specificExposure = {
      ...mockExposure,
      vulnerabilityId: 'CVE-2024-1234',
      affected: [
        {
          ...mockExposure.affected[0],
          vulnerabilityId: 'CVE-2024-1234',
        },
      ],
    }
    vi.mocked(getVulnerabilityExposure).mockResolvedValueOnce(specificExposure)

    render(<VulnerabilitiesPage />)
    await screen.findByTestId('vuln-affected')

    const searchInput = screen.getByTestId('vuln-search')
    fireEvent.change(searchInput, { target: { value: 'CVE-2024-1234' } })
    fireEvent.click(screen.getByRole('button', { name: /search/i }))

    await waitFor(() => {
      expect(getVulnerabilityExposure).toHaveBeenCalledWith('CVE-2024-1234')
    })
    expect(await screen.findByText('CVE-2024-1234')).toBeTruthy()
  })

  it('a rescan error message is shown', async () => {
    vi.mocked(rescanVulnerabilities).mockRejectedValueOnce(
      new Error('rescan failed: 403 forbidden for non-admin')
    )

    render(<VulnerabilitiesPage />)
    await screen.findByTestId('vuln-affected')

    const rescanBtn = screen.getByRole('button', { name: /rescan now/i })
    fireEvent.click(rescanBtn)

    expect(
      await screen.findByText('rescan failed: 403 forbidden for non-admin')
    ).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain('rescan failed: 403 forbidden for non-admin')
  })

  it('shows "never rescanned" when oldestRescanAt is null', async () => {
    vi.mocked(getRunningVulnerabilities).mockResolvedValueOnce({
      ...mockExposure,
      coverage: {
        ...mockExposure.coverage,
        rescanned: 0,
        oldestRescanAt: null,
      },
    })

    render(<VulnerabilitiesPage />)
    expect(await screen.findByTestId('vuln-coverage')).toBeTruthy()
    expect(screen.getByText(/never rescanned/)).toBeTruthy()
  })
})
