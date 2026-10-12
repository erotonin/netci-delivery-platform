#!/usr/bin/env bash
set -euo pipefail

VERSION="${VERSION:-v0.1.0}"
mkdir -p dist
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w -X main.version=${VERSION}" -o "dist/hello-systemd-${VERSION}" .
sha256sum "dist/hello-systemd-${VERSION}" | tee "dist/hello-systemd-${VERSION}.sha256"
