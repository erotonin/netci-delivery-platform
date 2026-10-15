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
import { isPromotable, runIntent, triggerText } from './ModulePage'
import { RulesPanel, promotionGate } from './DeliveryView'

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
    // A production request's release run builds nothing (the HA lab's first release read "manual build").
    expect(runIntent(run({ promotedFrom: 'r0', productionRequestId: 'req-1' }))).toBe('release')
    expect(triggerText(run({ promotedFrom: 'r0', productionRequestId: 'req-1', trigger: {} }))).toBe('production request')
  })

  it('offers promotion only for a run that published an artifact', () => {
    expect(isPromotable(run({}))).toBe(true)
    expect(isPromotable(run({ publishArtifact: false, artifactDigest: null }))).toBe(false)
    expect(isPromotable(run({ status: 'running' }))).toBe(false)
    expect(isPromotable(run({ artifactDigest: null }))).toBe(false)
  })

  it('states the rules in force, first match first, and that prod needs a request', () => {
    render(<RulesPanel rules={{
      moduleId: 'orders', defaulted: false, forkPullRequests: 'verify',
      triggers: [
        { on: 'push', branches: ['release/**'], deployTo: 'staging' },
        { on: 'tag', tags: ['v*'], registerVersion: true },
      ],
      promotion: { prod: { requireHealthyIn: 'staging', minSoakMinutes: 60 } },
    }} />)
    const panel = screen.getByTestId('delivery-rules')
    const items = panel.querySelectorAll('li')
    expect(items[0].textContent).toContain('Push to release/**')
    expect(items[0].textContent).toContain('deploy to staging')
    expect(items[1].textContent).toContain('Tag v*')
    expect(items[1].textContent).toContain('registers the version')
    expect(panel.textContent).toContain('verified only — never signed or published')
    expect(panel.textContent).toContain('once healthy in staging for 60 min')
    expect(panel.textContent).toContain('only through a production request')
  })

  it('lets an artifact move on only when the environment before it is healthy and has soaked', () => {
    const rules = { moduleId: 'm', defaulted: false, forkPullRequests: 'ignore' as const, triggers: [],
      promotion: { staging: { requireHealthyIn: 'dev' as const, minSoakMinutes: 30 } } }
    const now = Date.parse('2026-10-15T12:00:00Z')
    const dev = (status: string, minutesAgo: number) => ({ environment: 'dev' as const, status, artifactDigest: `sha256:${'c'.repeat(64)}`,
      pipelineRunId: 'r1', updatedAt: new Date(now - minutesAgo * 60000).toISOString() })
    expect(promotionGate(rules, { environment: 'dev', status: 'never_deployed' }, 'staging', now).tone).toBe('blocked')
    expect(promotionGate(rules, dev('failed', 90), 'staging', now)).toEqual({ tone: 'blocked', text: 'Development is failed, not healthy' })
    expect(promotionGate(rules, dev('healthy', 10), 'staging', now)).toEqual({ tone: 'wait', text: 'Soaking in development: 20 min of 30 left' })
    expect(promotionGate(rules, dev('healthy', 31), 'staging', now).tone).toBe('ok')
  })
})
