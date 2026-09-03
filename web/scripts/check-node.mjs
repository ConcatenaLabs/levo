// Refuse to start a build that cannot finish, and say why in one line.
//
// Vite's own engine range is ^20.19.0 || >=22.12.0. Run on anything older, the
// build does not say so: npm skips the bundler's native binding as an unmet
// engine, the JavaScript fallback then imports `styleText` from node:util,
// which arrived in Node 20.19, and the whole thing dies with
//
//   SyntaxError: The requested module 'node:util' does not provide an export
//   named 'styleText'
//
// which reads like a corrupt install rather than a Node too old, and sends
// whoever hit it looking in the wrong place. It also happens before vite writes
// anything, so the previous bundle stays where it is: a deploy that lost the
// exit status -- piping the build through `tail`, say -- goes on serving the
// old app and reports success. That is what this file is here to prevent.
//
// Read from vite's own package.json rather than repeated here, so the two
// cannot drift.

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const web = dirname(dirname(fileURLToPath(import.meta.url)))

export function ranges() {
  try {
    const pkg = JSON.parse(readFileSync(join(web, 'node_modules/vite/package.json'), 'utf8'))
    return String(pkg.engines?.node || '')
  } catch {
    return ''            // no install yet; npm will say so before this matters
  }
}

// Enough of a semver range for the one shape vite states: a list of `^x.y.z`
// and `>=x.y.z` clauses separated by ||.
export function satisfies(version, range) {
  const [maj, min, pat] = version.replace(/^v/, '').split('.').map(Number)
  const cmp = (a, b, c) => (maj - a) || (min - b) || (pat - c)
  const clauses = range.split('||').map((s) => s.trim()).filter(Boolean)
  if (!clauses.length) return true
  return clauses.some((clause) => {
    const m = clause.match(/^([\^>]=?|>=)?\s*(\d+)\.(\d+)\.(\d+)$/)
    if (!m) return true                       // a shape this cannot read is not a refusal
    const [, op, a, b, c] = m
    const at = cmp(Number(a), Number(b), Number(c))
    return op === '^' ? maj === Number(a) && at >= 0 : at >= 0
  })
}

// Only when run as the build's first step; importing this to test it must not
// take the process down.
const invoked = process.argv[1] && import.meta.url.endsWith(process.argv[1].split('/').pop())
const range = invoked ? ranges() : ''
if (range && !satisfies(process.version, range)) {
  process.stderr.write(
    'levo-web cannot be built with Node ' + process.version + '.\n' +
    'Vite needs ' + range + '. On an older Node the build fails partway with a\n' +
    "message about node:util's styleText export, which is this and not a broken\n" +
    'checkout. Put a newer Node first on PATH -- the deployed box keeps one at\n' +
    '/opt/node24/bin -- and build again.\n')
  process.exit(1)
}
