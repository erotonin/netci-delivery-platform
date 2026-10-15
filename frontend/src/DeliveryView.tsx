import { useCallback, useEffect, useState, type ReactNode } from 'react'
import {
  ArrowRight, CheckCircle2, Clock3, FileCheck2, GitBranch, GitCommitHorizontal, GitPullRequest, Lock,
  Package, Play, Rocket, ShieldCheck, Tag, XCircle,
} from 'lucide-react'
import {
  getModuleDeliveryRules, getModuleOverview, promoteModuleArtifact,
  type DeliveryRules, type Environment, type EnvironmentState, type ModuleOverview, type PipelineRun,
} from './api/netciClient'
import { usePortalFeedback } from './PortalFeedback'
import { StatusPill } from './PortalShell'
import { RunStatus, runIntent, timeAgo, triggerText } from './ModulePage'
import './delivery.css'

/**
 * A module's delivery as it really runs (ADR-043): a commit is built once into a signed digest;
 * that digest -- not a rebuild -- is promoted dev → staging, each step allowed only when the
 * rules say so, and production is reached through a production request a second person approves.
 * The Portal used to show one "pipeline" per environment, each rebuilding from source, which is
 * not how the server works and not how a release is made.
 */

const ORDER: Environment[] = ['dev', 'staging', 'prod']
const LABEL: Record<Environment, string> = { dev: 'Development', staging: 'Staging', prod: 'Production' }
const shortDigest = (digest?: string | null) => (digest ? digest.replace(/^sha256:/, '').slice(0, 12) : '—')
const shortSha = (sha?: string | null) => (sha ? sha.slice(0, 7) : '—')
const person = (subject?: string | null) => (!subject ? '—' : /^[0-9a-f]{8}-[0-9a-f]{4}-/.test(subject) ? `${subject.slice(0, 8)}…` : subject)
const stateLabel = (status: string) => ({
  never_deployed: 'not deployed', healthy: 'healthy', failed: 'failed', rolled_back: 'rolled back',
  deploying: 'deploying', pending_approval: 'awaiting approval', rollback_in_progress: 'rolling back', cancelled: 'cancelled', rollback_failed: 'rollback failed',
}[status] ?? status.replace(/_/g, ' '))

type Gate = { tone: 'ok' | 'wait' | 'blocked' | 'info'; text: string }

/** Whether the artifact running in `from` may move on to `to` now, by the module's own rule --
 *  a hint: the server decides when the promotion is asked for, and its refusal is shown. */
export function promotionGate(rules: DeliveryRules | null, from: EnvironmentState | undefined, to: Environment, now = Date.now()): Gate {
  const rule = rules?.promotion?.[to]
  const needs = rule?.requireHealthyIn ?? (to === 'staging' ? 'dev' : null)
  if (!from || from.status === 'never_deployed' || !from.artifactDigest) {
    return { tone: 'blocked', text: `Nothing is deployed in ${LABEL[needs ?? 'dev'].toLowerCase()} yet` }
  }
  if (needs && from.status !== 'healthy') return { tone: 'blocked', text: `${LABEL[needs]} is ${stateLabel(from.status)}, not healthy` }
  const soak = rule?.minSoakMinutes ?? 0
  if (soak > 0 && from.updatedAt) {
    const left = Math.ceil(soak - (now - new Date(from.updatedAt).getTime()) / 60000)
    if (left > 0) return { tone: 'wait', text: `Soaking in ${LABEL[needs ?? 'dev'].toLowerCase()}: ${left} min of ${soak} left` }
  }
  return { tone: 'ok', text: needs ? `Healthy in ${LABEL[needs].toLowerCase()}${soak ? ` for ${soak}+ min` : ''}` : 'No prior environment required' }
}

function GateLine({ gate }: { gate: Gate }) {
  const Icon = gate.tone === 'ok' ? CheckCircle2 : gate.tone === 'wait' ? Clock3 : gate.tone === 'blocked' ? XCircle : Lock
  return <p className={`gate gate-${gate.tone}`}><Icon size={14} />{gate.text}</p>
}

/** The rule that deploys into dev, in words: "push to main". */
function devTrigger(rules: DeliveryRules | null): string {
  const rule = rules?.triggers.find((t) => t.deployTo === 'dev')
  if (!rule) return 'Deployed by a manual build'
  const patterns = (rule.on === 'tag' ? rule.tags : rule.branches) ?? []
  return `Deployed on ${rule.on === 'tag' ? 'tag' : rule.on === 'pull_request' ? 'pull request to' : 'push to'} ${patterns.join(', ')}`
}

