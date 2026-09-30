# State machines

State transitions are owned by netCI. Jenkins or an adapter reports facts; it does not write arbitrary domain states.

## PipelineRun

```text
queued -> running -> succeeded
   |         |          |
   +---------+----------+-> failed
   |         |
   +---------+--------------> cancelled
             +--------------> waiting_approval -> running
successful deployment failure ----------------> rolled_back
```

Allowed terminal states are `succeeded`, `failed`, `cancelled` and `rolled_back`. A duplicate callback must be idempotent. A stale callback cannot move a terminal run backwards.

## Deployment

```text
pending_approval -> deploying -> healthy
       |               |          |
       |               +--------> failed -> rolled_back
       +-------------------------> failed
healthy -- explicit rollback ----------------> rolled_back
```

- Dev/staging may enter `deploying` after policy success according to environment policy.
- Production enters `pending_approval`; only an authorized reviewer/admin can advance it.
- Health failure triggers compensation/rollback when a previous healthy revision exists.
- Approval, denial, deployment and rollback each append an audit event.

## Transition data

Every transition records subject ID, previous/new state, timestamp, actor/system principal, correlation ID, reason and artifact digest where applicable. Compare-and-set/version checks are required once persistence is introduced.
