SHELL := /usr/bin/env bash
PYTHON ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)
NPM ?= npm
NODE ?= node

# Where the executable gates point themselves. Override on the command line when the
# lab runs on other ports, e.g. `make e2e-container NETCI_API_URL=http://127.0.0.1:8100`.
NETCI_API_URL ?= http://127.0.0.1:8000
NETCI_REGISTRY ?= 127.0.0.1:5000
NETCI_LAB_NETWORK_MODE ?= bridge
NETCI_LAB_APP_PORT ?= 18081
NETCI_LAB_SYSTEMD_PORT ?= 18082
NETCI_LAB_SYSTEMD_SCOPE ?= user
NETCI_LAB_MODE ?= compose
# The Kubernetes gate addresses the registry by the name the *cluster* resolves, which
# is not always the name this host uses. NETCI_KIND_PUSH_REGISTRY overrides the push
# address when the two differ.
NETCI_KIND_REGISTRY ?= localhost:5000
NETCI_KIND_PUSH_REGISTRY ?=
NETCI_REBUILD_CONTROLLER ?= a
NETCI_DRILL_VICTIM ?= jenkins-a
NETCI_BENCHMARK_RUNS ?= 3
NETCI_BACKSTAGE_URL ?= http://127.0.0.1:7007
NETCI_BACKUP_DIR ?= backups
NETCI_BACKUP ?=
GATE_ENV = NETCI_API_URL=$(NETCI_API_URL) NETCI_REGISTRY=$(NETCI_REGISTRY)

.PHONY: help doctor doctor-windows backend frontend frontend-install frontend-build oss-check \
	lab-up lab-down lab-status \
	compose-config compose-up up kind-up kind-down registry-connect jenkins-up jenkins-rebuild \
	migrate migrate-status schema test test-durability validate release-check release-portable release-windows release-ubuntu \
	e2e-container e2e-kubernetes e2e-systemd security-test benchmark failure-drill dora-dashboard backstage-test \
	jenkins-lab-up jenkins-lab-status jenkins-lab-down jenkins-ci-loop jenkins-rebuild-gate backstage-lab-up \
	gates gates-all clean-gates

help:
	@printf '%s\n' \
	  'Portable: validate test frontend-install frontend-build oss-check release-check release-portable' \
	  'Database: migrate migrate-status schema test-durability backup backup-verify' \
	  'Ubuntu lab: doctor lab-up lab-status lab-down compose-config kind-up registry-connect' \
	  'Jenkins lab: jenkins-lab-up jenkins-lab-status jenkins-lab-down  Backstage lab: backstage-lab-up' \
	  'Gates (lab):  security-test e2e-container e2e-kubernetes e2e-systemd dora-dashboard gates' \
	  'Gates (Jenkins lab): jenkins-ci-loop jenkins-rebuild-gate failure-drill benchmark backstage-test' \
	  'Everything:   gates-all' \
	  'Release: release-ubuntu'

# ---------------------------------------------------------------------- dev loop

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

# ------------------------------------------------------------------- database

migrate:
	$(PYTHON) scripts/migrate.py

migrate-status:
	$(PYTHON) scripts/migrate.py --status

schema:
	$(PYTHON) scripts/migrate.py --emit-schema

# --------------------------------------------------------------------- checks

validate:
	$(NODE) scripts/validate_oss_readiness.mjs
	$(PYTHON) scripts/validate_windows.py
	$(PYTHON) scripts/validate_catalog.py
	$(PYTHON) scripts/validate_platform.py
	$(PYTHON) scripts/migrate.py --check-schema
	$(PYTHON) scripts/validate_release.py

oss-check:
	$(NODE) scripts/validate_oss_readiness.mjs

test:
	$(PYTHON) -m pytest backend/tests tests/contract -q
	$(NPM) --prefix frontend test
	$(NPM) --prefix frontend run build

# Durability, concurrency and restart recovery need a real database; these are skipped
# unless NETCI_TEST_DATABASE_URL points at one that has been migrated.
test-durability:
	$(PYTHON) -m pytest backend/tests/test_persistence_postgres.py -v

release-check:
	$(PYTHON) scripts/validate_release.py

# Everything that runs anywhere with only Python and Node -- exactly what CI runs.
# A backup is a hope until it has been restored. `backup-verify` restores into a
# throwaway database and compares row counts and the migration ledger against what was
# recorded when the dump was taken.
backup:
	$(PYTHON) scripts/netci_backup.py create --output $(NETCI_BACKUP_DIR)

backup-verify:
	$(PYTHON) scripts/netci_backup.py verify --input $(NETCI_BACKUP)

