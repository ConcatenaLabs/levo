// The app loads nothing from anywhere else: its fonts, its scripts and its
// styles are served by levod, and the proxy's content-security-policy says
// 'self' for all three. A stylesheet that reached for a font on a CDN, or a
// script tag pointing off-site, would be blocked there without a word, and
// the page would fall back to a system face or lose a feature silently.
// Links a person clicks are not resources and are not what this scans.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

const root = new URL('..', import.meta.url).pathname

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(css|jsx?|html)$/.test(name)) out.push(p)
  }
  return out
}

const RESOURCE = [
  /url\(\s*['"]?https?:\/\//,                    // css url(https://...)
  /@import\s+(url\()?\s*['"]?https?:\/\//,       // css @import
  /<(script|link|img|iframe|source|video|audio)\b[^>]*\b(src|href)=["']https?:\/\//, // markup
  /\bfrom\s+['"]https?:\/\//,                    // import x from 'https://...'
  /\bimport\(\s*['"]https?:\/\//,                // import('https://...')
]

test('no source loads a font, a script, a style or an image from another origin', () => {
  const files = [join(root, 'index.html'), ...walk(join(root, 'src'))]
  const hits = []
  for (const f of files) {
    const text = readFileSync(f, 'utf8')
    for (const re of RESOURCE) {
      const m = text.match(re)
      if (m) hits.push(f.slice(root.length) + ': ' + m[0].slice(0, 60))
    }
  }
  assert.deepEqual(hits, [])
})
