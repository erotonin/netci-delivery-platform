import { useState } from 'react'
import {
  BookOpen, CheckCircle2, ChevronRight, Compass, Cpu,
  ExternalLink, GitBranch, Layers, Shield, Sparkles, Workflow, Zap, AlertTriangle
} from 'lucide-react'

type TabKey = 'benchmark' | 'truth-matrix' | 'traits' | 'agentic-mcp'

export function ArchitectureRoadmapPage() {
  const [activeTab, setActiveTab] = useState<TabKey>('benchmark')
  const [demoEnv, setDemoEnv] = useState<'prod' | 'staging' | 'dev'>('prod')
  const [simulatedGap, setSimulatedGap] = useState<'none' | 'unreleased' | 'undispatched' | 'crashloop'>('none')

  return (
    <div className="architecture-page" style={{ padding: '24px', maxWidth: '1440px', margin: '0 auto' }}>
      {/* Header Banner */}
      <header className="panel" style={{
        background: 'linear-gradient(135deg, rgba(30, 41, 59, 0.95) 0%, rgba(15, 23, 42, 0.98) 100%)',
        border: '1px solid rgba(255, 255, 255, 0.1)',
        padding: '28px 32px',
        borderRadius: '16px',
        marginBottom: '24px',
        boxShadow: '0 8px 32px rgba(0, 0, 0, 0.3)'
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
          <span style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: '6px',
            padding: '4px 12px',
            borderRadius: '999px',
            background: 'rgba(59, 130, 246, 0.2)',
            color: '#60a5fa',
            fontSize: '0.82rem',
            fontWeight: 600,
            border: '1px solid rgba(59, 130, 246, 0.3)'
          }}>
            <Sparkles size={14} /> IDP Evolution 2026
          </span>
          <span style={{ color: '#94a3b8', fontSize: '0.82rem' }}>
            Comparative study from ConfigHub, Kubara &amp; OpenChoreo
          </span>
        </div>
        <h1 style={{ fontSize: '1.85rem', fontWeight: 800, color: '#f8fafc', margin: '0 0 10px 0', letterSpacing: '-0.02em' }}>
          IDP 2026 Architectural Lessons &amp; netCI Strategic Roadmap
        </h1>
        <p style={{ color: '#cbd5e1', fontSize: '0.98rem', maxWidth: '960px', margin: 0, lineHeight: 1.6 }}>
          In-depth analysis of state-of-the-art Internal Developer Platform paradigms: the <em>&quot;Platform as a Record&quot;</em> and <em>&quot;4 Levels of Truth&quot;</em> philosophy by <strong>Alexis Richardson (ConfigHub)</strong>, the <em>Multi-Plane &amp; Traits</em> architecture by <strong>OpenChoreo (CNCF Sandbox)</strong>, and field-tested operational lessons from <strong>Xeus Nguyen</strong>.
        </p>

        <div style={{ display: 'flex', gap: '16px', marginTop: '20px', flexWrap: 'wrap' }}>
          <a
            href="https://confighub.com/blog/simpler-faster-and-safer-platform-engineering-with-confighub-and-kubara-9db869bd9816"
            target="_blank"
            rel="noreferrer"
            style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '0.84rem', color: '#93c5fd', textDecoration: 'none' }}
          >
            <ExternalLink size={14} /> ConfigHub &amp; Kubara Blog
          </a>
          <span style={{ color: '#475569' }}>•</span>
          <a
            href="https://wiki.xeusnguyen.xyz/Tech-Second-Brain/Personal/DevSecOps/Why-Internal-Developer-Platforms-(IDPs)-Matter-in-2026----OpenChoreo-and-the-Journey-of-Innovation#control-planes"
            target="_blank"
            rel="noreferrer"
            style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '0.84rem', color: '#93c5fd', textDecoration: 'none' }}
          >
            <ExternalLink size={14} /> Xeus Nguyen Wiki: IDPs in 2026
          </a>
          <span style={{ color: '#475569' }}>•</span>
          <a
            href="https://github.com/openchoreo/openchoreo"
            target="_blank"
            rel="noreferrer"
            style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '0.84rem', color: '#93c5fd', textDecoration: 'none' }}
          >
            <ExternalLink size={14} /> OpenChoreo GitHub (CNCF)
          </a>
        </div>
      </header>

      {/* Tabs Navigation */}
      <div style={{
        display: 'flex',
        gap: '8px',
        borderBottom: '1px solid rgba(255, 255, 255, 0.1)',
        paddingBottom: '8px',
        marginBottom: '24px',
        overflowX: 'auto'
      }}>
        {[
          { key: 'benchmark', label: 'Architecture Overview', icon: Compass },
          { key: 'truth-matrix', label: 'State Reconciliation', icon: Layers },
          { key: 'traits', label: 'Pluggable Traits', icon: Cpu },
          { key: 'agentic-mcp', label: 'Agentic AI & MCP', icon: Sparkles },
        ].map((tab) => {
          const Icon = tab.icon
          const isActive = activeTab === tab.key
          return (
            <button
              key={tab.key}
              onClick={() => setActiveTab(tab.key as TabKey)}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '8px',
                padding: '10px 18px',
                borderRadius: '8px',
                border: 'none',
                background: isActive ? 'rgba(59, 130, 246, 0.2)' : 'transparent',
                color: isActive ? '#60a5fa' : '#94a3b8',
                fontWeight: isActive ? 600 : 500,
                fontSize: '0.9rem',
                cursor: 'pointer',
                transition: 'all 0.15s ease',
                borderBottom: isActive ? '2px solid #3b82f6' : '2px solid transparent'
              }}
            >
              <Icon size={16} />
              {tab.label}
            </button>
          )
        })}
      </div>

      {/* Tab 1: Benchmark & Comparison */}
      {activeTab === 'benchmark' && (
        <section className="tab-content" style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
          <div className="panel" style={{ padding: '24px', borderRadius: '12px' }}>
            <h2 style={{ fontSize: '1.3rem', fontWeight: 700, marginTop: 0, marginBottom: '16px', color: '#f8fafc' }}>
              Comprehensive Architectural Comparison: netCI vs. Industry Leaders
            </h2>
            <p style={{ color: '#cbd5e1', fontSize: '0.92rem', marginBottom: '20px', lineHeight: 1.5 }}>
              While <strong>OpenChoreo</strong> and <strong>ConfigHub/Kubara</strong> focus strictly on Kubernetes environments, <strong>netCI</strong> maintains a distinctive advantage by natively supporting Docker Containers, Kubernetes clusters, and Linux Systemd Daemons — purpose-built for enterprise, financial, and hybrid legacy workloads.
            </p>

            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.88rem', textAlign: 'left' }}>
                <thead>
                  <tr style={{ background: 'rgba(255, 255, 255, 0.04)', borderBottom: '1px solid rgba(255, 255, 255, 0.1)' }}>
                    <th style={{ padding: '12px 16px', color: '#94a3b8' }}>Architectural Dimension</th>
                    <th style={{ padding: '12px 16px', color: '#94a3b8' }}>ConfigHub + Kubara</th>
                    <th style={{ padding: '12px 16px', color: '#94a3b8' }}>OpenChoreo (CNCF)</th>
                    <th style={{ padding: '12px 16px', color: '#38bdf8' }}>netCI Delivery Platform</th>
                  </tr>
                </thead>
                <tbody>
                  <tr style={{ borderBottom: '1px solid rgba(255, 255, 255, 0.06)' }}>
                    <td style={{ padding: '14px 16px', fontWeight: 600, color: '#f1f5f9' }}>Supported Runtimes</td>
                    <td style={{ padding: '14px 16px', color: '#cbd5e1' }}>Kubernetes only</td>
                    <td style={{ padding: '14px 16px', color: '#cbd5e1' }}>Kubernetes only</td>
                    <td style={{ padding: '14px 16px', color: '#34d399', fontWeight: 600 }}>
                      ✓ Docker, KinD/K8s, Linux Systemd Daemons
                    </td>
                  </tr>
                  <tr style={{ borderBottom: '1px solid rgba(255, 255, 255, 0.06)' }}>
                    <td style={{ padding: '14px 16px', fontWeight: 600, color: '#f1f5f9' }}>Configuration Governance &amp; CAS</td>
                    <td style={{ padding: '14px 16px', color: '#cbd5e1' }}>Units on OCI, GitOps via Argo</td>
                    <td style={{ padding: '14px 16px', color: '#cbd5e1' }}>K8s CRDs, Controller status</td>
                    <td style={{ padding: '14px 16px', color: '#34d399', fontWeight: 600 }}>
                      ✓ CAS v2 SHA-256, 3-way diff, Drift detection
                    </td>
                  </tr>
                  <tr style={{ borderBottom: '1px solid rgba(255, 255, 255, 0.06)' }}>
                    <td style={{ padding: '14px 16px', fontWeight: 600, color: '#f1f5f9' }}>Supply Chain Security</td>
                    <td style={{ padding: '14px 16px', color: '#cbd5e1' }}>Requires external toolchain</td>
                    <td style={{ padding: '14px 16px', color: '#cbd5e1' }}>Delegated to workflow plane</td>
                    <td style={{ padding: '14px 16px', color: '#34d399', fontWeight: 600 }}>
                      ✓ Automated: Syft SBOM, Trivy scan, Cosign signing
                    </td>
                  </tr>
                  <tr style={{ borderBottom: '1px solid rgba(255, 255, 255, 0.06)' }}>
                    <td style={{ padding: '14px 16px', fontWeight: 600, color: '#f1f5f9' }}>State Reconciliation</td>
                    <td style={{ padding: '14px 16px', color: '#60a5fa', fontWeight: 600 }}>
                      The 4 Levels of Truth
                    </td>
                    <td style={{ padding: '14px 16px', color: '#cbd5e1' }}>Operator reconciliation loop</td>
                    <td style={{ padding: '14px 16px', color: '#fbbf24' }}>
                      ⚡ Integrating State Grid based on ConfigHub
                    </td>
                  </tr>
                  <tr style={{ borderBottom: '1px solid rgba(255, 255, 255, 0.06)' }}>
                    <td style={{ padding: '14px 16px', fontWeight: 600, color: '#f1f5f9' }}>Service Abstraction</td>
                    <td style={{ padding: '14px 16px', color: '#cbd5e1' }}>Helm Catalog + Value overrides</td>
                    <td style={{ padding: '14px 16px', color: '#60a5fa', fontWeight: 600 }}>
                      Dual-API: ComponentType + Pluggable Traits
                    </td>
                    <td style={{ padding: '14px 16px', color: '#fbbf24' }}>
                      ⚡ Integrating Traits Engine into Wizard
                    </td>
                  </tr>
                  <tr style={{ borderBottom: '1px solid rgba(255, 255, 255, 0.06)' }}>
                    <td style={{ padding: '14px 16px', fontWeight: 600, color: '#f1f5f9' }}>Edge/Cloud Protocol</td>
                    <td style={{ padding: '14px 16px', color: '#cbd5e1' }}>Pull-based via ArgoCD</td>
                    <td style={{ padding: '14px 16px', color: '#60a5fa', fontWeight: 600 }}>
                      Outbound WebSocket (wss://) Zero Inbound
                    </td>
                    <td style={{ padding: '14px 16px', color: '#fbbf24' }}>
                      ⚡ Roadmap: netCI Runner Agent
                    </td>
                  </tr>
                  <tr>
                    <td style={{ padding: '14px 16px', fontWeight: 600, color: '#f1f5f9' }}>AI Agent &amp; MCP Readiness</td>
                    <td style={{ padding: '14px 16px', color: '#cbd5e1' }}>Approval bound to revision hash</td>
                    <td style={{ padding: '14px 16px', color: '#60a5fa', fontWeight: 600 }}>
                      Native MCP Server (Model Context Protocol)
                    </td>
                    <td style={{ padding: '14px 16px', color: '#fbbf24' }}>
                      ⚡ Roadmap: netCI MCP Adapter for RCA
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </section>
      )}

      {/* Tab 2: The 4 Levels of Truth */}
      {activeTab === 'truth-matrix' && (
        <section className="tab-content" style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
          <div className="panel" style={{ padding: '24px', borderRadius: '12px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: '16px', marginBottom: '16px' }}>
              <div>
                <h2 style={{ fontSize: '1.3rem', fontWeight: 700, margin: 0, color: '#f8fafc' }}>
                  The 4 Levels of Truth Matrix
                </h2>
                <p style={{ color: '#94a3b8', fontSize: '0.9rem', margin: '4px 0 0 0' }}>
                  Alexis Richardson theory: <em>&quot;Most outages happen in the gaps between these 4 truths.&quot;</em>
                </p>
              </div>

              {/* Interactive Simulator Controls */}
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px', background: 'rgba(255, 255, 255, 0.05)', padding: '6px 14px', borderRadius: '8px' }}>
                <span style={{ fontSize: '0.84rem', color: '#cbd5e1' }}>Simulate incident:</span>
                <select
                  value={simulatedGap}
                  onChange={(e) => setSimulatedGap(e.target.value as any)}
                  style={{ background: '#1e293b', border: '1px solid #475569', color: '#f8fafc', padding: '4px 8px', borderRadius: '6px', fontSize: '0.84rem' }}
                >
                  <option value="none">Zero Drift (Fully Converged)</option>
                  <option value="unreleased">Gap 1-2: Code missing CI/SBOM/Signatures</option>
                  <option value="undispatched">Gap 2-3: Built artifact not dispatched to CD</option>
                  <option value="crashloop">Gap 3-4: Dispatched but Runtime crash / port error</option>
                </select>
              </div>
            </div>

            {/* Matrix Visual Grid */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: '16px', marginTop: '20px' }}>
              {/* Level 1 */}
              <div style={{
                background: 'rgba(30, 41, 59, 0.6)',
                border: '1px solid rgba(255, 255, 255, 0.1)',
                padding: '18px',
                borderRadius: '10px'
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '8px' }}>
                  <span style={{ fontSize: '0.78rem', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 600 }}>Level 1: Desired Spec</span>
                  <span style={{ fontSize: '0.75rem', color: '#38bdf8' }}>CAS Revision #3</span>
                </div>
                <h3 style={{ margin: '0 0 6px 0', fontSize: '1.05rem', color: '#f8fafc' }}>Design Target</h3>
                <p style={{ fontSize: '0.84rem', color: '#cbd5e1', margin: '0 0 12px 0' }}>
                  Configuration defined in CAS v2 database (ports, replicas, env vars).
                </p>
                <div style={{ padding: '8px 12px', background: 'rgba(15, 23, 42, 0.6)', borderRadius: '6px', fontSize: '0.78rem', fontFamily: 'monospace', color: '#94a3b8' }}>
                  commit: 5a314b8 (main)<br />
                  runtime: docker / k8s
                </div>
                <div style={{ marginTop: '12px', display: 'flex', alignItems: 'center', gap: '6px', color: '#34d399', fontSize: '0.82rem', fontWeight: 600 }}>
                  <CheckCircle2 size={15} /> Spec Validated
                </div>
              </div>

              {/* Level 2 */}
              <div style={{
                background: 'rgba(30, 41, 59, 0.6)',
                border: simulatedGap === 'unreleased' ? '1px solid #ef4444' : '1px solid rgba(255, 255, 255, 0.1)',
                padding: '18px',
                borderRadius: '10px'
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '8px' }}>
                  <span style={{ fontSize: '0.78rem', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 600 }}>Level 2: Released Manifest</span>
                  <span style={{ fontSize: '0.75rem', color: simulatedGap === 'unreleased' ? '#ef4444' : '#38bdf8' }}>
                    {simulatedGap === 'unreleased' ? 'MISMATCH' : 'OCI Digest verified'}
                  </span>
                </div>
                <h3 style={{ margin: '0 0 6px 0', fontSize: '1.05rem', color: '#f8fafc' }}>Signed Package</h3>
                <p style={{ fontSize: '0.84rem', color: '#cbd5e1', margin: '0 0 12px 0' }}>
                  Artifact passed supply-chain gates (Syft SBOM, Trivy, Cosign).
                </p>
                <div style={{ padding: '8px 12px', background: 'rgba(15, 23, 42, 0.6)', borderRadius: '6px', fontSize: '0.78rem', fontFamily: 'monospace', color: '#94a3b8' }}>
                  {simulatedGap === 'unreleased' ? (
                    <span style={{ color: '#f87171' }}>⚠️ Missing Cosign signature for new commit</span>
                  ) : (
                    <>digest: sha256:5a314...<br />signature: verified (cosign)</>
                  )}
                </div>
                <div style={{ marginTop: '12px', display: 'flex', alignItems: 'center', gap: '6px', color: simulatedGap === 'unreleased' ? '#ef4444' : '#34d399', fontSize: '0.82rem', fontWeight: 600 }}>
                  {simulatedGap === 'unreleased' ? <><AlertTriangle size={15} /> Admission Gate Blocked</> : <><CheckCircle2 size={15} /> Artifact Provenance Verified</>}
                </div>
              </div>

              {/* Level 3 */}
              <div style={{
                background: 'rgba(30, 41, 59, 0.6)',
                border: simulatedGap === 'undispatched' ? '1px solid #ef4444' : '1px solid rgba(255, 255, 255, 0.1)',
                padding: '18px',
                borderRadius: '10px'
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '8px' }}>
                  <span style={{ fontSize: '0.78rem', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 600 }}>Level 3: Dispatched CD</span>
                  <span style={{ fontSize: '0.75rem', color: simulatedGap === 'undispatched' ? '#ef4444' : '#38bdf8' }}>
                    {simulatedGap === 'undispatched' ? 'PENDING SYNC' : 'Runner Succeeded'}
                  </span>
                </div>
                <h3 style={{ margin: '0 0 6px 0', fontSize: '1.05rem', color: '#f8fafc' }}>Deployment Orchestration</h3>
                <p style={{ fontSize: '0.84rem', color: '#cbd5e1', margin: '0 0 12px 0' }}>
                  Handoff status to target runtime environment via Local Runner / Jenkins.
                </p>
                <div style={{ padding: '8px 12px', background: 'rgba(15, 23, 42, 0.6)', borderRadius: '6px', fontSize: '0.78rem', fontFamily: 'monospace', color: '#94a3b8' }}>
                  {simulatedGap === 'undispatched' ? (
                    <span style={{ color: '#f87171' }}>⚠️ CD rollout blocked (Lock held by active release)</span>
                  ) : (
                    <>target: 127.0.0.1:18081<br />rollout: completed (0 downtime)</>
                  )}
                </div>
                <div style={{ marginTop: '12px', display: 'flex', alignItems: 'center', gap: '6px', color: simulatedGap === 'undispatched' ? '#ef4444' : '#34d399', fontSize: '0.82rem', fontWeight: 600 }}>
                  {simulatedGap === 'undispatched' ? <><AlertTriangle size={15} /> Deployment Lagging</> : <><CheckCircle2 size={15} /> Reconciled with Host</>}
                </div>
              </div>

              {/* Level 4 */}
              <div style={{
                background: 'rgba(30, 41, 59, 0.6)',
                border: simulatedGap === 'crashloop' ? '1px solid #ef4444' : '1px solid rgba(255, 255, 255, 0.1)',
                padding: '18px',
                borderRadius: '10px'
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '8px' }}>
                  <span style={{ fontSize: '0.78rem', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 600 }}>Level 4: Runtime Ready</span>
                  <span style={{ fontSize: '0.75rem', color: simulatedGap === 'crashloop' ? '#ef4444' : '#38bdf8' }}>
                    {simulatedGap === 'crashloop' ? 'CRASHING' : 'Health 200 OK'}
                  </span>
                </div>
                <h3 style={{ margin: '0 0 6px 0', fontSize: '1.05rem', color: '#f8fafc' }}>Live Production Health</h3>
                <p style={{ fontSize: '0.84rem', color: '#cbd5e1', margin: '0 0 12px 0' }}>
                  Liveness and readiness probe results from live service endpoint.
                </p>
                <div style={{ padding: '8px 12px', background: 'rgba(15, 23, 42, 0.6)', borderRadius: '6px', fontSize: '0.78rem', fontFamily: 'monospace', color: '#94a3b8' }}>
                  {simulatedGap === 'crashloop' ? (
                    <span style={{ color: '#f87171' }}>⚠️ HTTP 502 / Connection Refused on port 18081</span>
                  ) : (
                    <>endpoint: http://127.0.0.1:18081/healthz<br />latency: 2ms • 100% healthy</>
                  )}
                </div>
                <div style={{ marginTop: '12px', display: 'flex', alignItems: 'center', gap: '6px', color: simulatedGap === 'crashloop' ? '#ef4444' : '#34d399', fontSize: '0.82rem', fontWeight: 600 }}>
                  {simulatedGap === 'crashloop' ? <><AlertTriangle size={15} /> Runtime Outage (Auto-Rollback Triggered)</> : <><CheckCircle2 size={15} /> Service Serving Traffic</>}
                </div>
              </div>
            </div>

            {/* Diagnosis Banner */}
            <div style={{
              marginTop: '20px',
              padding: '16px 20px',
              borderRadius: '8px',
              background: simulatedGap === 'none' ? 'rgba(52, 211, 153, 0.1)' : 'rgba(239, 68, 68, 0.1)',
              border: simulatedGap === 'none' ? '1px solid rgba(52, 211, 153, 0.3)' : '1px solid rgba(239, 68, 68, 0.3)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between'
            }}>
              <div>
                <strong style={{ color: simulatedGap === 'none' ? '#34d399' : '#f87171', fontSize: '0.95rem' }}>
                  {simulatedGap === 'none' && '✓ Fully Converged: Zero drift detected across all four levels of truth.'}
                  {simulatedGap === 'unreleased' && '⚠️ Gap 1-2 Detected: Source code pushed but unverified by supply-chain security gates.'}
                  {simulatedGap === 'undispatched' && '⚠️ Gap 2-3 Detected: Verified artifact ready but CD rollout has not been dispatched.'}
                  {simulatedGap === 'crashloop' && '⚠️ Gap 3-4 Detected: Artifact deployed but runtime endpoint failed health probes. netCI recommends rollback to CAS #2.'}
                </strong>
                <p style={{ margin: '4px 0 0 0', fontSize: '0.85rem', color: '#cbd5e1' }}>
                  This matrix enables operators to identify root failure domains in seconds without SSH or searching distributed log sinks.
                </p>
              </div>
            </div>
          </div>
        </section>
      )}

      {/* Tab 3: Dual-API & Traits Engine */}
      {activeTab === 'traits' && (
        <section className="tab-content" style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
          <div className="panel" style={{ padding: '24px', borderRadius: '12px' }}>
            <h2 style={{ fontSize: '1.3rem', fontWeight: 700, marginTop: 0, color: '#f8fafc' }}>
              OpenChoreo Paradigm: Dual-API &amp; Pluggable Traits Engine
            </h2>
            <p style={{ color: '#cbd5e1', fontSize: '0.92rem', lineHeight: 1.6 }}>
              In OpenChoreo, application developers are not required to be Kubernetes YAML specialists. They declare a simple <strong>Component</strong> and attach pre-built <strong>Traits</strong> defined by Platform Engineers.
            </p>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: '16px', marginTop: '20px' }}>
              <div style={{ background: 'rgba(255, 255, 255, 0.03)', padding: '18px', borderRadius: '8px', border: '1px solid rgba(255, 255, 255, 0.08)' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '10px', color: '#60a5fa', fontWeight: 600 }}>
                  <Shield size={18} /> Ingress &amp; TLS Trait
                </div>
                <p style={{ fontSize: '0.85rem', color: '#cbd5e1', lineHeight: 1.5, margin: 0 }}>
                  Automated route allocation and TLS termination. When selected, netCI automatically configures reverse proxies (Caddy/Traefik or K8s Ingress) without manual routing rules.
                </p>
              </div>

              <div style={{ background: 'rgba(255, 255, 255, 0.03)', padding: '18px', borderRadius: '8px', border: '1px solid rgba(255, 255, 255, 0.08)' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '10px', color: '#34d399', fontWeight: 600 }}>
                  <Zap size={18} /> Event-Driven Autoscaling (KEDA)
                </div>
                <p style={{ fontSize: '0.85rem', color: '#cbd5e1', lineHeight: 1.5, margin: 0 }}>
                  Automatically scales pods or systemd worker daemons based on Kafka queue lag, RabbitMQ backlog, or CPU thresholds without complex HPA boilerplate.
                </p>
              </div>

              <div style={{ background: 'rgba(255, 255, 255, 0.03)', padding: '18px', borderRadius: '8px', border: '1px solid rgba(255, 255, 255, 0.08)' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '10px', color: '#f59e0b', fontWeight: 600 }}>
                  <Layers size={18} /> Zero-Trust Network Policy (eBPF)
                </div>
                <p style={{ fontSize: '0.85rem', color: '#cbd5e1', lineHeight: 1.5, margin: 0 }}>
                  Deny-all network isolation by default; explicitly authorizes traffic between modules in the same system or declared dependencies.
                </p>
              </div>
            </div>

            <div style={{ marginTop: '24px', padding: '16px', background: '#0f172a', borderRadius: '8px', border: '1px solid rgba(255, 255, 255, 0.1)' }}>
              <div style={{ fontSize: '0.82rem', color: '#94a3b8', marginBottom: '8px', fontWeight: 600 }}>
                Example Abstract Manifest applied in NewModuleWizard:
              </div>
              <pre style={{ margin: 0, color: '#38bdf8', fontSize: '0.82rem', fontFamily: 'monospace', lineHeight: 1.5 }}>
{`module:
  id: "payment-api"
  type: "backend"
  runtime: "docker" # or kubernetes, systemd
  traits:
    - type: "ingress-tls"
      properties: { host: "payment.corp.internal", auth: "oidc" }
    - type: "managed-database"
      properties: { engine: "postgresql", envVar: "DATABASE_URL" }
    - type: "health-probing"
      properties: { path: "/healthz", intervalSeconds: 5 }`}
              </pre>
            </div>
          </div>
        </section>
      )}

      {/* Tab 4: AI Agent & MCP */}
      {activeTab === 'agentic-mcp' && (
        <section className="tab-content" style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
          <div className="panel" style={{ padding: '24px', borderRadius: '12px' }}>
            <h2 style={{ fontSize: '1.3rem', fontWeight: 700, marginTop: 0, color: '#f8fafc' }}>
              Prepared for the AI Agentic SRE Era (Model Context Protocol - MCP)
            </h2>
            <p style={{ color: '#cbd5e1', fontSize: '0.92rem', lineHeight: 1.6 }}>
              Both <strong>OpenChoreo</strong> and <strong>ConfigHub</strong> identify 2026 as the inflection point where AI Agents actively assist operations. To eliminate unsupervised risk, the platform enforces <strong>Human-in-the-loop Approval Gates</strong> based on open <strong>MCP</strong> standards.
            </p>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '14px', marginTop: '20px' }}>
              <div style={{ display: 'flex', gap: '14px', alignItems: 'flex-start', background: 'rgba(255, 255, 255, 0.03)', padding: '16px', borderRadius: '8px' }}>
                <span style={{ padding: '6px 10px', background: 'rgba(59, 130, 246, 0.2)', color: '#60a5fa', borderRadius: '6px', fontWeight: 700, fontSize: '0.85rem' }}>STEP 1</span>
                <div>
                  <strong style={{ color: '#f8fafc', fontSize: '0.92rem' }}>Failure Detection &amp; Automated Root Cause Analysis (RCA)</strong>
                  <p style={{ margin: '4px 0 0 0', color: '#cbd5e1', fontSize: '0.84rem', lineHeight: 1.5 }}>
                    When a pipeline run or runtime container fails, an AI SRE Agent queries the <code>netci_get_stage_logs</code> MCP tool to parse stack traces and pinpoint root causes.
                  </p>
                </div>
              </div>

              <div style={{ display: 'flex', gap: '14px', alignItems: 'flex-start', background: 'rgba(255, 255, 255, 0.03)', padding: '16px', borderRadius: '8px' }}>
                <span style={{ padding: '6px 10px', background: 'rgba(168, 85, 247, 0.2)', color: '#c084fc', borderRadius: '6px', fontWeight: 700, fontSize: '0.85rem' }}>STEP 2</span>
                <div>
                  <strong style={{ color: '#f8fafc', fontSize: '0.92rem' }}>Propose CAS Draft Revision Remediation</strong>
                  <p style={{ margin: '4px 0 0 0', color: '#cbd5e1', fontSize: '0.84rem', lineHeight: 1.5 }}>
                    The AI crafts a proposed configuration fix (e.g. increasing memory limit from 256MB to 512MB to resolve OOMKilled) as a draft CAS revision with a precise 3-way diff.
                  </p>
                </div>
              </div>

              <div style={{ display: 'flex', gap: '14px', alignItems: 'flex-start', background: 'rgba(255, 255, 255, 0.03)', padding: '16px', borderRadius: '8px' }}>
                <span style={{ padding: '6px 10px', background: 'rgba(52, 211, 153, 0.2)', color: '#34d399', borderRadius: '6px', fontWeight: 700, fontSize: '0.85rem' }}>STEP 3</span>
                <div>
                  <strong style={{ color: '#f8fafc', fontSize: '0.92rem' }}>Human-in-the-Loop Approval Gate</strong>
                  <p style={{ margin: '4px 0 0 0', color: '#cbd5e1', fontSize: '0.84rem', lineHeight: 1.5 }}>
                    The Tech Lead reviews the notification in netCI Portal and approves the diff in one click. The platform then dispatches a zero-downtime CD rollout.
                  </p>
                </div>
              </div>
            </div>
          </div>
        </section>
      )}
    </div>
  )
}
