# Security policy

## Supported versions

Until the first stable release, security fixes are provided only for the latest
commit on `main`. Published releases will document their support window here.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's private
security advisory flow for this repository. If that feature is unavailable,
contact the repository owner through a private channel shown on the GitHub
profile and include "netCI security" in the subject.

Include the affected commit or version, prerequisites, reproduction steps,
impact, and any suggested mitigation. Do not include real credentials or data.
Expect acknowledgement within five business days. A fix, disclosure date and
credit will be coordinated according to severity and deployment impact.

## Security boundaries

The threat model and trust seams are documented in `docs/security-model.md`.
In particular:

- browser-supplied actor, owner, host, script and secret paths are untrusted;
- CI evidence is recorded but signatures are re-verified at deployment when
  Cosign mode is enabled;
- production promotion must reuse the approved immutable artifact;
- machine credentials cannot approve a deployment, and human credentials
  cannot report machine callbacks;
- `NETCI_AUTH_MODE=none` is restricted to a true loopback client.

Reports caused only by explicitly documented local/demo modes are still
welcome when those modes can escape their stated boundary.
