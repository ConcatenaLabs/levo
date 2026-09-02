import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// LEVO_BASE lets one codebase serve from the site root during development and
// from a sub-path in production (Caddy strips /levo before levod sees it, so
// only the browser-facing URLs need the prefix).
const base = process.env.LEVO_BASE || '/'

// The share-preview image is named in a meta tag, and Vite rewrites asset URLs
// only in the attributes it knows. `%BASE%` is replaced here so the tag points
// at the file wherever the site is served from; LEVO_SITE_ORIGIN makes it
// absolute, which is what a link preview in a chat client needs.
const origin = (process.env.LEVO_SITE_ORIGIN || '').replace(/\/+$/, '')

function htmlBase() {
  return {
    name: 'levo-html-base',
    transformIndexHtml(html) {
      return html.split('%BASE%').join(origin + base)
    },
  }
}

export default defineConfig({
  base,
  plugins: [react(), htmlBase()],
  server: {
    proxy: { '/api': { target: 'http://127.0.0.1:8099', changeOrigin: true } },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
