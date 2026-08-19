import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dev server proxies the API to levod so the app runs the same way it
// ships: one origin, no CORS, no environment-specific base URL.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { '/api': { target: 'http://127.0.0.1:8099', changeOrigin: true } },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
