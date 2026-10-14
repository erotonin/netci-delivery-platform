# ADR-086: A CSRF crumb every replica accepts, bound to the shared sign-in

- Status: accepted; unit-tested, measured in the lab (below)
- Date: 2026-10-15
- Refines: ADR-077 (a sign-in that survives its replica)

## Context

Jenkins binds a CSRF crumb to the user and to the HTTP session. A session belongs to one JVM.
ADR-077 made the *sign-in* survive the replica, but not the crumb. So a form served by jenkins-0
and posted to jenkins-1 was refused with 403 "No valid crumb", although the person was signed in
on both. That happens in three cases:
- HAProxy retries a 503 on the other replica;
- the next click after jenkins-0 died;
- each step of a rolling restart (3 refused creates in every upgrade probe so far).

ADR-085 found it again on a store failover: a "try again" (503) became a "forbidden" (403).

The salt is not the problem: it is the `jenkins.model.Jenkins.crumbSalt` secret, already the same on
every replica. Only the session id differs.

## Decision

1. **Each shared sign-in has an id** (`sid`, 18 random bytes) inside the signed `NETCI_SESSION`
   cookie:
   - kept when the cookie is renewed, so a crumb in an open page stays valid;
   - new at the next sign-in;
   - gone at sign-out.
2. **`SharedCrumbIssuer`** binds the crumb to user id + sid, under Jenkins' own salt. A request with
   no shared sign-in gets Jenkins' own crumb, unchanged:
   - an API token (which needs no crumb);
   - a script sending its password, whose first crumb comes before any cookie.

   Either form is accepted where it is valid. The bound form carries a prefix, so it can never equal
   a Jenkins crumb for some session.
3. **A POST that is the first request on a replica is checked against the cookie's sign-in.** The
   filter that restores a sign-in from the cookie may run after Jenkins' CSRF filter. A cookie
   naming a user other than the request's own binds nothing.
4. **Installed at start-up over Jenkins' default issuer only**, when shared sign-ins are on. A
   site's own choice is kept. JCasC can name it: `crumbIssuer: netciSharedSession`.
5. **Read back from config.xml, it is made anew** (`readResolve`). XStream builds objects without
   their constructor, and `DefaultCrumbIssuer`'s own `readResolve` is private, so it is not run for a
   subclass. That left its digest null.

## Why it is as safe as Jenkins' own

A crumb defends against a page on another site making the browser post. Such a page cannot read
the crumb or compute it:
- the sid lives in an HttpOnly cookie, and the crumb salt is secret;
- the cookie is signed, so a sid cannot be chosen;
- signing in again, signing out, and "Invalidate sessions" (the user's seed) each end the sid.

## Tests (`SharedCrumbTest`)

1. **The main test.** A crumb served on one replica is accepted from a browser that has only the
   shared cookie and no session. With Jenkins' default issuer this test fails with 403, the failure
   seen in the lab.
2. A crumb is refused when:
   - it was served to alice and is posted as bob;
   - it is missing, or made up;
   - it belongs to the sign-in before a sign-out.
3. A renewed cookie keeps its sid, and the open page's crumb still works.
4. After a save and reload of the configuration, crumbs are still issued. This test fails without
   `readResolve`.
5. Only Jenkins' default issuer is replaced.

The plugin's 80 tests pass; SpotBugs reports 0 bugs.

## Lab (controller netci31)

- **The first build of this, netci30, shipped the bug in decision 5.** After its rolling start both
  replicas answered `"crumb": null`, and every form was refused. That is how it was found; netci31
  fixes it.
- **Rolling restart under load** (`aa-phase6-upgrade-20261014T193437Z.json`). Two people signed in
  through the form, each creating a job every second with the crumb their page gave them:
  - none of their 239 creates was refused;
  - person r1 held one crumb for 149 creates, answered 60 times by r0 and 89 times by r1.
- The two scripts that send a password were still refused twice (403). Their crumb came before any
  shared sign-in, so it is Jenkins' own, bound to one JVM.
- The UI answered 331 of 331 reads.

## What it costs, said plainly

- **Scripts that sign in with a password and keep a crumb** still get 403 after their replica
  restarts. The answer is Jenkins' own advice: an API token, which needs no crumb.
- **Removing the plugin** leaves config.xml naming a class that is gone. Jenkins then falls back to
  no crumb issuer configured, so set the issuer again (or through JCasC) when removing the plugin.
