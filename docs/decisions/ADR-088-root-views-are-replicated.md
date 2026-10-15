# ADR-088: Root views are replicated; a job's pages reached through a view are ordered too

- Status: accepted; unit-tested, measured in the lab (below)
- Date: 2026-10-15
- Refines: ADR-076 (replicated homes), ADR-078 (owner routing)

## Context

A view at the top of Jenkins lives in Jenkins' own `config.xml`, together with the global settings
each replica gets from JCasC. That file is not replicated, so a view made on one replica never
reached the other: after a failover a person's views were gone. Other views were already
replicated:
- a folder's views are in the folder's configuration;
- a user's "My Views" are in the user's configuration.

A second defect was found while doing this. A job's pages are reached through the view a person is
looking at as well, `/view/team/job/app/configSubmit`; these are the links a view's own page gives.
`EditOrder` and `OwnerRouting` read only paths that start with `/job/`, so:
- an edit made there was not ordered at all;
- "Build now" there, on a replica that does not own the job, was declined without a word.

## Decision

1. **Each root view is an entry of the store, `@views/<name>`**, holding the view's own XML
   (`View.writeXml`).
   - A replica applies another's change with `View.updateByXml`, or with
     `View.createViewFromXML` + `Jenkins.addView`, or with `deleteView`.
   - The primary view is never deleted this way.
   - At start, a home publishes the views the store does not have, like its items. Views equal to
     the store's are held as applied, so they are not published again.
2. **A view request is ordered like an edit.** `EditOrder.editedView` maps the request to the view:
   - `/createView` (name);
   - `/view/<name>/` + `configSubmit`, `config.xml`, `submitDescription`, `doDelete`, `addJobToView`
     or `removeJobFromView`;
   - a view inside a view: the outer one.

   The view is locked, and the home catches up. After Jenkins has acted, still under the lock, every
   root view is compared with what this home last published or applied. What differs is published,
   and what is gone is published as deleted. This runs on the request's thread, not in a save
   listener: Jenkins saves holding its monitor, and applying another replica's view needs that
   monitor too.
3. **Other saves of Jenkins' configuration** (a list view following a renamed job, a script) compare
   the views a second later, on a timer, outside that monitor.
4. **`/view/<v>[/view/<w>…]` is skipped before a job's path is read**, in `EditOrder` and
   `OwnerRouting` alike.

## Tests

- **Plugin (JenkinsRule, fake store):**
  - a view made here is published;
  - its description set through the web is published under its lock, and its deletion is published;
  - another replica's view is made here, changed, deleted, and nothing is published back;
  - an edit through `/view/all/job/<j>/` is made under the job's lock.
- **`EditOrderTest`:** the view a request changes, nested views, `/view/<v>/job/<j>/…` for edits,
  moves and owner routing.

## Lab (controller netci34)

`lab/aa/phase10_views_probe.py`, `aa-phase10-views-20261015T024853Z.json`:

| Step | Seen on the other replica after |
|---|---|
| V1: a list view made on r0 (XML, as the API does) | 0.12 s |
| V2: a job added to it and its description set on r1 | 0.10 s |
| V3: a job's description set through `/view/<v>/job/<j>/` on r0 | 0.09 s |
| V4: the view deleted on r0 | 0.12 s |

## What it costs, said plainly

- **Which view is the primary one** stays each replica's own; JCasC sets it.
- **Every save of Jenkins' configuration** compares the root views a second later. It is a
  serialisation of each view, cheap for tens of views.
- **Two people editing the same view at once**, on two replicas, are ordered by its lock: the second
  is refused for a moment (409), as for jobs.
