SHELL := /usr/bin/env bash
PYTHON ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)

.PHONY: help build test vet check toolchain toolchain-check image

help: ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

build: ## build every service binary into bin/
	@mkdir -p bin && if ls cmd/*/ >/dev/null 2>&1; then go build -o bin/ ./cmd/...; else echo "no services yet"; fi

VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
IMAGE ?= netci/netci:$(VERSION)

image: ## static linux/amd64 binaries and the scratch image holding them (build from a clean worktree)
	@case "$(VERSION)" in *-dirty) echo "refusing to build an image from uncommitted changes"; exit 1;; esac
	mkdir -p bin/linux-amd64
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags "-s -w" -o bin/linux-amd64/ ./cmd/...
	docker build -t $(IMAGE) .

PKGS = $(shell go list ./... 2>/dev/null)

test: ## unit tests (race detector on)
	@if [ -n "$(PKGS)" ]; then go test -race -count=1 ./...; else echo "no Go packages yet"; fi

vet: ## static checks
	@if [ -n "$(PKGS)" ]; then go vet ./...; fi
	@unformatted="$$(gofmt -l $$(go list -f '{{.Dir}}' ./... 2>/dev/null) </dev/null)"; \
	  test -z "$$unformatted" || { echo "$$unformatted"; echo "gofmt: files above are not formatted"; exit 1; }

toolchain: ## regenerate jenkins/plugins.txt and the controller base from toolchain/versions.yaml
	$(PYTHON) scripts/toolchain_sync.py

toolchain-check: ## fail if they have drifted from toolchain/versions.yaml
	$(PYTHON) scripts/toolchain_sync.py --check

check: vet test toolchain-check ## everything CI runs
