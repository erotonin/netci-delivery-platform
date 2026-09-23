# Built from the repository root (compose: context `.`), like the worker.
FROM ghcr.io/sigstore/cosign/cosign:v3.1.2 AS cosign

# kubectl drives per-project build namespaces (NETCI_BUILD_ISOLATION=kubernetes) and the
# ingress-nginx traffic router. Pinned to a cluster-compatible minor and checked against
# the published digest: a tool that rewrites cluster state is a supply-chain input.
FROM debian:bookworm-slim AS kubectl
ARG KUBECTL_VERSION=v1.34.11
ARG KUBECTL_SHA256=8efbb9435132a190920eb65a47a8c1ecf755ad85ab57a600c9bedbab460bb7a8
ADD https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl /kubectl
RUN echo "${KUBECTL_SHA256}  /kubectl" | sha256sum -c - && chmod 0755 /kubectl

FROM python:3.12.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# git: the run dialog lists a module's branches and tags with `git ls-remote`. Without it
# every Kubernetes install showed an empty commit field and a repository error, which
# reads like a slow Git server and is really a missing binary.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
# The API re-verifies artifact signatures and /readyz probes the verifier, so it needs
# cosign too -- the same 3.x the worker and the build agent use (a v2 verifier cannot
# read v3 bundle signatures).
COPY --from=cosign /ko-app/cosign /usr/local/bin/cosign
COPY --from=kubectl /kubectl /usr/local/bin/kubectl
# The migrations and their runner travel with the API image so a Kubernetes install can
# apply the schema as a Job before any replica starts: without them a fresh cluster came
# up against an empty database and every request failed on a missing relation.
# scripts/migrate.py resolves backend/migrations relative to itself (parents[1]), so the
# layout mirrors the repository.
COPY backend/migrations ./backend/migrations
COPY backend/schema.sql ./backend/schema.sql
COPY scripts/migrate.py ./scripts/migrate.py

ENV NETCI_COSIGN_EXECUTABLE=/usr/local/bin/cosign

RUN useradd --create-home --uid 10001 netci \
    && chown -R netci:netci /app

USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
