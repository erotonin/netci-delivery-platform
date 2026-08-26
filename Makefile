SHELL := /usr/bin/env bash
PYTHON ?= python3
NPM ?= npm

.PHONY: help doctor doctor-windows backend frontend frontend-install frontend-build compose-config compose-up up kind-up registry-connect jenkins-up jenkins-rebuild test validate release-check release-windows release-ubuntu e2e-container e2e-kubernetes e2e-systemd security-test benchmark failure-drill dora-dashboard backstage-test

help:
	@printf '%s\n' \
	  'Portable: validate test frontend-install frontend-build release-check release-windows' \
	  'Ubuntu: doctor compose-config compose-up up kind-up registry-connect jenkins-up jenkins-rebuild release-ubuntu' \
	  'Blocked until real runners exist: e2e-container e2e-kubernetes e2e-systemd security-test benchmark failure-drill dora-dashboard backstage-test'

doctor:
	$(PYTHON) scripts/doctor.py --profile ubuntu

doctor-windows:
	$(PYTHON) scripts/doctor.py --profile windows

backend:
	cd backend && $(PYTHON) -m uvicorn app.main:app --reload --port 8000

frontend-install:
	$(NPM) --prefix frontend ci

frontend:
	$(NPM) --prefix frontend run dev

frontend-build:
	$(NPM) --prefix frontend run build

validate:
	$(PYTHON) scripts/validate_windows.py
	$(PYTHON) scripts/validate_catalog.py
	$(PYTHON) scripts/validate_platform.py
	$(PYTHON) scripts/validate_release.py

test:
	$(PYTHON) -m pytest backend/tests tests/contract -q
	$(NPM) --prefix frontend run build

release-check:
	$(PYTHON) scripts/validate_release.py

release-windows:
	$(PYTHON) scripts/validate_release.py --profile windows --execute

release-ubuntu:
	$(PYTHON) scripts/validate_release.py --profile ubuntu --execute

compose-config:
	docker compose config

compose-up:
	docker compose up -d postgres registry minio temporalite netci-api

up: compose-up

kind-up:
	kind create cluster --config infra/kind/kind-config.yaml

registry-connect:
	bash infra/kind/local-registry.sh

jenkins-up:
	docker compose up --build -d jenkins-a jenkins-b

jenkins-rebuild:
	docker compose up --build --force-recreate -d jenkins-a jenkins-b

e2e-container:
	@echo 'BLOCKED: implement a real Portal/API -> CI -> Docker VM runner with health and evidence assertions.' >&2
	@exit 2

e2e-kubernetes:
	@echo 'BLOCKED: implement a real CI/security -> Ansible/Helm -> kind runner with health and evidence assertions.' >&2
	@exit 2

e2e-systemd:
	@echo 'BLOCKED: implement a real binary -> Ansible SSH -> systemd runner with health and rollback assertions.' >&2
	@exit 2

security-test:
	@echo 'BLOCKED: implement a deny test that checks real SBOM, vulnerability and signature evidence.' >&2
	@exit 2

benchmark:
	@echo 'BLOCKED: configure and measure real shared/ephemeral builds; skeleton output is not release evidence.' >&2
	@exit 2

failure-drill:
	@echo 'BLOCKED: automate controller stop, detection, reroute, completion and rejoin before collecting MTTR.' >&2
	@exit 2

dora-dashboard:
	@echo 'BLOCKED: implement the dashboard and verify metrics against source events.' >&2
	@exit 2

backstage-test:
	@echo 'BLOCKED: run the Software Template in Backstage with the documented proxy configuration.' >&2
	@exit 2
