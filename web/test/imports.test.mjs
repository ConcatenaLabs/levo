// Every name a module imports has to be used in it.
//
// An unused import is not a style question here: it is the trace of a piece of
// interface that was removed and half-forgotten, and the first pass left three
// of them. There is no linter in this project on purpose -- the gate is the
// tests -- so the check lives with the tests.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

function sources(dir) {
  const out = []
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) out.push(...sources(path))
    else if (/\.(jsx?|mjs)$/.test(name)) out.push(path)
  }
  return out
}

// The named and default bindings an import statement introduces.
function importedNames(source) {
  const names = []
  const re = /import\s+([^'"]+?)\s+from\s+['"][^'"]+['"]/g
  let m
  while ((m = re.exec(source))) {
    const clause = m[1]
    const braces = clause.match(/\{([^}]*)\}/)
    if (braces) {
      for (const part of braces[1].split(',')) {
        const name = part.trim().split(/\s+as\s+/).pop().trim()
        if (name) names.push({ name, at: m.index })
      }
    }
    const plain = clause.replace(/\{[^}]*\}/, '').replace(/,/g, ' ').trim()
    for (const name of plain.split(/\s+/)) {
      if (name && name !== '*' && name !== 'as') names.push({ name, at: m.index })
    }
  }
  return names
}

test('no source file imports a name it does not use', () => {
  const unused = []
  for (const path of sources('src')) {
    const source = readFileSync(path, 'utf8')
    const body = source.replace(/import\s+[^'"]+?\s+from\s+['"][^'"]+['"]/g, '')
    for (const { name } of importedNames(source)) {
      const used = new RegExp('\\b' + name.replace(/[$]/g, '\\$') + '\\b').test(body)
      if (!used) unused.push(path + ': ' + name)
    }
  }
  assert.deepEqual(unused, [], 'unused imports')
})

// ...and every name a module USES has to come from somewhere.
//
// The other half, and the one that shipped a white screen: `timeLabel` was
// called in a panel that never imported it, so the whole page unmounted the
// moment a project opened its own ledger. A bundler does not catch this -- a
// free identifier is a runtime lookup, valid JavaScript until it runs -- and
// the render suite could not, because the panel needs an issuer's session.
test('no source file calls a helper it never imported', () => {
  const helpers = {}
  for (const path of sources('src')) {
    if (!/\/lib\//.test(path)) continue
    const source = readFileSync(path, 'utf8')
    for (const m of source.matchAll(/export\s+(?:async\s+)?function\s+(\w+)/g)) {
      helpers[m[1]] = path
    }
    for (const m of source.matchAll(/export\s+(?:const|let)\s+(\w+)/g)) {
      helpers[m[1]] = path
    }
  }
  const free = []
  for (const path of sources('src')) {
    const source = readFileSync(path, 'utf8')
    const imported = new Set(importedNames(source).map((i) => i.name))
    // Method definitions (`compact() {`) read like calls to a regex, and a
    // class of its own may name a method whatever it likes.
    const body = source.replace(/import\s+[^'"]+?\s+from\s+['"][^'"]+['"]/g, '')
                       .replace(/^\s*\w+\s*\([^)]*\)\s*\{/gm, '')
    for (const [name, from] of Object.entries(helpers)) {
      if (path === from || imported.has(name)) continue
      // Declared locally under the same name is fine; called without either is not.
      if (new RegExp('(?:function|const|let|var)\\s+' + name + '\\b').test(body)) continue
      if (new RegExp('(?<![.\\w])' + name + '\\s*\\(').test(body)) {
        free.push(path + ': ' + name + ' (exported by ' + from + ')')
      }
    }
  }
  assert.deepEqual(free, [], 'helpers used without an import')
})
