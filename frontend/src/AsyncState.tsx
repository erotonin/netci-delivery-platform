import { Component, useCallback, useEffect, useState, type ReactNode } from 'react'
import { CircleAlert, Inbox, RefreshCw } from 'lucide-react'
import { NetciApiError } from './api/netciClient'

/**
 * One way to load data, so every page fails the same way.
 *
 * Before this, each page hand-rolled its own fetch: some tracked an error flag, some did
 * not, none showed that a request was in flight, and none offered a retry. The result was
 * a Portal that, when the API was slow, looked like a Portal with no data — which is the
 * one reading a delivery platform must never invite.
 */

export type AsyncStatus = 'loading' | 'ready' | 'error'

export type AsyncData<T> = {
  status: AsyncStatus
  data: T | null
  error: Error | null
  reload: () => void
}

export function useAsyncData<T>(load: () => Promise<T>, dependencies: unknown[] = []): AsyncData<T> {
  const [state, setState] = useState<{ status: AsyncStatus; data: T | null; error: Error | null }>({
    status: 'loading',
    data: null,
    error: null,
  })
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    // `active` guards against a response arriving after the component unmounted or the
    // inputs changed: without it a slow first request can overwrite a fast second one.
    let active = true
    setState((current) => ({ ...current, status: 'loading' }))
    load()
      .then((data) => { if (active) setState({ status: 'ready', data, error: null }) })
      .catch((cause) => {
        if (!active) return
        setState({ status: 'error', data: null, error: cause instanceof Error ? cause : new Error(String(cause)) })
      })
    return () => { active = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attempt, ...dependencies])

  return { ...state, reload: useCallback(() => setAttempt((value) => value + 1), []) }
}

/** What went wrong, in words the reader can act on rather than a status code. */
export function describeError(error: Error | null): string {
  if (error instanceof NetciApiError) {
    if (error.status === 403) return 'You do not have permission to view this data.'
    if (error.status === 404) return 'Data not found.'
    if (error.status === 429) return 'Too many requests. Please wait a moment and try again.'
    if (error.status >= 500) return 'netCI API is experiencing issues. Displayed data may be outdated.'
  }
  return 'Cannot connect to netCI API.'
}

export function Skeleton({ rows = 3, label = 'Loading' }: { rows?: number; label?: string }) {
  return <div className="skeleton" role="status" aria-live="polite" aria-label={label}>
    {Array.from({ length: rows }, (_, index) => <i key={index} style={{ width: `${100 - index * 12}%` }} />)}
  </div>
}

export function EmptyState({ title, hint, icon }: { title: string; hint?: string; icon?: ReactNode }) {
  return <div className="empty-state">
    {icon ?? <Inbox size={22} />}
    <strong>{title}</strong>
    {hint && <span>{hint}</span>}
  </div>
}

export function LoadFailure({ error, onRetry }: { error: Error | null; onRetry: () => void }) {
  return <div className="load-failure" role="alert">
    <CircleAlert size={20} />
    <div>
      <strong>{describeError(error)}</strong>
      {/* The underlying message, in case someone is reading this over a shoulder in an
          incident. Truncated, because a stack trace in a panel helps nobody. */}
      {error?.message && <span>{error.message.slice(0, 160)}</span>}
    </div>
    <button className="secondary-button" onClick={onRetry}><RefreshCw size={15} />Retry</button>
  </div>
}

/**
 * Render one of loading / error+retry / empty / content, so a page cannot accidentally
 * present "still loading" and "there is nothing" as the same screen.
 */
export function AsyncPanel<T>({
  state,
  children,
  skeletonRows,
  isEmpty,
  empty,
}: {
  state: AsyncData<T>
  children: (data: T) => ReactNode
  skeletonRows?: number
  isEmpty?: (data: T) => boolean
  empty?: ReactNode
}) {
  if (state.status === 'loading' && state.data === null) return <Skeleton rows={skeletonRows} />
  if (state.status === 'error' && state.data === null) return <LoadFailure error={state.error} onRetry={state.reload} />
  if (state.data === null) return null
  if (isEmpty?.(state.data) && empty) return <>{empty}</>
  return <>
    {/* A reload that failed still shows the last good data, with a warning: stale data
        that says it is stale beats an empty screen that says nothing. */}
    {state.status === 'error' && <div className="sync-note is-warning" role="status">
      <CircleAlert size={15} />{describeError(state.error)} Displaying the most recently loaded data.
      <button className="link-button" onClick={state.reload}>Retry</button>
    </div>}
    {children(state.data)}
  </>
}

/**
 * Stop one broken component from blanking the whole Portal.
 *
 * Without this, a render error in any panel unmounts the entire React tree and the user
 * gets a white page with no way forward — including no way to log out or navigate away.
 */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error) {
    // Left as console output on purpose: netCI has no browser telemetry sink, and
    // inventing one here would be a dependency nobody asked for.
    console.error('Portal render error', error)
  }

  render() {
    if (!this.state.error) return this.props.children
    return <div className="portal-crash" role="alert">
      <CircleAlert size={28} />
      <h1>Application Error</h1>
      <p>A component of the Portal failed to render. Data on netCI remains unaffected.</p>
      <code>{this.state.error.message.slice(0, 300)}</code>
      <button className="primary-button" onClick={() => { this.setState({ error: null }); window.location.reload() }}>
        <RefreshCw size={16} />Reload Portal
      </button>
    </div>
  }
}
