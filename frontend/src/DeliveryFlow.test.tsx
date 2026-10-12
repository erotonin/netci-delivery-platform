import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

vi.mock('./api/netciClient', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/netciClient')>()
  return {
    ...original,
    getModuleDeliveryRules: vi.fn().mockResolvedValue({
      moduleId: 'orders', defaulted: false, forkPullRequests: 'verify',
      triggers: [
        { on: 'push', branches: ['release/**'], deployTo: 'staging' },
        { on: 'tag', tags: ['v*'], registerVersion: true },
      ],
      promotion: { prod: { requireHealthyIn: 'staging', minSoakMinutes: 60 } },
    }),
  }
})

import type { PipelineRun } from './api/netciClient'
import { DeliveryFlowPanel, isPromotable, runIntent, triggerText } from './ModulePage'

const run = (over: Partial<PipelineRun>): PipelineRun => ({
  id: 'r1', applicationId: 'a1', status: 'succeeded', commitSha: 'a'.repeat(40), branch: 'main',
  environment: 'dev', parameters: {}, jenkinsRunId: null, workflowId: null, artifactDigest: `sha256:${'b'.repeat(64)}`,
  consoleUrl: null, retryOf: null, startedBy: 'dev1', createdAt: '2026-09-23T00:00:00Z', updatedAt: '2026-09-23T00:00:00Z',
  ...over,
})

describe('delivery intent in the portal', () => {
  it('names what started a run from the server record', () => {
    expect(triggerText(run({ trigger: { event: 'pull_request', pullRequest: 12, baseBranch: 'main', fromFork: true } }))).toBe('PR #12 → main (fork)')
    expect(triggerText(run({ trigger: { event: 'tag', tag: 'v1.2.0' } }))).toBe('tag v1.2.0')
    expect(triggerText(run({ trigger: { event: 'push', branch: 'feature/x' } }))).toBe('push feature/x')
    expect(triggerText(run({}))).toBe('manual')
  })

  it('says whether a run deploys, only builds or only verifies', () => {
    expect(runIntent(run({}))).toBe('deploys')
    expect(runIntent(run({ deployAfterBuild: false }))).toBe('build only')
    expect(runIntent(run({ deployAfterBuild: false, publishArtifact: false }))).toBe('verify only')
  })

  it('offers promotion only for a run that published an artifact', () => {
    expect(isPromotable(run({}))).toBe(true)
    expect(isPromotable(run({ publishArtifact: false, artifactDigest: null }))).toBe(false)
    expect(isPromotable(run({ status: 'running' }))).toBe(false)
    expect(isPromotable(run({ artifactDigest: null }))).toBe(false)
  })

  it('states the rules in force, first match first, and that prod needs a request', async () => {
    render(<DeliveryFlowPanel moduleId="orders" />)
    const panel = await screen.findByTestId('delivery-flow')
    const items = panel.querySelectorAll('li')
    expect(items[0].textContent).toBe('push release/** → build → deploy staging')
    expect(items[1].textContent).toBe('tag v* → build only + register version')
    expect(panel.textContent).toContain('verified only — never signed or published')
    expect(panel.textContent).toContain('prod needs the artifact healthy in staging for 60 min')
    expect(panel.textContent).toContain('only through a production request')
  })
})
