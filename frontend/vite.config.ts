import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Proxies API + WebSocket to the FastAPI backend on :8000.
export default defineConfig({
  plugins: [react()],
  build: {
    sourcemap: false, // Ensure source maps are never generated or served publicly in production
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
})
