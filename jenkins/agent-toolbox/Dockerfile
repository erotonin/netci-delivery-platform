# Pin this base by digest in the Ubuntu runtime environment before release.
ARG JENKINS_AGENT_BASE=jenkins/inbound-agent:jdk21
FROM ${JENKINS_AGENT_BASE}

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl git jq make python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Runtime images are injected through build args in the lab release job.
ARG SYFT_VERSION=1.19.0
ARG TRIVY_VERSION=0.59.1
ARG COSIGN_VERSION=2.4.1
ARG TARGETARCH=amd64

RUN curl -fsSL "https://raw.githubusercontent.com/anchore/syft/main/install.sh" | sh -s -- -b /usr/local/bin "v${SYFT_VERSION}" \
    && curl -fsSL "https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh" | sh -s -- -b /usr/local/bin "v${TRIVY_VERSION}" \
    && curl -fsSL "https://github.com/sigstore/cosign/releases/download/v${COSIGN_VERSION}/cosign-linux-${TARGETARCH}" -o /usr/local/bin/cosign \
    && chmod +x /usr/local/bin/cosign

USER 1000
