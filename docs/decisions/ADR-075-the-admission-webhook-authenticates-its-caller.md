# ADR-075: The admission webhook authenticates its caller

- Status: accepted
- Date: 2026-10-13
- Refines: ADR-024 (policy governance and admission)

## Context

`POST /admission/validate` is what a cluster's ValidatingWebhook calls for every pod. It took
any caller. Each call evaluated the image against supply-chain policy and **recorded a policy
decision** under the digest in the body, so anyone who could reach the API could:
- write a decision per request, with a target of their choosing, into the table auditors read;
- learn which digests netCI holds security evidence for, by the reason in the answer.

ADR-015 has the server decide every identity; this endpoint decided none.

## Decision

- The kube-apiserver authenticates to webhooks with a bearer token (or a client certificate)
  from the kubeconfig named in its `AdmissionConfiguration`. netCI compares the bearer token,
  in constant time, with `NETCI_ADMISSION_WEBHOOK_TOKEN_FILE` (or `NETCI_ADMISSION_WEBHOOK_TOKEN`).
  A missing or wrong token is 401 and nothing is evaluated or recorded.
- **Outside `NETCI_ENVIRONMENT=local`, no configured secret is a 503 to every caller.** It is
  never an admission: the cluster's `failurePolicy` then decides, and `Fail` is the setting
  that keeps the check.
- Local keeps answering without a token, as every local-only shortcut here does, so the demo and
  the test suite exercise the policy itself.
- The chart: `admission.webhook.enabled` with `secrets.admissionWebhookToken`, mounted as a file.
  The `AdmissionConfiguration` is cluster-level and stays outside the chart.

## Rejected

- **A workload-identity token** (the agents' kind). Those are capped at 24 h (ADR-015) and the
  apiserver's admission kubeconfig is not something to rewrite daily; raising the cap for one
  caller would weaken it for all.
- **Relying on the network** (only the apiserver can reach the API). The portal's API is reached
  by browsers and pipelines; a network rule is not an identity and is not visible in the code.
- **Skipping the record for unauthenticated calls but still answering.** The answer itself
  discloses which digests have evidence.
