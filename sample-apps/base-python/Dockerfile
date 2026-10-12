# Golden base image for the sample applications.
#
# Patching happens here, once, rather than in every application Dockerfile. Two reasons:
#
#  1. Supply chain. Pinning a base freezes it at the vulnerabilities it shipped with;
#     the patch step is what keeps the Trivy gate passable without weakening it. Doing
#     it centrally means one rebuild fixes every application.
#  2. Build isolation. A `RUN` step needs a user namespace the build agent may not be
#     able to create (see docs/troubleshooting.md). Application builds that only COPY
#     files need no such privilege, so keeping package management out of them lets the
#     agent stay unprivileged.
ARG PYTHON_IMAGE=python:3.12-alpine
FROM ${PYTHON_IMAGE}

RUN apk upgrade --no-cache

LABEL org.opencontainers.image.title="netci-python-base" \
      org.opencontainers.image.description="Patched Python base for netCI sample applications"
