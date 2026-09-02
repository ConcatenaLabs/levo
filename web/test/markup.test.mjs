// Checks on the markup that a build cannot make for itself.
//
// Every one of these is a defect an audit found in shipped code: a label
// pointing at a block of text, a hint no screen reader reads, a button that
// disables itself under the reader's cursor.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

function sources(dir) {
  const out = []
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) out.push(...sources(path))
    else if (/\.jsx$/.test(name)) out.push(path)
  }
  return out
}

const files = sources('src').map((path) => ({ path, text: readFileSync(path, 'utf8') }))

test('every htmlFor names an element that exists in the same file', () => {
  const dangling = []
  for (const { path, text } of files) {
    for (const m of text.matchAll(/htmlFor="([^"]+)"/g)) {
      if (!new RegExp('id="' + m[1] + '"').test(text)) dangling.push(path + ': ' + m[1])
    }
  }
  assert.deepEqual(dangling, [], 'labels pointing at nothing')
})

test('every aria-describedby names an element that exists in the same file', () => {
  const dangling = []
  for (const { path, text } of files) {
    for (const m of text.matchAll(/aria-describedby="([^"]+)"/g)) {
      for (const id of m[1].split(/\s+/)) {
        if (!new RegExp('id="' + id + '"').test(text)) dangling.push(path + ': ' + id)
      }
    }
  }
  assert.deepEqual(dangling, [], 'descriptions pointing at nothing')
})

test('no action button disables itself while it is working', () => {
  // `disabled` removes the element from the tab order, so a reader who
  // activated it with the keyboard is dropped at the top of the document.
  // aria-disabled says the same thing and keeps the focus.
  const offenders = []
  for (const { path, text } of files) {
    for (const m of text.matchAll(/(^|[^-\w])disabled=\{[^}]*busy[^}]*\}/g)) {
      offenders.push(path + ': ' + m[0].trim())
    }
  }
  assert.deepEqual(offenders, [], 'buttons that vanish under the keyboard')
})

test('no Copy button is nested inside a label', () => {
  // A button inside a label joins the control's accessible name, so a textarea
  // announces as "Unsigned transaction Copy, read only".
  const offenders = []
  for (const { path, text } of files) {
    for (const m of text.matchAll(/<label[^>]*>(?:(?!<\/label>)[\s\S])*?<Copy/g)) {
      offenders.push(path)
    }
  }
  assert.deepEqual([...new Set(offenders)], [], 'copy buttons inside labels')
})
