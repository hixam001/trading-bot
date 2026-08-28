import React from 'react'
import ReactDOM from 'react-dom/client'
// Self-hosted variable fonts (no CDN, no layout shift) — DESIGN.md §1.
import '@fontsource-variable/inter'
import '@fontsource-variable/jetbrains-mono'
import App from './App'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
