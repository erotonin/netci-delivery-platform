import { test, type APIRequestContext } from '@playwright/test'

/**
 * Preconditions some journeys need and CI does not have, asked of the real API -- never assumed.
 *
 * The demo modules point at repositories that do not exist and no pipeline has run, so the git
 * picker cannot offer a commit and there is no built artifact to redeploy. On the lab (its git
 * server, Jenkins, the registry) these tests run. Each skip says what was missing, in the API's own
 * words, the way oidc-login.spec.ts skips without an identity provider.
 */
const API = process.env.NETCI_API_URL ?? 'http://127.0.0.1:8100'

export async function skipUnlessRepositoryReachable(request: APIRequestContext, moduleId: string) {
  const response = await request.get(`${API}/modules/${encodeURIComponent(moduleId)}/git-refs`)
  const body = response.ok() ? await response.json() : { error: `git-refs answered ${response.status()}`, branches: [] }
  const reason = String(body.error ?? 'the repository has no branches').split('\n')[0]
  test.skip(Boolean(body.error) || body.branches.length === 0, `needs ${moduleId}'s repository: ${reason}`)
}

export async function hasSucceededRun(request: APIRequestContext, moduleId: string): Promise<boolean> {
  const response = await request.get(`${API}/modules/${encodeURIComponent(moduleId)}/overview`)
  if (!response.ok()) return false
  const runs: Array<{ status?: string }> = (await response.json()).recentRuns ?? []
  return runs.some((run) => String(run.status).toLowerCase() === 'succeeded')
}

export async function skipUnlessSucceededRun(request: APIRequestContext, moduleId: string) {
  test.skip(!(await hasSucceededRun(request, moduleId)), `needs a succeeded pipeline run of ${moduleId} (Jenkins and the registry): there is no artifact to redeploy`)
}

/**
 * Whether the server keeps requester and approver apart. It does not with authentication off:
 * every caller is the same anonymous subject and may approve (separation_of_duties_enabled in
 * main.py). A test asserts the posture the server reports, so "pat cannot approve" is checked
 * where it is true -- an OIDC deployment -- instead of failing, or passing, where it is not.
 */
export async function separationOfDuties(request: APIRequestContext): Promise<boolean> {
  const response = await request.get(`${API}/me`)
  return response.ok() && (await response.json()).separationOfDuties === true
}

/** The ids of the still-active change freezes with this name. Read-only: what a test asserts on. */
export async function activeFreezesNamed(request: APIRequestContext, name: string): Promise<string[]> {
  const response = await request.get(`${API}/change-freezes`)
  if (!response.ok()) throw new Error(`GET /change-freezes answered ${response.status()}`)
  const body = await response.json()
  const items: Array<{ id: string; name: string; cancelledAt: string | null }> = Array.isArray(body) ? body : body.items ?? []
  return items.filter((f) => f.name === name && !f.cancelledAt).map((f) => f.id)
}

/**
 * Cancel, through the API, every still-active change freeze with this name. For a test's
 * `finally` only, never for an assertion: a freeze it created and failed to cancel blocks every
 * later production request in the run -- the API refuses them, rightly -- so one failure would
 * masquerade as several.
 */
export async function cancelFreezesNamed(request: APIRequestContext, name: string): Promise<void> {
  for (const id of await activeFreezesNamed(request, name)) {
    await request.post(`${API}/change-freezes/${encodeURIComponent(id)}/cancel`)
  }
}
