import { useEffect, useState } from 'react'

// Small pieces every page uses: a copy button, an external link, a notice
// that screen readers announce, a hex value that wraps, the page title, and
// a read-it-again for pages left open.

export function Copy({ value, label = 'Copy' }) {
  // Three states, because the clipboard can refuse: an insecure origin, a
  // browser that asks, a permission the reader denied. Swallowing that left
  // the button looking broken, on a page whose whole point is values you have
  // to take somewhere else.
  const [state, setState] = useState('')
  async function go() {
    try {
      await navigator.clipboard.writeText(String(value))
      setState('did')
    } catch {
      setState('failed')
    }
    setTimeout(() => setState(''), 2500)
  }
  return (
    <button type="button" className={'copy' + (state ? ' ' + state : '')} onClick={go}
            aria-label={label + (state === 'did' ? ', copied'
              : state === 'failed' ? ', could not copy: select it and copy by hand' : '')}
            title={state === 'failed' ? 'Your browser would not let the page copy this. '
              + 'Select it and copy it by hand.' : undefined}>
      {state === 'did' ? 'Copied' : state === 'failed' ? 'Copy failed' : 'Copy'}
    </button>
  )
}

export function Ext({ href, children }) {
  if (!href) return <>{children}</>
  return <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
}

export function Notice({ kind = '', children, style }) {
  return (
    <div className={'notice' + (kind ? ' ' + kind : '')} role={kind === 'bad' ? 'alert' : undefined}
         style={style}>
      {children}
    </div>
  )
}

// A hex value: monospace, wraps anywhere, copies with one click, links out to
// the explorer when there is one.
export function Hex({ value, href, copy = true, short, label }) {
  if (!value) return <span className="dim">—</span>
  const shown = short ? String(value).slice(0, short) + '…' : value
  return (
    <span className="hex">
      <Ext href={href}>{shown}</Ext>
      {/* Every one of these on a page otherwise reads as the same button to a
          screen reader's button list: "Copy", seven times. */}
      {copy && <Copy value={value} label={label ? 'Copy the ' + label : undefined} />}
    </span>
  )
}

// Read something again, every so often and whenever the tab comes back into
// view. A page left open has to stay true: a sale that closed while its page
// was on screen went on offering the buy panel, and a purchase the watcher
// recorded never reached an account page that was already open. A read that
// fails changes nothing shown; the page keeps what it has.
export function useReread(fn, ms, deps = []) {
  useEffect(() => {
    if (!fn) return undefined
    const again = () => { try { const r = fn(); if (r && r.catch) r.catch(() => {}) } catch { /* keep what is shown */ } }
    const every = setInterval(again, ms)
    const onVisible = () => { if (document.visibilityState === 'visible') again() }
    document.addEventListener('visibilitychange', onVisible)
    return () => { clearInterval(every); document.removeEventListener('visibilitychange', onVisible) }
    // Whether there is a reader yet is part of the key: a page that hands
    // null until its first read lands has to start the clock once it has.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, Boolean(fn), ...deps])
}

export function usePageTitle(title) {
  useEffect(() => {
    document.title = title ? title + ' · Levo' : 'Levo'
    return () => { document.title = 'Levo' }
  }, [title])
}
