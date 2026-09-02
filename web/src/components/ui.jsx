import { useEffect, useState } from 'react'

// Small pieces every page uses: a copy button, an external link, a notice
// that screen readers announce, a hex value that wraps, and the page title.

export function Copy({ value, label = 'Copy' }) {
  const [did, setDid] = useState(false)
  async function go() {
    try {
      await navigator.clipboard.writeText(String(value))
      setDid(true)
      setTimeout(() => setDid(false), 1500)
    } catch {}
  }
  return (
    <button type="button" className={'copy' + (did ? ' did' : '')} onClick={go}
            aria-label={label + (did ? ', copied' : '')}>
      {did ? 'Copied' : 'Copy'}
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
export function Hex({ value, href, copy = true, short }) {
  if (!value) return <span className="dim">—</span>
  const shown = short ? String(value).slice(0, short) + '…' : value
  return (
    <span className="hex">
      <Ext href={href}>{shown}</Ext>
      {copy && <Copy value={value} />}
    </span>
  )
}

export function usePageTitle(title) {
  useEffect(() => {
    document.title = title ? title + ' · Levo' : 'Levo'
    return () => { document.title = 'Levo' }
  }, [title])
}
