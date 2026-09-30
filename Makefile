SHELL := /usr/bin/env bash
PYTHON ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)

.PHONY: help build test vet check toolchain toolchain-check

help: ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

build: ## build every service binary into bin/
	@mkdir -p bin && if ls cmd/*/ >/dev/null 2>&1; then go build -o bin/ ./cmd/...; else echo "no services yet"; fi

PKGS = $(shell go list ./... 2>/dev/null)

test: ## unit tests (race detector on)
	@if [ -n "$(PKGS)" ]; then go test -race -count=1 ./...; else echo "no Go packages yet"; fi

vet: ## static checks
	@if [ -n "$(PKGS)" ]; then go vet ./...; fi
	@test -z "$$(gofmt -l $$(git ls-files "*.go"))" || { gofmt -l $$(git ls-files "*.go"); echo "gofmt: files above are not formatted"; exit 1; }

toolchain: ## regenerate jenkins/plugins.txt and the controller base from toolchain/versions.yaml
	$(PYTHON) scripts/toolchain_sync.py

toolchain-check: ## fail if they have drifted from toolchain/versions.yaml
	$(PYTHON) scripts/toolchain_sync.py --check

check: vet test toolchain-check ## everything CI runs
