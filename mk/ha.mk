# The Jenkins HA layer (Go services, the Jenkins plugin, the controller toolchain): included by the
# top-level Makefile, whose targets are the IDP's. `make check` here is the Go gate; `make test` there
# is the IDP's pytest + frontend gate.
.PHONY: build go-test vet check check-pg plugin toolchain toolchain-check image

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

go-test: ## Go unit tests (race detector on); PostgreSQL tests skip unless NETCI_QUEUE_TEST_DATABASE_URL is set
	@if [ -n "$(PKGS)" ]; then go test -race -count=1 ./...; else echo "no Go packages yet"; fi

check-pg: ## make check, refusing to let the PostgreSQL tests skip
	@test -n "$$NETCI_QUEUE_TEST_DATABASE_URL" || { echo "NETCI_QUEUE_TEST_DATABASE_URL is not set: the queue's tests would skip"; exit 1; }
	@$(MAKE) --no-print-directory check
	@! go test -count=1 -v ./internal/runqueue/ ./internal/fabric/ ./internal/homesync/ 2>&1 | grep -q -- '--- SKIP' || { echo "a PostgreSQL test skipped"; exit 1; }

plugin: ## build and test the Jenkins plugin (Maven in a container; cache in ~/.cache/netci-m2)
	@mkdir -p $$HOME/.cache/netci-m2
	docker run --rm --network host --user $$(id -u):$$(id -g) -e HOME=/tmp -v $$HOME/.cache/netci-m2:/m2 \
	  -v $(CURDIR)/jenkins/plugin:/src -w /src maven:3.9-eclipse-temurin-21 mvn -B -ntp -Dmaven.repo.local=/m2 verify

vet: ## static checks
	@if [ -n "$(PKGS)" ]; then go vet ./...; fi
	@unformatted="$$(gofmt -l $$(go list -f '{{.Dir}}' ./... 2>/dev/null) </dev/null)"; \
	  test -z "$$unformatted" || { echo "$$unformatted"; echo "gofmt: files above are not formatted"; exit 1; }

toolchain: ## regenerate jenkins/plugins.txt and the controller base from toolchain/versions.yaml
	$(PYTHON) scripts/toolchain_sync.py

toolchain-check: ## fail if they have drifted from toolchain/versions.yaml
	$(PYTHON) scripts/toolchain_sync.py --check

check: vet go-test toolchain-check ## everything the Go CI job runs
