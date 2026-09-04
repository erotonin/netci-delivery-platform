import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const errors = []

const required = [
  'LICENSE',
  'NOTICE',
  'THIRD_PARTY_NOTICES.md',
  'CONTRIBUTING.md',
  'SECURITY.md',
  'CODE_OF_CONDUCT.md',
  'GOVERNANCE.md',
  'SUPPORT.md',
  'CHANGELOG.md',
  '.github/PULL_REQUEST_TEMPLATE.md',
  '.github/CODEOWNERS',
  '.github/ISSUE_TEMPLATE/bug_report.yml',
  '.github/ISSUE_TEMPLATE/feature_request.yml',
  '.github/dependabot.yml',
  '.github/workflows/sbom.yml',
  '.github/workflows/release.yml',
  '.github/workflows/dco.yml',
  'scripts/validate_dco.mjs',
  'docs/open-source/README.md',
  'contrib/upstream/README.md',
  'contrib/upstream/manifest.yaml',
  'deploy/ansible/galaxy.yml',
  'deploy/ansible/meta/runtime.yml',
  'deploy/ansible/meta/execution-environment.yml',
]

for (const relative of required) {
  if (!fs.existsSync(path.join(root, relative))) errors.push(`missing OSS file: ${relative}`)
}

const read = relative => fs.readFileSync(path.join(root, relative), 'utf8')
if (!read('LICENSE').includes('Apache License') || !read('LICENSE').includes('Version 2.0')) {
  errors.push('LICENSE is not the complete Apache-2.0 license')
}
if (!read('NOTICE').includes('netCI contributors')) errors.push('NOTICE has no copyright holder')
if (!/private\s+security advisory/i.test(read('SECURITY.md'))) {
  errors.push('SECURITY.md must provide a private reporting path')
}

for (const workflow of [
  '.github/workflows/ci.yml',
  '.github/workflows/sbom.yml',
  '.github/workflows/release.yml',
  '.github/workflows/dco.yml',
]) {
  for (const match of read(workflow).matchAll(/^\s*-?\s*uses:\s*([^\s#]+).*$/gm)) {
    const reference = match[1]
    if (!/@[0-9a-f]{40}$/.test(reference)) errors.push(`${workflow} action is not pinned to a full commit: ${reference}`)
  }
}
for (const requiredStep of [
  'actions/workflows/ci.yml/runs',
  'git archive',
  'sha256sum',
  'cosign sign-blob',
  'gh release upload',
]) {
  if (!read('.github/workflows/release.yml').includes(requiredStep)) {
    errors.push(`signed release workflow is missing: ${requiredStep}`)
  }
}

const jenkinsCandidate = read('contrib/upstream/jenkins-pipeline-examples/immutable-artifact-shared-library/vars/immutableArtifactPipeline.groovy')
if (!jenkinsCandidate.includes('SPDX-License-Identifier: MIT')) {
  errors.push('Jenkins upstream candidate has no target-compatible SPDX license')
}
if (/netci|localhost|erotonin/i.test(jenkinsCandidate)) {
  errors.push('Jenkins upstream candidate still contains project-specific names or URLs')
}
const sigstoreCandidate = read('contrib/upstream/sigstore-docs/deploy-time-verification.md')
for (const invariant of ['repository@digest', 'fail closed', 'trust root controlled outside the build job']) {
  if (!sigstoreCandidate.includes(invariant)) errors.push(`Sigstore candidate is missing invariant: ${invariant}`)
}

const frontend = JSON.parse(read('frontend/package.json'))
const lock = JSON.parse(read('frontend/package-lock.json'))
if (frontend.license !== 'Apache-2.0') errors.push('frontend package has no Apache-2.0 declaration')
for (const group of ['dependencies', 'devDependencies']) {
  for (const [name, version] of Object.entries(frontend[group] ?? {})) {
    const resolved = lock.packages?.[`node_modules/${name}`]
    if (!resolved) errors.push(`frontend lock is missing ${name}`)
    if (!resolved?.license) errors.push(`frontend lock has no SPDX license for direct dependency ${name}`)
    if (/^(latest|next|\*|[~^><=])/.test(version)) errors.push(`frontend dependency is not exact: ${name}=${version}`)
  }
}

for (const relative of ['backend/requirements.txt', 'backend/requirements-dev.txt', 'deploy/ansible/requirements.txt']) {
  for (const line of read(relative).split(/\r?\n/)) {
    const value = line.trim()
    if (!value || value.startsWith('#') || value.startsWith('-r ')) continue
    if (!/^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^\s]+$/.test(value)) {
      errors.push(`${relative} contains an unpinned requirement: ${value}`)
    }
  }
}

const dockerfiles = [
  'backend/Dockerfile',
  'backend/Dockerfile.worker',
  'frontend/Dockerfile',
  'jenkins/Dockerfile.controller',
  'jenkins/agent-toolbox/Dockerfile',
]
for (const relative of dockerfiles) {
  for (const match of read(relative).matchAll(/^FROM\s+([^\s]+).*$/gm)) {
    const image = match[1]
    if (image.includes('${')) continue
    if (image.includes(':latest') || !image.includes(':')) errors.push(`${relative} uses an unpinned base image: ${image}`)
  }
  for (const match of read(relative).matchAll(/^ARG\s+[A-Z0-9_]*IMAGE=([^\s]+)$/gm)) {
    const image = match[1]
    if (image.includes(':latest') || !image.includes(':')) errors.push(`${relative} has an unpinned image argument: ${image}`)
  }
}

for (const direct of [
  'FastAPI', 'Temporal Python SDK / Temporal', 'Psycopg', 'React / React DOM', 'Jenkins', 'MinIO',
  'Ansible Core', 'Syft', 'Trivy', 'Cosign / Sigstore', 'Backstage',
]) {
  if (!read('THIRD_PARTY_NOTICES.md').includes(`| ${direct} |`)) {
    errors.push(`THIRD_PARTY_NOTICES.md is missing ${direct}`)
  }
}

if (errors.length) {
  console.error([...new Set(errors)].join('\n'))
  process.exit(1)
}

console.log(`OSS readiness passed: ${required.length} governance files, exact direct dependencies and pinned base-image tags`)
