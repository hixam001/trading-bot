/** @type {import('tailwindcss').Config} */
// Design tokens — the ONLY place raw values live (frontend/DESIGN.md §1).
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // surface ladder
        ink: '#0a0e14',
        panel: '#11161f',
        raised: '#161e2c',
        line: '#1e2633',
        'line-strong': '#2c3849',
        // text ladder
        bright: '#eef2f8',
        body: '#c7d0dd',
        dim: '#8b96a5',
        faint: '#5c6773',
        // semantics
        pos: '#3fb950',
        neg: '#f85149',
        warn: '#d29922',
        info: '#58a6ff',
      },
      fontFamily: {
        mono: ['"JetBrains Mono Variable"', 'ui-monospace', 'SFMono-Regular',
               'Menlo', 'Consolas', 'monospace'],
        sans: ['"Inter Variable"', 'system-ui', '-apple-system', 'Segoe UI',
               'sans-serif'],
      },
      borderRadius: {
        DEFAULT: '6px',
      },
    },
  },
  plugins: [],
}
