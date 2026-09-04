# Domain context

- **Application**: deployable software registered with netCI, owned by one team and bound to one runtime template.
- **Pipeline run**: one CI execution for an application commit and target environment. It records the verified requester, immutable artifact digest, external Jenkins/workflow ids and logs.
- **Security evidence**: durable SBOM, vulnerability scan and signature facts bound to one pipeline run and one immutable digest.
- **Policy decision**: allow or deny result calculated from security evidence. It is an audit fact, not a substitute for retaining the evidence.
- **Deployment**: attempt to place one immutable artifact digest in one environment through a runtime adapter.
- **Production request**: approval aggregate that selects registered module versions, schedule, ordering and rollback intent. Approval is not itself a deployment result.
- **System**: Portal grouping of modules owned by a business or platform unit.
- **Module**: Portal projection of one delivery application inside a system.
- **Version**: named release record for a module. A production-ready version must resolve to an immutable digest and its pipeline evidence.
- **Delivery event**: immutable source fact used to project DORA metrics. It is distinct from the audit ledger.
- **Audit record**: immutable account of a command or policy decision, including actor, correlation id and affected resources.
- **DCIM catalog**: external source of systems, modules and infrastructure inventory. An unconfigured catalog returns an explicit `not_configured` state and no substitute records.
- **Runtime target**: server, cluster namespace or service endpoint explicitly stored in module deployment configuration.
