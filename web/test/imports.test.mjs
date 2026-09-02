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
