from __future__ import annotations

import json
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
errors: list[str] = []

compose = yaml.safe_load((ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))
for service in {'postgres', 'registry', 'minio', 'temporalite', 'netci-api', 'jenkins-a', 'jenkins-b'}:
    if service not in compose.get('services', {}):
        errors.append(f'missing compose service: {service}')

base = yaml.safe_load((ROOT / 'jenkins/casc/base.yaml').read_text(encoding='utf-8'))
for key in {'jenkins', 'unclassified', 'credentials', 'jobs'}:
    if key not in base:
        errors.append(f'missing JCasC base section: {key}')
for controller in ('a', 'b'):
    overlay = yaml.safe_load((ROOT / f'jenkins/casc/controller-{controller}.yaml').read_text(encoding='utf-8'))
    text = str(overlay)
    if f'jenkins-{controller}' not in text:
        errors.append(f'controller overlay missing identity: {controller}')

chart = ROOT / 'deploy/helm/sample-kubernetes-app'
for required in ('Chart.yaml', 'values.yaml', 'templates/_helpers.tpl', 'templates/deployment.yaml'):
    if not (chart / required).exists():
        errors.append(f'missing Helm file: {required}')

schema = json.loads((ROOT / 'evidence/security-evidence.schema.json').read_text(encoding='utf-8'))
for required in ('artifactDigest', 'sbom', 'vulnerabilityScan', 'signature', 'decision'):
    if required not in schema.get('required', []):
        errors.append(f'security schema missing required field: {required}')

if errors:
    raise SystemExit('\n'.join(errors))
print('platform static validation passed')
