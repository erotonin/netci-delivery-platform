ARG JENKINS_AGENT_BASE=jenkins/inbound-agent:3386.v353e57a_1b_ea_0-1-jdk21
FROM ${JENKINS_AGENT_BASE}

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash buildah ca-certificates curl git jq make python3 uidmap \
    && rm -rf /var/lib/apt/lists/*

# Tool versions and release checksums are intentionally explicit. Update both
# together when upgrading; a version-only download is not an integrity pin.
ARG SYFT_VERSION=1.51.0
ARG SYFT_SHA256_AMD64=2a2e837a2c8d59ec9af5472ee22d3b04ee463c4e44476ecf993fd1e5ab6ebc7f
ARG SYFT_SHA256_ARM64=6c0466811541ea03add5213a60a1562f0851e4c0b0ecfdee1a694a9455285900
ARG TRIVY_VERSION=0.73.0
ARG TRIVY_SHA256_AMD64=2edd39da482bb4e9831962487b68f68e3928ec3137794757f54d00383d79547b
ARG TRIVY_SHA256_ARM64=13833d97e8a1a5367471c372a173180157f593bece570e20d5d925fef552f5dd
ARG COSIGN_VERSION=3.1.2
ARG COSIGN_SHA256_AMD64=f7622ed3cf22e55e1ae6377c080979ff77a22da9981c11df222a2e444991e7cf
ARG COSIGN_SHA256_ARM64=90e7ae0b5dfd60f20816b52c012addf7fc055ebcc7bea4ce81c428ca8518c302
ARG GO_VERSION=1.27.0
ARG GO_SHA256_AMD64=675c26c449cbb18fc24b74650de1eabbae6e16f64326fd85a283fb3b58280685
ARG GO_SHA256_ARM64=51798d2c42d0f1c6ed7fd9f48728b4193abac9e8aad6dbac2fe96a81f5909bda
ARG TARGETARCH=amd64

RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) syft_sha="${SYFT_SHA256_AMD64}"; trivy_arch=64bit; trivy_sha="${TRIVY_SHA256_AMD64}"; cosign_sha="${COSIGN_SHA256_AMD64}"; go_sha="${GO_SHA256_AMD64}" ;; \
      arm64) syft_sha="${SYFT_SHA256_ARM64}"; trivy_arch=ARM64; trivy_sha="${TRIVY_SHA256_ARM64}"; cosign_sha="${COSIGN_SHA256_ARM64}"; go_sha="${GO_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /tmp/syft.tar.gz "https://github.com/anchore/syft/releases/download/v${SYFT_VERSION}/syft_${SYFT_VERSION}_linux_${TARGETARCH}.tar.gz"; \
    echo "${syft_sha}  /tmp/syft.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/syft.tar.gz -C /usr/local/bin syft; \
    curl -fsSLo /tmp/trivy.tar.gz "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-${trivy_arch}.tar.gz"; \
    echo "${trivy_sha}  /tmp/trivy.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/trivy.tar.gz -C /usr/local/bin trivy; \
    curl -fsSLo /usr/local/bin/cosign "https://github.com/sigstore/cosign/releases/download/v${COSIGN_VERSION}/cosign-linux-${TARGETARCH}"; \
    echo "${cosign_sha}  /usr/local/bin/cosign" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/cosign; \
    curl -fsSLo /tmp/go.tar.gz "https://go.dev/dl/go${GO_VERSION}.linux-${TARGETARCH}.tar.gz"; \
    echo "${go_sha}  /tmp/go.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/go.tar.gz -C /usr/local; \
    rm -f /tmp/syft.tar.gz /tmp/trivy.tar.gz /tmp/go.tar.gz; \
    grep -q '^jenkins:' /etc/subuid || echo 'jenkins:100000:65536' >> /etc/subuid; \
    grep -q '^jenkins:' /etc/subgid || echo 'jenkins:100000:65536' >> /etc/subgid

ENV PATH="/usr/local/go/bin:${PATH}" \
    BUILDAH_ISOLATION=chroot \
    STORAGE_DRIVER=vfs

RUN syft version && trivy --version && cosign version && go version && buildah version

USER 1000
