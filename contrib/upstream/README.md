# Upstream contribution candidates

This directory contains sanitized patch candidates, not vendored runtime code.
Nothing under it is imported by netCI. Each candidate must be rebased onto its
upstream project, adapted to that project's style, tested there and submitted
with the contributor's identity and DCO/CLA requirements.

- `jenkins-pipeline-examples/` demonstrates a reusable immutable-artifact
  pipeline without netCI callbacks or credentials.
- `sigstore-docs/` explains why deployment should verify the exact digest again
  instead of trusting a CI-produced boolean.

Do not copy customer names, internal URLs or netCI secrets into an upstream PR.
