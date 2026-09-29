import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import type { ToolchainStatus } from './api/netciClient'

const mockToolchainData: ToolchainStatus = {
  declared: {
    toolbox: {
      image: 'netci/ci-toolbox',
      tag: '0.4.0',
    },
    tools: {
      syft: {
        version: '1.51.0',
        sha256: '6e4776103a4b7caec46f560e9095fb8fc51ec30ad547d25e016a9ddfa8279434',
        url: 'https://github.com/anchore/syft/releases/download/v1.51.0/syft_1.51.0_linux_amd64.tar.gz',
      },
      trivy: {
        version: '0.73.0',
        sha256: '2093ea651c6b1b426613348c48a7350cb4a52fcbe46eb3032d665798950d603a',
        url: 'https://github.com/aquasecurity/trivy/releases/download/v0.73.0/trivy_0.73.0_Linux-64bit.tar.gz',
      },
      cosign: {
        version: '3.1.2',
        sha256: 'a937a7836ea7a329d443f115a31a9807577fc769e961952a6a682da05b1c55dc',
        url: 'https://github.com/sigstore/cosign/releases/download/v3.1.2/cosign-linux-amd64',
      },
      buildah: {
        version: 'apt',
        source: 'apt',
        package: 'buildah',
      },
    },
    trivyDb: {
      repository: '',
      maxAgeHours: 72,
    },
  },
  observed: [
    {
      controllerId: 'jenkins-prod-01',
      when: '2026-09-26T08:00:00Z',
      toolVersions: {
        syft: '1.51.0',
        trivy: '0.73.0',
        cosign: '3.1.2',
        buildah: '1.33.7',
        trivyDbUpdatedAt: '2026-09-26T02:00:00Z',
      },
    },
  ],
  drift: [],
  trivyDb: {
    maxAgeHours: 72,
    observedUpdatedAt: '2026-09-26T02:00:00Z',
    stale: false,
  },
}

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    getToolchain: vi.fn(),  // resolved in beforeEach: a vi.mock factory is hoisted above the data
  }
})

import { getToolchain } from './api/netciClient'
import { ToolchainPage } from './ToolchainPage'

