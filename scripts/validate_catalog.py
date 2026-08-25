from __future__ import annotations

import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'templates' / 'catalog.yaml'
ALLOWED_RUNTIMES = {'docker', 'kubernetes', 'systemd'}
REQUIRED_STAGES = {'checkout', 'unit-test', 'build', 'publish', 'deploy', 'health-check'}

catalog = yaml.safe_load(CATALOG.read_text(encoding='utf-8'))
errors: list[str] = []
for item in catalog.get('templates', []):
    template_id = item.get('id', '<unknown>')
    if item.get('runtime') not in ALLOWED_RUNTIMES:
        errors.append(f'{template_id}: unsupported runtime')
    missing = REQUIRED_STAGES - set(item.get('stages', []))
    if missing:
        errors.append(f'{template_id}: missing stages {sorted(missing)}')
    if set(item.get('environments', [])) != {'dev', 'staging', 'prod'}:
        errors.append(f'{template_id}: environments must be dev/staging/prod')

if errors:
    print('\n'.join(errors))
    raise SystemExit(1)

print(f'catalog valid: {len(catalog.get("templates", []))} templates')