function Lane({ state, gate, action, entry }: { state: EnvironmentState; gate: Gate; action?: ReactNode; entry: string }) {
  const deployed = state.status !== 'never_deployed'
  return <article className={`lane lane-${state.environment} ${deployed ? '' : 'lane-empty'}`} data-testid={`lane-${state.environment}`}>
    <header><div><span className={`env-dot env-dot-${state.environment}`} /><h3>{LABEL[state.environment]}</h3></div><StatusPill status={stateLabel(state.status)} /></header>
    {deployed ? <div className="lane-body">
      <div className="lane-version">{state.version ? <><Tag size={15} /><strong>{state.version}</strong></> : <><Package size={15} /><strong className="mono">{shortDigest(state.artifactDigest)}</strong><em>unversioned</em></>}</div>
      <dl>
        <div><dt>Digest</dt><dd className="mono" title={state.artifactDigest ?? ''}>{shortDigest(state.artifactDigest)}</dd></div>
        <div><dt>Commit</dt><dd className="mono" title={state.commitSha ?? ''}>{shortSha(state.commitSha)}</dd></div>
        <div><dt>Updated</dt><dd title={state.updatedAt}>{timeAgo(state.updatedAt)}</dd></div>
        <div><dt>Strategy</dt><dd>{state.strategy ?? 'rolling'}</dd></div>
        {state.approvedBy && <div><dt>Approved by</dt><dd title={state.approvedBy}>{person(state.approvedBy)}</dd></div>}
      </dl>
    </div> : <div className="lane-body lane-placeholder"><Package size={22} /><span>No release here yet</span></div>}
    <footer>
      <span className="lane-entry">{entry}</span>
      <GateLine gate={gate} />
      {action}
    </footer>
  </article>
}

function Evidence({ quality }: { quality: ModuleOverview['quality'] }) {
  const q = quality
  if (!q.source) return <section className="panel evidence-panel"><div className="panel-heading"><div><h2>Artifact evidence</h2><p>No successful build has produced an artifact yet.</p></div></div></section>
  const scanOk = q.scan?.status === 'passed' || (q.scan?.critical === 0 && q.scan?.high === 0)
  const tests = q.ciReport?.autoTest
  const coverage = q.ciReport?.coveragePercentage ?? q.ciReport?.coverage
  const items = [
    { key: 'sign', ok: !!q.signature?.verified, label: 'Signature', value: q.signature?.verified ? `${q.signature.provider ?? 'cosign'} verified` : 'not verified', icon: ShieldCheck },
    { key: 'sbom', ok: !!q.sbom?.present, label: 'SBOM', value: q.sbom?.present ? `${q.sbom.format ?? 'present'}` : 'none', icon: FileCheck2 },
    { key: 'scan', ok: !!q.scan?.scanner && scanOk, label: 'Vulnerabilities', value: q.scan?.scanner ? `critical ${q.scan.critical ?? '?'} · high ${q.scan.high ?? '?'}` : 'not scanned', icon: ShieldCheck },
    { key: 'tests', ok: tests === 'passed', label: 'Tests', value: tests ? `${tests}${typeof q.ciReport?.testsRun === 'number' ? ` · ${q.ciReport.testsRun}` : ''}${typeof coverage === 'number' ? ` · ${coverage}% cov.` : ''}` : 'not reported', icon: CheckCircle2 },
  ]
  return <section className="panel evidence-panel">
    <div className="panel-heading"><div><h2>Artifact evidence</h2><p>Latest artifact <span className="mono">{shortDigest(q.source.artifactDigest)}</span> from commit <span className="mono">{shortSha(q.source.commitSha)}</span> · {timeAgo(q.source.at)}</p></div>{q.decision && <StatusPill status={q.decision === 'allow' ? 'policy: allow' : `policy: ${q.decision}`} />}</div>
    <div className="evidence-grid">{items.map((item) => <div key={item.key} className={`evidence-item ${item.ok ? 'is-ok' : 'is-missing'}`}>
      <span className="evidence-icon"><item.icon size={16} /></span><div><span>{item.label}</span><strong>{item.value}</strong></div>
    </div>)}</div>
  </section>
}

function ruleIcon(on: string) {
  return on === 'tag' ? Tag : on === 'pull_request' ? GitPullRequest : GitBranch
}