describe('ToolchainPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(getToolchain).mockResolvedValue(mockToolchainData)
  })

  it('renders declared tools table and toolbox info', async () => {
    render(<ToolchainPage />)

    expect(await screen.findByTestId('declared-table')).toBeTruthy()
    expect(screen.getByText('Quản lý Toolchain')).toBeTruthy()
    expect(screen.getByText(/netci\/ci-toolbox:0.4.0/)).toBeTruthy()

    // Declared tools, read from the declared table: the observed table repeats versions
    const declared = within(screen.getByTestId('declared-table'))
    expect(declared.getByText('syft')).toBeTruthy()
    expect(declared.getByText('1.51.0')).toBeTruthy()
    expect(declared.getByText('trivy')).toBeTruthy()
    expect(declared.getByText('0.73.0')).toBeTruthy()
    expect(declared.getByText('cosign')).toBeTruthy()
    expect(declared.getByText('3.1.2')).toBeTruthy()
    expect(declared.getByText('buildah')).toBeTruthy()
    expect(declared.getByText('apt: buildah')).toBeTruthy()
  })

  it('renders observed controllers and tools', async () => {
    render(<ToolchainPage />)

    expect(await screen.findByTestId('observed-table')).toBeTruthy()
    expect(screen.getByText('jenkins-prod-01')).toBeTruthy()
    expect(screen.getByText('2026-09-26T08:00:00Z')).toBeTruthy()
    expect(screen.getByText('1.33.7')).toBeTruthy()
  })

  it('shows zero drift banner when there is no drift', async () => {
    render(<ToolchainPage />)

    expect(await screen.findByTestId('no-drift-banner')).toBeTruthy()
    expect(screen.getByText(/Không phát hiện sai lệch \(Zero Drift\)/)).toBeTruthy()
    expect(screen.queryByTestId('drift-panel')).toBeNull()
  })

  it('shows drift panel and highlights drifted tool when drift is detected', async () => {
    const driftedData: ToolchainStatus = {
      ...mockToolchainData,
      observed: [
        {
          controllerId: 'jenkins-staging-02',
          when: '2026-09-26T08:30:00Z',
          toolVersions: {
            syft: '1.50.0', // Drifted!
            trivy: '0.73.0',
            cosign: '3.1.2',
            buildah: '1.33.7',
            trivyDbUpdatedAt: '2026-09-26T02:00:00Z',
          },
        },
      ],
      drift: [
        {
          tool: 'syft',
          declared: '1.51.0',
          observed: '1.50.0',
          controllerId: 'jenkins-staging-02',
        },
      ],
    }

    vi.mocked(getToolchain).mockResolvedValueOnce(driftedData)
    render(<ToolchainPage />)

    expect(await screen.findByTestId('drift-panel')).toBeTruthy()
    expect(screen.getByTestId('drift-table')).toBeTruthy()
    expect(screen.getByText(/Phát hiện sai lệch phiên bản \(1 cảnh báo drift\)/)).toBeTruthy()
    const drift = within(screen.getByTestId('drift-table'))
    expect(drift.getByText('jenkins-staging-02')).toBeTruthy()
    expect(drift.getByText('1.50.0')).toBeTruthy()
    expect(screen.queryByTestId('no-drift-banner')).toBeNull()
  })

  it('renders Trivy DB fresh status when not stale', async () => {
    render(<ToolchainPage />)

    expect(await screen.findByTestId('trivy-fresh-status')).toBeTruthy()
    expect(screen.getByText(/Cơ sở dữ liệu Trivy hợp lệ \(Fresh\)/)).toBeTruthy()
    expect(screen.queryByTestId('trivy-stale-warning')).toBeNull()
  })

  it('renders Trivy DB stale warning when DB age exceeds threshold', async () => {
    const staleData: ToolchainStatus = {
      ...mockToolchainData,
      trivyDb: {
        maxAgeHours: 72,
        observedUpdatedAt: '2026-09-20T00:00:00Z',
        stale: true,
      },
    }

    vi.mocked(getToolchain).mockResolvedValueOnce(staleData)
    render(<ToolchainPage />)

    expect(await screen.findByTestId('trivy-stale-warning')).toBeTruthy()
    expect(screen.getByText(/Cảnh báo: Cơ sở dữ liệu Trivy đã quá hạn \(Stale\)!/)).toBeTruthy()
    expect(screen.getByText(/Vượt quá ngưỡng tối đa cho phép 72 giờ/)).toBeTruthy()
    expect(screen.queryByTestId('trivy-fresh-status')).toBeNull()
  })

  it('shows empty observed message when no controllers have reported yet', async () => {
    const emptyObservedData: ToolchainStatus = {
      ...mockToolchainData,
      observed: [],
    }

    vi.mocked(getToolchain).mockResolvedValueOnce(emptyObservedData)
    render(<ToolchainPage />)

    expect(await screen.findByText('Chưa có dữ liệu quan sát từ agent nào')).toBeTruthy()
  })

  it('displays error message when getToolchain rejects', async () => {
    vi.mocked(getToolchain).mockRejectedValueOnce(new Error('Network error 500'))
    render(<ToolchainPage />)

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByTestId('toolchain-error')).toBeTruthy()
    expect(screen.getByText(/Không thể tải dữ liệu toolchain: Network error 500/)).toBeTruthy()
  })

  it('re-fetches toolchain data when clicking refresh button', async () => {
    render(<ToolchainPage />)

    await screen.findByTestId('declared-table')
    expect(getToolchain).toHaveBeenCalledTimes(1)

    const refreshBtn = screen.getByText('Làm mới')
    fireEvent.click(refreshBtn)

    expect(getToolchain).toHaveBeenCalledTimes(2)
  })

  it("shows each controller's plugin drift against the declared set, and an unreadable one", async () => {
    vi.mocked(getToolchain).mockResolvedValueOnce({
      ...mockToolchainData,
      jenkins: {
        controller: { base: 'jenkins/jenkins:2.555.3-lts-jdk21@sha256:abc', image: 'netci/jenkins-controller', tag: '2.555.3-netci1' },
        plugins: { git: '5.10.1', 'credentials-binding': '728.v902a_273b_8947' },
        requires: ['git'],
        enforced: true,
        controllers: [
          { controllerId: 'jenkins-corp', readable: true, error: null, pluginCount: 2,
            drift: [{ plugin: 'credentials-binding', declared: '728.v902a_273b_8947', observed: '719.v80e905ef14eb_', kind: 'version' }] },
          { controllerId: 'jenkins-b', readable: false, error: 'JenkinsHttpError: plugin list returned 403', pluginCount: null, drift: [] },
        ],
      },
    })
    render(<ToolchainPage />)
    const panel = await screen.findByTestId('jenkins-plugins')
    expect(within(panel).getByText('netci/jenkins-controller:2.555.3-netci1')).toBeTruthy()
    expect(within(panel).getByText(/Lệch 1 plugin/)).toBeTruthy()
    expect(within(panel).getByText(/đang chạy 719.v80e905ef14eb_, khai báo 728.v902a_273b_8947/)).toBeTruthy()
    expect(within(panel).getByText(/Không đọc được/)).toBeTruthy()
    expect(within(panel).getByText(/sẽ không nhận build/)).toBeTruthy()
    fireEvent.click(within(panel).getByRole('button', { name: /Xem 2 plugin khai báo/ }))
    expect(within(panel).getAllByText('credentials-binding')).toHaveLength(2)
  })
})
