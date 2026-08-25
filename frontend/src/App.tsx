import { useMemo, useState } from 'react'
import './styles.css'

type Runtime = 'docker' | 'kubernetes' | 'systemd'

type Template = {
  id: string
  name: string
  runtime: Runtime
  stages: string[]
}

const templates: Template[] = [
  {
    id: 'container-ci-cd-v1',
    name: 'Container CI/CD',
    runtime: 'docker',
    stages: ['checkout', 'unit-test', 'build', 'sbom', 'vulnerability-scan', 'sign', 'publish', 'deploy', 'health-check'],
  },
  {
    id: 'kubernetes-ci-cd-v1',
    name: 'Kubernetes CI/CD',
    runtime: 'kubernetes',
    stages: ['checkout', 'unit-test', 'build', 'sbom', 'vulnerability-scan', 'sign', 'publish', 'deploy', 'health-check'],
  },
  {
    id: 'systemd-ansible-ci-cd-v1',
    name: 'Systemd Ansible CI/CD',
    runtime: 'systemd',
    stages: ['checkout', 'unit-test', 'build', 'publish', 'deploy', 'health-check'],
  },
]

const labels: Record<string, string> = {
  checkout: 'Checkout source',
  'unit-test': 'Unit tests',
  build: 'Build artifact/image',
  sbom: 'Generate SBOM',
  'vulnerability-scan': 'Vulnerability scan',
  sign: 'Sign artifact',
  publish: 'Publish artifact',
  deploy: 'Deploy through netCI',
  'health-check': 'Health check',
}

export default function App() {
  const [selectedId, setSelectedId] = useState(templates[0].id)
  const [applicationName, setApplicationName] = useState('hello-netci')
  const selected = useMemo(() => templates.find((item) => item.id === selectedId) ?? templates[0], [selectedId])

  return (
    <main className="shell">
      <header className="header">
        <div>
          <p className="eyebrow">netCI Delivery Platform</p>
          <h1>Golden path delivery</h1>
          <p className="muted">Cấu hình pipeline qua Portal, không cần mở Jenkins.</p>
        </div>
        <span className="status">LOCAL PROTOTYPE</span>
      </header>

      <section className="grid">
        <article className="card">
          <h2>Create application</h2>
          <label>
            Application name
            <input value={applicationName} onChange={(event) => setApplicationName(event.target.value)} />
          </label>
          <label>
            Pipeline template
            <select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>
              {templates.map((template) => <option key={template.id} value={template.id}>{template.name}</option>)}
            </select>
          </label>
          <div className="summary">
            <span>Runtime</span>
            <strong>{selected.runtime}</strong>
          </div>
          <button onClick={() => window.alert(`Application ${applicationName} ready for ${selected.id}`)}>Create application</button>
        </article>

        <article className="card">
          <div className="card-heading">
            <div>
              <p className="eyebrow">Stage catalog</p>
              <h2>{selected.name}</h2>
            </div>
            <span className="chip">{selected.stages.length} stages</span>
          </div>
          <div className="stage-list">
            {selected.stages.map((stage, index) => (
              <div className="stage" key={stage}>
                <span className="stage-number">{index + 1}</span>
                <span>{labels[stage]}</span>
                <span className="stage-kind">ready</span>
              </div>
            ))}
          </div>
        </article>
      </section>
    </main>
  )
}