export function RulesPanel({ rules }: { rules: DeliveryRules | null }) {
  if (!rules) return null
  return <section className="panel rules-panel" data-testid="delivery-rules">
    <div className="panel-heading"><div><h2>Delivery rules{rules.defaulted ? ' · defaults' : ''}</h2><p>What each repository event does, first match wins. Edit them in Configuration.</p></div></div>
    <ul>
      {rules.triggers.map((rule, i) => {
        const Icon = ruleIcon(rule.on)
        const patterns = (rule.on === 'tag' ? rule.tags : rule.branches) ?? []
        const when = rule.on === 'tag' ? `Tag ${patterns.join(', ')}` : rule.on === 'pull_request' ? `Pull request to ${patterns.join(', ')}` : `Push to ${patterns.join(', ')}`
        const then = rule.on === 'pull_request' && !rule.deployTo ? 'Verify: build and test, never signed or published'
          : rule.deployTo ? `Build, sign, publish → deploy to ${LABEL[rule.deployTo as Environment]?.toLowerCase() ?? rule.deployTo}` : 'Build, sign and publish (promote later)'
        // Stated only when the rule sets them: the server serialises what the module declared.
        const chips = [
          rule.cancelInProgress === undefined ? '' : rule.cancelInProgress ? 'a newer build cancels this one' : 'never cancelled by a newer build',
          rule.paths?.length ? `only when ${rule.paths.join(', ')} changed` : '',
          rule.pathsIgnore?.length ? `not when only ${rule.pathsIgnore.join(', ')} changed` : '',
        ].filter(Boolean)
        return <li key={i}><span className="rule-icon"><Icon size={15} /></span><span className="rule-when">{when}</span><ArrowRight size={14} className="rule-arrow" /><span className="rule-then">{then}{rule.registerVersion ? ' · registers the version' : ''}{chips.length > 0 && <span className="rule-chips">{chips.map((c) => <em key={c}>{c}</em>)}</span>}</span></li>
      })}
      {Object.entries(rules.promotion).filter(([env]) => env !== 'dev').map(([env, rule]) => <li key={`p-${env}`}><span className="rule-icon"><Rocket size={15} /></span><span className="rule-when">Promote to {LABEL[env as Environment]?.toLowerCase() ?? env}</span><ArrowRight size={14} className="rule-arrow" /><span className="rule-then">{rule.requireHealthyIn ? `the same digest, once healthy in ${LABEL[rule.requireHealthyIn].toLowerCase()}${rule.minSoakMinutes ? ` for ${rule.minSoakMinutes} min` : ''}` : 'no prior environment required'}</span></li>)}
      <li><span className="rule-icon"><Lock size={15} /></span><span className="rule-when">Release to production</span><ArrowRight size={14} className="rule-arrow" /><span className="rule-then">only through a production request, approved by someone other than the requester</span></li>
    </ul>
    <p className="rules-foot">Fork pull requests: {rules.forkPullRequests === 'ignore' ? 'ignored' : 'verified only — never signed or published'}.
      {/* A path filter reads as "this change was skipped" unless it says when it is not applied (ADR-051). */}
      {rules.triggers.some((rule) => rule.paths?.length || rule.pathsIgnore?.length) && ' Path filters skip a build only when the repository reported every changed file; otherwise the rule matches and the build says so. Pull requests always build in full.'}</p>
  </section>
}

