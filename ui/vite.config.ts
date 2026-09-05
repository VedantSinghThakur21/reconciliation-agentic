import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Live demo API: scripts/run_api.py defaults to 8003 (override with RECONQ_API_PORT).
      '/api': 'http://127.0.0.1:8003',
    },
  },
})