release-portable:
	$(PYTHON) scripts/validate_release.py --profile portable --execute

release-windows:
	$(PYTHON) scripts/validate_release.py --profile windows --execute

release-ubuntu:
	$(PYTHON) scripts/validate_release.py --profile ubuntu --execute

# ------------------------------------------------------------------ local lab
#
# lab-up brings up the minimum the gates need (PostgreSQL, a registry, the API).
# NETCI_LAB_MODE=hostnet joins every container to the host network, which is what
# a Docker daemon without a bridge/NAT requires.

lab-up:
	bash scripts/lab.sh up $(NETCI_LAB_MODE)

lab-status:
	bash scripts/lab.sh status

lab-down:
	bash scripts/lab.sh down

compose-config:
	docker compose config

compose-up:
	docker compose up -d postgres registry minio temporalite netci-api

up: compose-up

kind-up:
	$(PYTHON) scripts/gate_kind.py

kind-down:
	kind delete cluster --name netci-local

registry-connect:
	bash infra/kind/local-registry.sh

jenkins-up:
	docker compose up --build -d jenkins-a jenkins-b

jenkins-rebuild:
	docker compose up --build --force-recreate -d jenkins-a jenkins-b

# ----------------------------------------------------------- executable gates
#
# Each of these runs real commands against real infrastructure and writes an evidence
# file under evidence/ with the commands, timings, outputs and assertions. They exit
# non-zero when an assertion fails, so they cannot report a green result they did not earn.

security-test:
	$(GATE_ENV) $(PYTHON) scripts/gate_security.py

e2e-container:
	$(GATE_ENV) NETCI_LAB_NETWORK_MODE=$(NETCI_LAB_NETWORK_MODE) NETCI_LAB_APP_PORT=$(NETCI_LAB_APP_PORT) \
	  $(PYTHON) scripts/gate_e2e_container.py \
	  --network-mode $(NETCI_LAB_NETWORK_MODE) --port $(NETCI_LAB_APP_PORT)

e2e-systemd:
	$(GATE_ENV) $(PYTHON) scripts/gate_e2e_systemd.py \
	  --scope $(NETCI_LAB_SYSTEMD_SCOPE) --port $(NETCI_LAB_SYSTEMD_PORT)

dora-dashboard:
	$(GATE_ENV) $(PYTHON) scripts/gate_dora.py

e2e-kubernetes:
	$(GATE_ENV) $(PYTHON) scripts/gate_e2e_kubernetes.py \
	  --registry $(NETCI_KIND_REGISTRY) \
	  $(if $(NETCI_KIND_PUSH_REGISTRY),--push-registry $(NETCI_KIND_PUSH_REGISTRY),)

gates: security-test e2e-container e2e-kubernetes e2e-systemd dora-dashboard

# Everything, including the gates that need the Jenkins lab and Backstage running.
gates-all: gates jenkins-ci-loop jenkins-rebuild-gate failure-drill benchmark backstage-test

clean-gates:
	rm -rf .netci-gate/e2e-container .netci-gate/e2e-kubernetes .netci-gate/e2e-systemd .netci-gate/security

# --------------------------------------------------- gates that need the Jenkins lab
#
# These drive live controllers, so they need `make jenkins-lab-up` (two controllers built
# from JCasC, a git server and the shared baseline agent) and `make kind-up` beforehand.
# `scripts/jenkins_lab.sh up` prints the environment to export; the targets below read the
# same variables, so `source .netci-gate/jenkins/lab.env` is enough to run them.

jenkins-lab-up:
	bash scripts/jenkins_lab.sh up

jenkins-lab-status:
	bash scripts/jenkins_lab.sh status

jenkins-lab-down:
	bash scripts/jenkins_lab.sh down

jenkins-ci-loop:
	$(GATE_ENV) $(PYTHON) scripts/gate_jenkins_ci.py

jenkins-rebuild-gate:
	$(GATE_ENV) $(PYTHON) scripts/gate_jenkins_rebuild.py --controller $(NETCI_REBUILD_CONTROLLER)

failure-drill:
	$(GATE_ENV) $(PYTHON) scripts/gate_failure_drill.py --victim $(NETCI_DRILL_VICTIM)

benchmark:
	$(GATE_ENV) $(PYTHON) scripts/gate_benchmark.py --runs $(NETCI_BENCHMARK_RUNS)

backstage-lab-up:
	bash scripts/backstage_lab.sh up

backstage-test:
	$(GATE_ENV) $(PYTHON) scripts/gate_backstage.py --backstage-url $(NETCI_BACKSTAGE_URL)