export function DeliveryTab({ moduleId, onOpenRun, onRunBuild, onRequestRelease }: { moduleId: string; onOpenRun: (run: PipelineRun) => void; onRunBuild: () => void; onRequestRelease: () => void }) {
  const { notify } = usePortalFeedback()
  const [overview, setOverview] = useState<ModuleOverview | null>(null)
  const [rules, setRules] = useState<DeliveryRules | null>(null)
  const [error, setError] = useState('')
  const [promoting, setPromoting] = useState<Environment | null>(null)
  const load = useCallback(() => {
    let active = true
    getModuleOverview(moduleId).then((result) => { if (active) { setOverview(result); setError('') } }).catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : 'Unable to load module overview.') })
    getModuleDeliveryRules(moduleId).then((result) => { if (active) setRules(result) }).catch(() => undefined)
    return () => { active = false }
  }, [moduleId])
  useEffect(() => load(), [load])

  if (error) return <section className="panel empty-tab-state"><XCircle size={28} /><h2>Delivery state unavailable</h2><p>{error}</p></section>
  if (!overview) return <section className="panel empty-tab-state"><Clock3 size={28} /><h2>Loading delivery state</h2><p>Reading environments, builds and evidence from netCI.</p></section>

  const env = (name: Environment): EnvironmentState => overview.environments.find((e) => e.environment === name) ?? { environment: name, status: 'never_deployed' }
  const dev = env('dev'), staging = env('staging'), prod = env('prod')
  const promote = async (from: EnvironmentState, to: Environment) => {
    if (!from.pipelineRunId) return
    setPromoting(to)
    try {
      const result = await promoteModuleArtifact(moduleId, { pipelineRunId: from.pipelineRunId, environment: to })
      notify(`Promoting ${shortDigest(result.artifactDigest)} to ${LABEL[to].toLowerCase()} — ${result.status}.`)
      load()
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : 'The promotion was refused.', 'error')
    } finally {
      setPromoting(null)
    }
  }
  const stagingGate = promotionGate(rules, dev, 'staging')
  const sameInStaging = !!dev.artifactDigest && dev.artifactDigest === staging.artifactDigest
  const prodGate: Gate = staging.status === 'healthy'
    ? { tone: 'info', text: 'Needs a production request and a second approver' }
    : { tone: 'blocked', text: staging.status === 'never_deployed' ? 'Nothing has reached staging yet' : `Staging is ${stateLabel(staging.status)}` }
  const hasActivity = overview.recentRuns.length > 0 || overview.recentDeployments.length > 0

  return <div className="delivery">
    <section className="panel promotion-path">
      <div className="panel-heading"><div><h2>Promotion path</h2><p>One build, one signed digest, moved forward environment by environment. Nothing is rebuilt on the way.</p></div>
        <button className="primary-button" onClick={onRunBuild}><Play size={15} />Run build</button></div>
      <div className="lanes">
        <Lane state={dev} entry={devTrigger(rules)} gate={dev.status === 'never_deployed' ? { tone: 'info', text: 'Waiting for the first build' } : { tone: dev.status === 'healthy' ? 'ok' : 'blocked', text: dev.status === 'healthy' ? 'Running and verified healthy' : `Last deployment ${stateLabel(dev.status)}` }}
          action={<button className="secondary-button lane-action" onClick={onRunBuild}><Play size={15} />Build a commit</button>} />
        <div className="lane-arrow" aria-hidden="true"><ArrowRight size={18} /></div>
        <Lane state={staging} entry="Promoted from development" gate={sameInStaging ? { tone: 'ok', text: 'Already running what development runs' } : stagingGate}
          action={<button className="secondary-button lane-action" data-testid="promote-staging" disabled={promoting !== null || stagingGate.tone !== 'ok' || sameInStaging || !dev.pipelineRunId} onClick={() => promote(dev, 'staging')}>
            <Rocket size={15} />{promoting === 'staging' ? 'Promoting…' : dev.artifactDigest ? `Promote ${dev.version ?? shortDigest(dev.artifactDigest)}` : 'Promote to staging'}</button>} />
        <div className="lane-arrow" aria-hidden="true"><ArrowRight size={18} /></div>
        <Lane state={prod} entry="Released by production request" gate={prodGate}
          action={<button className="secondary-button lane-action" data-testid="request-prod" disabled={staging.status !== 'healthy'} onClick={onRequestRelease}><Lock size={15} />Request release</button>} />
      </div>
    </section>

    <Evidence quality={overview.quality} />

    {hasActivity ? <div className="delivery-grid">
      <section className="panel"><div className="panel-heading"><div><h2>Recent builds</h2><p>Every build of this module, newest first</p></div></div>
        <div className="build-list">{overview.recentRuns.map((run) => <button className="build-row" key={run.id} onClick={() => onOpenRun(run)} title={run.id}>
          <RunStatus run={run} />
          <span className="build-main"><strong><GitCommitHorizontal size={14} /><span className="mono">{shortSha(run.commitSha)}</span> · {run.branch}</strong><small>{triggerText(run)} · {runIntent(run) === 'deploys' ? `deploys to ${run.environment}` : runIntent(run)}{run.releaseTag ? ` · ${run.releaseTag}` : ''}</small></span>
          <span className="build-meta"><span className="mono">{shortDigest(run.artifactDigest)}</span><small>{person(run.startedBy)} · {timeAgo(run.createdAt)}</small></span>
        </button>)}</div>
      </section>
      <section className="panel"><div className="panel-heading"><div><h2>Recent deployments</h2><p>What reached each environment, and who approved it</p></div></div>
        <div className="build-list">{overview.recentDeployments.map((d) => <div className="build-row static" key={d.id} title={d.id}>
          <StatusPill status={stateLabel(d.status)} />
          <span className="build-main"><strong><span className={`env-dot env-dot-${d.environment}`} />{LABEL[d.environment] ?? d.environment}</strong><small><span className="mono">{shortDigest(d.artifactDigest)}</span>{d.version ? ` · ${d.version}` : ''}</small></span>
          <span className="build-meta"><span>{person(d.approvedBy)}</span><small>{timeAgo(d.updatedAt)}</small></span>
        </div>)}{!overview.recentDeployments.length && <div className="empty-table"><strong>No deployments yet</strong><span>A build that deploys, or a promotion, appears here.</span></div>}</div>
      </section>
    </div> : <section className="panel empty-tab-state first-build">
      <Rocket size={28} /><h2>No builds yet</h2>
      <p>A push to the branch in the rules below starts a build automatically once the repository's webhook points at netCI. You can also build any commit now.</p>
      <button className="primary-button" onClick={onRunBuild}><Play size={15} />Run the first build</button>
    </section>}

    <RulesPanel rules={rules} />
  </div>
}
