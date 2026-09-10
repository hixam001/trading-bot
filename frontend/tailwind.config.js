/** @type {import('tailwindcss').Config} */
// Design tokens — the ONLY place raw values live (frontend/DESIGN.md §1).
//
// §59 SIGNAL world (the operator's demo_2_signal.html, evolved + ported):
// an ultra-black blue-tinted ground with a stepped surface ladder, a cyan
// signal accent reserved for STRUCTURE (brand, focus, tabs, live chrome),
// and pass/fail semantics that carry meaning and never double as brand.
// Every text token passes ≥4.5:1 on the surfaces it is used on (§1 contrast).
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // stepped blue-black surface ladder
        base: '#080b0e',
        surface: '#0f1419',
        raised: '#141b22',
        'surface-3': '#182129',
        line: '#223040',
        'line-soft': '#182029',
        'line-strong': '#2d3f52',
        // text ladder
        bright: '#e4ecf2',
        body: '#aebbc7',
        dim: '#8595a8',
        faint: '#73869c',
        // signal accent — structure/identity ONLY, never meaning
        live: '#4ab8ff',
        'live-soft': 'rgba(74, 184, 255, 0.10)',
        // verdict neutral — a [SKIP] decision chip, not a failure
        reject: '#b8c4d1',
        // semantics (meaning, never decoration)
        pass: '#3ee089',
        fail: '#f2695c',
        warn: '#d8a03a',
      },
      fontFamily: {
        mono: ['"JetBrains Mono Variable"', 'ui-monospace', 'SFMono-Regular',
               'Menlo', 'Consolas', 'monospace'],
        sans: ['"Inter Variable"', 'system-ui', '-apple-system', 'Segoe UI',
               'sans-serif'],
      },
      borderRadius: {
        DEFAULT: '4px',
      },
      // §59 motion system (DESIGN.md §4): one authored moment + quiet support.
      keyframes: {
        // The clock cursor — the only loop on the page. Steps, not ease.
        blink: {
          '0%, 49%': { opacity: '1' },
          '50%, 100%': { opacity: '0' },
        },
        // THE authored moment: a fresh decision lands on the tape — the row
        // lifts signal-cyan once and settles. Cyan = brand attention, not
        // meaning (the verdict word carries meaning).
        'row-flash': {
          '0%': { backgroundColor: 'rgba(74, 184, 255, 0.12)' },
          '100%': { backgroundColor: 'rgba(74, 184, 255, 0)' },
        },
        // Skeleton sweep: a solid light block slides across (no gradient —
        // §5 bans them outright; this is a moving surface, not a blend).
        sweep: {
          '0%': { transform: 'translateX(-120%)' },
          '100%': { transform: 'translateX(420%)' },
        },
        // Routine arrival for a state change (tab switch, panel mount).
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