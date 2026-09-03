import { useEffect, useState } from 'react'

// Small pieces every page uses: a copy button, an external link, a notice
// that screen readers announce, a hex value that wraps, and the page title.

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

export function usePageTitle(title) {
  useEffect(() => {
    document.title = title ? title + ' · Levo' : 'Levo'
    return () => { document.title = 'Levo' }
  }, [title])
}
