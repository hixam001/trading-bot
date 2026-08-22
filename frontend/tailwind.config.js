/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        term: {
          bg: '#0a0e14',
          panel: '#11161f',
          border: '#1e2633',
          text: '#c7d0dd',
          dim: '#6b7686',
          green: '#3fb950',
          red: '#f85149',
          amber: '#d29922',
          blue: '#58a6ff',
        },
      },
    },
  },
  plugins: [],
}
