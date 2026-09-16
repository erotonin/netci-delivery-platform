/**
 * Authorization Code + PKCE in the browser, against whatever identity provider netCI
 * names in `GET /auth/config`. No library: the flow is four steps and every one of them
 * is worth being able to read.
 *
 *   beginOidcLogin()     make verifier + state, remember them for this tab, redirect
 *   completeOidcLogin()  back with ?code&state: check state, exchange the code with the
 *                        verifier, hand the id_token to netCI's ordinary token login
 *
 * The verifier and state live in sessionStorage: one tab, one login attempt, gone when
 * the tab closes. The token that comes back is stored by the existing session code,
 * which already keeps it out of localStorage.
 */

import { request } from '../api/netciClient'

export type OidcBrowserConfig = {
  issuer: string
  clientId: string
  authorizationEndpoint?: string
  tokenEndpoint?: string
  endSessionEndpoint?: string
  scopes?: string[]
  pkce?: string
  error?: string
}

export type AuthConfig = {
  authMode: string
  oidc: OidcBrowserConfig | null
}

const VERIFIER_KEY = 'netci.oidc.verifier'
const STATE_KEY = 'netci.oidc.state'
const RETURN_KEY = 'netci.oidc.return_to'

export function fetchAuthConfig(): Promise<AuthConfig> {
  return request<AuthConfig>('/auth/config')
}

function randomString(bytes = 32): string {
  const buffer = new Uint8Array(bytes)
  crypto.getRandomValues(buffer)
  return base64url(buffer)
}

function base64url(bytes: ArrayBuffer | Uint8Array): string {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes)
  let binary = ''
  view.forEach((b) => { binary += String.fromCharCode(b) })
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

async function challengeFor(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))
  return base64url(digest)
}

/** The redirect URI is this page without query or hash: what the IdP was registered with. */
export function redirectUri(): string {
  return `${window.location.origin}${window.location.pathname}`
}

export async function beginOidcLogin(config: OidcBrowserConfig): Promise<void> {
  if (!config.authorizationEndpoint) throw new Error('identity provider has no authorization endpoint')
  const verifier = randomString(48)
  const state = randomString(16)
  window.sessionStorage.setItem(VERIFIER_KEY, verifier)
  window.sessionStorage.setItem(STATE_KEY, state)
  window.sessionStorage.setItem(RETURN_KEY, window.location.hash || '')
  const params = new URLSearchParams({
    response_type: 'code',
    client_id: config.clientId,
    redirect_uri: redirectUri(),
    scope: (config.scopes ?? ['openid']).join(' '),
    state,
    code_challenge: await challengeFor(verifier),
    code_challenge_method: 'S256',
  })
  window.location.assign(`${config.authorizationEndpoint}?${params.toString()}`)
}

/** True when the current URL is the IdP sending us back. */
export function isOidcCallback(): boolean {
  const query = new URLSearchParams(window.location.search)
  return query.has('code') && query.has('state')
}

/**
 * Exchange the code for tokens. Returns the id_token, which carries the audience and
 * group claims netCI verifies. Throws on a state mismatch: a code that arrives with a
 * state this tab did not issue is somebody else's login being pushed at us.
 */
export async function completeOidcLogin(config: OidcBrowserConfig): Promise<string> {
  const query = new URLSearchParams(window.location.search)
  const code = query.get('code') ?? ''
  const state = query.get('state') ?? ''
  const expectedState = window.sessionStorage.getItem(STATE_KEY)
  const verifier = window.sessionStorage.getItem(VERIFIER_KEY)
  window.sessionStorage.removeItem(STATE_KEY)
  window.sessionStorage.removeItem(VERIFIER_KEY)
  if (!expectedState || !verifier || state !== expectedState) {
    throw new Error('login state mismatch: start the sign-in again from this tab')
  }
  if (!config.tokenEndpoint) throw new Error('identity provider has no token endpoint')
  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    client_id: config.clientId,
    code,
    redirect_uri: redirectUri(),
    code_verifier: verifier,
  })
  const response = await fetch(config.tokenEndpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: body.toString(),
  })
  const payload = (await response.json().catch(() => ({}))) as { id_token?: string; error?: string; error_description?: string }
  if (!response.ok || !payload.id_token) {
    throw new Error(payload.error_description || payload.error || `token endpoint answered ${response.status}`)
  }
  // Leave the address bar as it was before the redirect: no code, no state.
  const returnTo = window.sessionStorage.getItem(RETURN_KEY) ?? ''
  window.sessionStorage.removeItem(RETURN_KEY)
  window.history.replaceState(null, '', `${redirectUri()}${returnTo}`)
  return payload.id_token
}
