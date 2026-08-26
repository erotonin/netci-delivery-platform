import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { CheckCircle2, CircleAlert, Info, X } from 'lucide-react'

type FeedbackKind = 'success' | 'error' | 'info'
type FeedbackValue = { notify: (message: string, kind?: FeedbackKind) => void }

const FeedbackContext = createContext<FeedbackValue | null>(null)

export function PortalFeedbackProvider({ children }: { children: ReactNode }) {
  const [feedback, setFeedback] = useState<{ message: string; kind: FeedbackKind } | null>(null)

  useEffect(() => {
    if (!feedback) return
    const timer = window.setTimeout(() => setFeedback(null), 4200)
    return () => window.clearTimeout(timer)
  }, [feedback])

  const value = useMemo<FeedbackValue>(() => ({
    notify: (message, kind = 'success') => setFeedback({ message, kind }),
  }), [])
  const Icon = feedback?.kind === 'success' ? CheckCircle2 : feedback?.kind === 'error' ? CircleAlert : Info

  return <FeedbackContext.Provider value={value}>
    {children}
    {feedback && <div className={`toast toast-${feedback.kind}`} role={feedback.kind === 'error' ? 'alert' : 'status'} aria-live="polite"><Icon size={17} />{feedback.message}<button aria-label="Đóng thông báo" onClick={() => setFeedback(null)}><X size={15} /></button></div>}
  </FeedbackContext.Provider>
}

export function usePortalFeedback(): FeedbackValue {
  const value = useContext(FeedbackContext)
  if (!value) throw new Error('usePortalFeedback must be used inside PortalFeedbackProvider')
  return value
}
