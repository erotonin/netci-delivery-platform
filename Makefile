SHELL := /usr/bin/env bash

.PHONY: help doctor backend frontend compose-config compose-up kind-up registry-connect jenkins-up test validate e2e-container e2e-kubernetes e2e-systemd security-test benchmark failure-drill

help:
	@printf '%s\n' 'Targets: doctor validate test frontend compose-config compose-up kind-up registry-connect jenkins-up e2e-container e2e-kubernetes e2e-systemd security-test benchmark failure-drill'

doctor:
	@set -e; for tool in python3 docker kubectl kind helm ansible-playbook go syft trivy cosign; do command -v $$tool >/dev/null || { echo "missing required tool: $$tool" >&2; exit 1; }; done; docker info >/dev/null; echo 'doctor passed'

backend:
	cd backend && python3 -m uvicorn app.main:app --reload --port 8000

frontend:
	cd frontend && npm install && npm run dev

validate:
	python3 scripts/validate_windows.py
	python3 scripts/validate_catalog.py
	python3 scripts/validate_platform.py

test:
	python3 -m pytest backend/tests tests/contract -q
	$(MAKE) -C frontend build

compose-config:
	docker compose config

compose-up:
	docker compose up -d postgres registry minio temporalite

kind-up:
	kind create cluster --config infra/kind/kind-config.yaml

registry-connect:
	bash infra/kind/local-registry.sh

jenkins-up:
	docker compose up --build -d jenkins-a jenkins-b

e2e-container:
	python3 scripts/collect_evidence.py --name e2e-container make _e2e-container

e2e-kubernetes:
	python3 scripts/collect_evidence.py --name e2e-kubernetes make _e2e-kubernetes

e2e-systemd:
	python3 scripts/collect_evidence.py --name e2e-systemd make _e2e-systemd

_e2e-container:
	@echo 'Run container template through Portal/API, Jenkins CI, security gate and Ansible Docker VM.'
	@test -n "$(NETCI_E2E_READY)" || (echo 'set NETCI_E2E_READY=1 only after runtime is bootstrapped' && exit 1)

_e2e-kubernetes:
	@echo 'Run Kubernetes template through Helm/Ansible and verify health.'
	@test -n "$(NETCI_E2E_READY)" || (echo 'set NETCI_E2E_READY=1 only after runtime is bootstrapped' && exit 1)

_e2e-systemd:
	@echo 'Run Systemd template through Ansible SSH and verify rollback.'
	@test -n "$(NETCI_E2E_READY)" || (echo 'set NETCI_E2E_READY=1 only after runtime is bootstrapped' && exit 1)

security-test:
	@echo 'Security deny tests require Ubuntu runtime tools and evidence.'
	@test -n "$(NETCI_SECURITY_READY)" || (echo 'set NETCI_SECURITY_READY=1 only after Syft/Trivy/Cosign are configured' && exit 1)

benchmark:
	@python3 scripts/benchmark.py
	@test -f evidence/benchmark-report.json || (echo 'benchmark report missing; run real shared/ephemeral measurements' && exit 1)

failure-drill:
	@python3 scripts/failure_drill.py
	@test -f evidence/failure-drill/sample.json || exit 1
