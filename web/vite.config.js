import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// LEVO_BASE lets one codebase serve from the site root during development and
// from a sub-path in production (Caddy strips /levo before levod sees it, so
// only the browser-facing URLs need the prefix).
const base = process.env.LEVO_BASE || '/'

export default defineConfig({
  base,
  plugins: [react()],
  server: {
    proxy: { '/api': { target: 'http://127.0.0.1:8099', changeOrigin: true } },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
