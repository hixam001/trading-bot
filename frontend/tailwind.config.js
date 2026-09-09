/** @type {import('tailwindcss').Config} */
// Design tokens — the ONLY place raw values live (frontend/DESIGN.md §1).
//
// §58 PHOSPHOR AMBER world: a warm-black lacquer ground ladder (kinpaku-style),
// champagne text, and a gold brand accent reserved for structure and brand —
// semantic colors (pos/neg/warn/info) carry meaning and never double as brand.
// Every text token passes ≥4.5:1 on the surface it is used on (§1 contrast).
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // warm-black lacquer surface ladder
        ink: '#0e0c09',
        panel: '#161310',
        raised: '#1e1a15',
        line: '#29231a',
        'line-strong': '#3d3524',
        // champagne text ladder
        bright: '#f4efe4',
        body: '#d8d1c2',
        dim: '#a89d87',
        faint: '#8f8471',
        // brand — kinpaku gold (structure/identity ONLY, never meaning)
        gold: '#e0b04a',
        'gold-soft': '#a98f45',
        'gold-deep': '#6e5824',
        // semantics (meaning, never decoration)
        pos: '#59b259',
        neg: '#e05b44',
        warn: '#cf8c1e',
        info: '#5ab0a4',
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
      // §58 motion system (DESIGN.md §4): one authored moment + quiet support.
      keyframes: {
        // The prompt cursor — the only loop on the page. Steps, not ease:
        // a terminal cursor is on or off, never in between.
        blink: {
          '0%, 49%': { opacity: '1' },
          '50%, 100%': { opacity: '0' },
        },
        // THE authored moment: a fresh decision lands on the tape — the row
        // lifts gold once and settles. Gold = brand attention, not meaning.
        'row-flash': {
          '0%': { backgroundColor: 'rgba(224, 176, 74, 0.16)' },
          '100%': { backgroundColor: 'rgba(224, 176, 74, 0)' },
        },
        // Skeleton sweep: a solid light block slides across (no gradient —
        // §5 bans them outright; this is a moving surface, not a blend).
        sweep: {
          '0%': { transform: 'translateX(-120%)' },
          '100%': { transform: 'translateX(420%)' },
        },
        // Routine arrival for a state change (tab switch, panel mount):
        // 240ms, exponential ease-out, content visible from the first frame.
        'fade-rise': {
          '0%': { opacity: '0', transform: 'translateY(4px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
      },
      animation: {
        blink: 'blink 1.1s steps(1) infinite',
        'row-flash': 'row-flash 0.9s cubic-bezier(0.16, 1, 0.3, 1) 1 both',
        sweep: 'sweep 1.6s cubic-bezier(0.16, 1, 0.3, 1) infinite',
        'fade-rise': 'fade-rise 0.24s cubic-bezier(0.16, 1, 0.3, 1) 1 both',
      },
      transitionTimingFunction: {
        // Confident arrivals (impeccable animate.md) — the house ease.
        'out-expo': 'cubic-bezier(0.16, 1, 0.3, 1)',
      },
    },
  },
  plugins: [],
}
