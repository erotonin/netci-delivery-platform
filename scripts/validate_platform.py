from __future__ import annotations

import json
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
errors: list[str] = []

compose_text = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
compose = yaml.safe_load(compose_text)
compose_services = compose.get('services', {})
if not ({'temporal', 'temporalite'} & set(compose_services)):
    errors.append('missing compose service: temporal or temporalite')
for service in {'postgres', 'registry', 'minio', 'netci-api', 'jenkins-a', 'jenkins-b'}:
    if service not in compose_services:
        errors.append(f'missing compose service: {service}')
for controller in ('a', 'b'):
    expected = '/var/jenkins_home/casc/ephemeral-agent.yaml'
    if expected not in compose_text:
        errors.append(f'Jenkins {controller} does not load ephemeral agent config')

base = yaml.safe_load((ROOT / 'jenkins/casc/base.yaml').read_text(encoding='utf-8'))
if 'systemMessage' in base.get('jenkins', {}):
    errors.append('JCasC base must not define controller-specific systemMessage')
if 'globalNodeProperties' in base.get('jenkins', {}):
    errors.append('JCasC base must not define controller-specific globalNodeProperties')
for controller in ('a', 'b'):
    overlay = yaml.safe_load((ROOT / f'jenkins/casc/controller-{controller}.yaml').read_text(encoding='utf-8'))
    text = str(overlay)
    if f'jenkins-{controller}' not in text:
        errors.append(f'controller overlay missing identity: {controller}')

plugins = (ROOT / 'jenkins/plugins.txt').read_text(encoding='utf-8').splitlines()
for line in plugins:
    if line.strip() and not line.startswith('#') and ':' not in line:
        errors.append(f'unpinned Jenkins plugin: {line}')

chart = ROOT / 'deploy/helm/sample-kubernetes-app'
for required in ('Chart.yaml', 'values.yaml', 'templates/_helpers.tpl', 'templates/deployment.yaml', 'templates/service.yaml'):
    if not (chart / required).exists():
        errors.append(f'missing Helm file: {required}')
if 'image.digest' not in (chart / 'templates/deployment.yaml').read_text(encoding='utf-8'):
    errors.append('Helm deployment does not require immutable digest')

schema = json.loads((ROOT / 'evidence/security-evidence.schema.json').read_text(encoding='utf-8'))
for required in ('artifactDigest', 'sbom', 'vulnerabilityScan', 'signature', 'decision'):
    if required not in schema.get('required', []):
        errors.append(f'security schema missing required field: {required}')

if errors:
    raise SystemExit('\n'.join(errors))
print('platform static validation passed')
