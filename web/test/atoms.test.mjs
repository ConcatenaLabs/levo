// An atom count is a decimal string, and "0" is truthy.
//
// Every amount on this site arrives from levod as a decimal string, because a
// JavaScript number cannot hold an atom count above 2**53. That makes the two
// most natural ways to ask "is there any?" both wrong: `atoms ? a : b` takes
// the a-branch on "0", and `atoms > 0` only works by a coercion that stops
// being exact at the sizes the strings exist for. Both mistakes are invisible
// in review and produced a tier that cannot buy describing itself as buying
// "up to 0". So the rule is that a decision about an amount goes through
// big() or positive(), and this test is what makes the rule hold.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

import { positive, big } from '../src/lib/format.js'

function sources(dir) {
  const out = []
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) out.push(...sources(path))
    else if (/\.(jsx?|mjs)$/.test(name)) out.push(path)
  }
  return out
}

// A field holding an atom count: the wire spells every one of them `*_atoms`.
const FIELD = '[A-Za-z_$][\\w$]*(?:\\.[\\w$]+)*_atoms'

// Reading one for display is fine -- amount(), compact() and plain() all take
// the string, and a field after `?` is the value a ternary yields rather than
// a test of it. These are the shapes that DECIDE something on the field.
const WRONG = [
  ['tested for truthiness',
   new RegExp('(?:&&|\\|\\||!)\\s*' + FIELD + '\\s*(?:\\)|&&|\\|\\||\\?|$)'
              + '|\\b' + FIELD + '\\s*(?:\\?[^?]|&&|\\|\\|)', 'g')],
  ['compared as a number', new RegExp('\\b' + FIELD + '\\s*(?:>|<|>=|<=|===|!==|==|!=)\\s*-?\\d', 'g')],
  ['coerced with Number()', new RegExp('Number\\(\\s*' + FIELD, 'g')],
]

test('no decision is made on a raw atom field', () => {
  const bad = []
  for (const path of sources('src')) {
    if (path.endsWith(join('lib', 'format.js'))) continue   // where big() lives
    const source = readFileSync(path, 'utf8')
    source.split('\n').forEach((line, i) => {
      // A line that already routes through big()/positive() is the fix, and
      // geometry may hold a Number as long as a comment says it is geometry.
      if (/\b(?:big|positive)\s*\(/.test(line)) return
      if (/\/\/\s*geometry\b/.test(line)) return
      for (const [why, re] of WRONG) {
        re.lastIndex = 0
        if (re.test(line)) bad.push(`${path}:${i + 1} ${why}: ${line.trim()}`)
      }
    })
  }
  assert.deepEqual(bad, [], 'atom fields decided on raw:\n' + bad.join('\n'))
})

test('positive() reads the string a zero arrives as', () => {
  assert.equal(positive('0'), false)
  assert.equal(positive(0), false)
  assert.equal(positive(0n), false)
  assert.equal(positive(''), false)
  assert.equal(positive(null), false)
  assert.equal(positive(undefined), false)
  assert.equal(positive('1'), true)
  assert.equal(positive('-1'), false)
  // The size the strings exist for: a count a double cannot tell from its
  // neighbour is still exactly itself here.
  assert.equal(positive('9007199254740993'), true)
  assert.equal(big('9007199254740993') + 1n, 9007199254740994n)
  assert.equal(positive('not a number'), false)
})
