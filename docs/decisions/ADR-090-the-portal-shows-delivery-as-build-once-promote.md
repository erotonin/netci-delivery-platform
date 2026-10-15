# ADR-090: The Portal shows delivery as it runs: build once, promote the digest

- Status: accepted; unit-tested, browser-tested against the API (below)
- Date: 2026-10-15
- Refines: ADR-043 (CI and CD are separate acts), ADR-038/039 (production by request)

## Context

The server has delivered in the production way since ADR-043:
- a repository event builds a commit into one signed digest;
- a module's rules decide whether that build deploys, and to where;
- moving the digest onward is a promotion, which builds nothing, and is allowed once the
  environment before it is healthy (and has soaked);
- production is reached only through a production request that a second person approves.

The Portal still showed the model from before that. A module had one "pipeline" per environment
(Dev, Staging, Prod), each with a "Run Pipeline" button that built the commit again and deployed it
there. Production was offered as a build target "after approval". A person reading the module page
could not tell what was running where, which digest it was, or what stood between staging and
production. The user called the CI/CD "like a demo, not production". The page was why.

Reviewing it showed three more places where the Portal said something that was not so:
- **"Success rate 100%, 1 successful"** for a single run that was still queued. The dashboard
  counted every run that had not failed as a success.
- **A system marked "degraded"** because a build was queued, before anything had been deployed.
  A build says nothing about a service's runtime.
- **The demo data registered versions with a CI report** (42 tests passed, 94.5% coverage,
  "created by netCI Pipeline") that no build had produced. The Portal then showed that report
  beside "not deployed" everywhere.

## Decision

1. **The module page is organised the way delivery runs.** Tabs: Delivery, Builds, Releases,
   Configuration, Metrics.
   - **Delivery:** the promotion path, Development → Staging → Production. For each environment:
     what runs there (version or digest, commit, when, strategy, approver) and the gate that admits
     the next one.
     - "Promote" moves the digest that runs in development: the run recorded on that environment,
       `POST /modules/{id}/promotions`.
     - Production offers only "Request release", which opens production requests.
     - Below: the evidence of the latest artifact (signature, SBOM, vulnerabilities, tests), the
       recent builds and deployments, and the rules in force, written as "when … then …".
   - **Builds:** every run in one list, with its trigger (push, pull request, tag, manual), commit,
     artifact, and where it went. Filters: deploys, build only, PR verify.
   - **"Run build":** picks a branch and a commit, then either deploys to the first
     non-production environment or only builds. It never offers production. A module with only
     production configured gets "build only".
2. **The gate shown is a hint; the server decides.** `promotionGate` reads the module's promotion
   rule:
   - nothing deployed before → blocked;
   - not healthy → blocked;
   - soaking → minutes left;
   - otherwise ready.

   The server checks again when the promotion is asked for, and its refusal is shown as it was
   given.
3. **Run rates are over finished runs.** `/portal/dashboard` reports succeeded, failed,
   in-progress and cancelled runs. The success and failure rates are over succeeded + failed
   only. The schema is now in OpenAPI (`PortalDashboardKpis`). The Portal shows "—" until a run has
   finished.
4. **A system's health is its deployments'.** Builds no longer move it. With nothing deployed it is
   `unknown`.
5. **Demo versions say what they are.** They are still seeded with `NETCI_DEMO_DATA`, so a demo can
   walk a production request and its second approver without a CI server. Their metadata is
   `createdBy: demo seed`, `demo: true`, with no digest and no CI report, so the Portal shows their
   evidence as not reported. A re-seed adds the versions an earlier seed did not write.
6. **One design system** (`theme.css` tokens, `ui.css` components) with a light and a dark theme,
   following the system's or the person's choice:
   - 233 hard-coded colours became tokens;
   - text is no smaller than 11 px (it went down to 8 px);
   - every text token meets WCAG AA on its surfaces in both themes.

   The streamlined navigation of fca849d is kept: Release Plan, Scorecards, Stage Catalog and CI
   Cost are reached from search.

## Tests

- **Backend:**
  - a queued run is neither a success nor degraded health;
  - a cancelled run is counted as cancelled;
  - the SoD test registers the version it names instead of relying on the seed.
- **Unit (vitest, 124):**
  - the rules panel, including `cancelInProgress` and path filters;
  - `promotionGate`: blocked, not healthy, soaking (20 of 30 minutes), ready;
  - the Builds list for admission and supersession (ADR-050);
  - "Run build" never offering production.
- **Browser (Playwright against the API on its test database):** 41 passed. 6 skip, as before,
  because they need Keycloak. The specs now drive Builds → Run build, and they pick a system by
  name rather than by its place in a sorted list.
- **Full gate:**
  - `pytest backend/tests tests/contract`: 1509 + 62 passed;
  - `pyflakes` clean;
  - `migrate.py --check-schema`;
  - `npm test && npm run build`.

## What it costs, said plainly

- **The promotion path reads the module overview's environments.** An environment the module
  does not configure still shows as a lane, "not deployed".
- **"Request release" opens production requests;** it does not fill the request in.
- **Old screenshots and the defense deck show the per-environment pipeline cards.** They describe
  the Portal before this ADR.
