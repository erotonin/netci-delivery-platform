import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { applyTheme } from './theme'

// Before the first paint, so a dark theme does not flash white.
applyTheme()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
