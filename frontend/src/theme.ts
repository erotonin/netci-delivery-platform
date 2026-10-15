import { useEffect, useState } from 'react'

/** The person's choice of theme: their system's, or one they picked (kept in this browser only). */
export type ThemeChoice = 'system' | 'light' | 'dark'

const KEY = 'netci-theme'

function stored(): ThemeChoice {
  try {
    const v = window.localStorage.getItem(KEY)
    return v === 'light' || v === 'dark' ? v : 'system'
  } catch {
    return 'system' // storage blocked (a private window): the system's theme
  }
}

function systemDark(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-color-scheme: dark)').matches
}

/** Sets data-theme on <html>; theme.css keys every token off it. */
export function applyTheme(choice: ThemeChoice = stored()): 'light' | 'dark' {
  const resolved = choice === 'system' ? (systemDark() ? 'dark' : 'light') : choice
  document.documentElement.dataset.theme = resolved
  return resolved
}

export function useTheme(): { choice: ThemeChoice; resolved: 'light' | 'dark'; setChoice: (c: ThemeChoice) => void } {
  const [choice, setChoiceState] = useState<ThemeChoice>(stored)
  const [resolved, setResolved] = useState<'light' | 'dark'>(() => applyTheme(choice))
  useEffect(() => {
    setResolved(applyTheme(choice))
    if (choice !== 'system' || typeof window.matchMedia !== 'function') return
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const follow = () => setResolved(applyTheme('system'))
    media.addEventListener?.('change', follow)
    return () => media.removeEventListener?.('change', follow)
  }, [choice])
  const setChoice = (c: ThemeChoice) => {
    try {
      if (c === 'system') window.localStorage.removeItem(KEY)
      else window.localStorage.setItem(KEY, c)
    } catch {
      // not kept: the choice still applies to this page
    }
    setChoiceState(c)
  }
  return { choice, resolved, setChoice }
}
