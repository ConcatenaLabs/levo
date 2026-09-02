import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { useStore } from '../lib/store'
import { Notice, usePageTitle } from '../components/ui'
import { closeIn, closeLabel, compact, priceLabel } from '../lib/format'

export const STATUS_LABEL = {
  live: 'open', partial: 'open', sold_out: 'sold out', draft: 'not funded',
  closed: 'closed', ghost: 'not funded', reclaimed: 'reclaimed',
}

export function Status({ sale }) {
  if (!sale) return <span className="pill pill-draft">no sale</span>
  const s = sale.status
  return <span className={'pill pill-' + s}><i className="dot" />{STATUS_LABEL[s] || s}</span>
}

function Progress({ sale, decimals }) {
  if (!sale || !sale.terms || !sale.terms.total_atoms) return null
  const total = Number(sale.terms.total_atoms)
  const sold = Number(sale.sold_atoms || 0)
  const pct = total ? Math.min(100, (sold / total) * 100) : 0
  return (
    <div>
      <div className="meter" role="img" aria-label={Math.round(pct) + '% sold'}><i style={{ width: pct + '%' }} /></div>
      <div className="small dim num" style={{ marginTop: '.35rem' }}>
        {compact(sold, decimals)} of {compact(total, decimals)} sold
      </div>
    </div>
  )
}

function closeText(sale, height) {
  const s = sale.status
  if (s === 'sold_out' || s === 'reclaimed') return null
  const when = closeLabel(sale.terms.close_locktime)
  if (s === 'closed') return 'closed ' + when
  const left = closeIn(sale.terms.close_locktime, height)
  return 'closes ' + when + (left && left !== 'closed' ? ', ' + left : '')
}

const PAGE = 24
const FILTERS = [
  ['open', 'Open'],
  ['finished', 'Finished'],
  ['draft', 'Not funded'],
  ['all', 'All'],
]

export default function Projects() {
  usePageTitle('Sales')
  const { payment, chainHeight } = useStore()
  const [projects, setProjects] = useState(null)
  const [total, setTotal] = useState(0)
  const [error, setError] = useState(null)
  const [status, setStatus] = useState('open')
  const [sort, setSort] = useState('new')
  const [q, setQ] = useState('')
  const [typed, setTyped] = useState('')
  const [page, setPage] = useState(0)
  const height = chainHeight

  const load = () => {
    setError(null)
    api.projects({ status, sort, q, limit: PAGE, offset: page * PAGE })
      .then((r) => { setProjects(r.projects); setTotal(r.total) })
      .catch((e) => setError(e.message))
  }
  useEffect(() => { load() }, [status, sort, q, page])
  // Typing filters as you stop typing, rather than on every keystroke.
  useEffect(() => {
    const t = setTimeout(() => { setQ(typed.trim()); setPage(0) }, 250)
    return () => clearTimeout(t)
  }, [typed])

  return (
    <div className="wrap section">
      <div className="section-head">
        <h1 className="h2">Sales</h1>
        <p>
          Every sale's page shows the address its tokens are locked at and the
          terms that address was derived from. Rebuild it before you buy, and
          you do not have to take Levo's word for anything.
        </p>
      </div>

      <div className="board-filters">
        <div className="chips" role="group" aria-label="Which sales">
          {FILTERS.map(([id, label]) => (
            <button key={id} type="button"
                    className={'chip' + (status === id ? ' on' : '')}
                    aria-pressed={status === id}
                    onClick={() => { setStatus(id); setPage(0) }}>{label}</button>
          ))}
        </div>
        <div className="board-controls">
          <label className="visually-hidden" htmlFor="q">Search sales</label>
          <input id="q" type="search" value={typed} placeholder="Search by name or ticker"
                 onChange={(e) => setTyped(e.target.value)} />
          <label className="visually-hidden" htmlFor="sort">Order</label>
          <select id="sort" value={sort} onChange={(e) => { setSort(e.target.value); setPage(0) }}>
            <option value="new">Newest first</option>
            <option value="closing">Closing soonest</option>
            <option value="progress">Most sold</option>
          </select>
        </div>
      </div>

      {error && (
        <Notice kind="bad">
          {error} <button className="btn btn-sm btn-ghost" style={{ marginLeft: '.75rem' }} onClick={load}>Try again</button>
        </Notice>
      )}
      <p aria-live="polite" className="visually-hidden">
        {!projects ? 'Loading the sales.'
          : total === 0 ? 'No sales match.'
            : total + (total === 1 ? ' sale' : ' sales') +
              (q ? ' matching ' + q : '') + ', showing ' + projects.length + '.'}
      </p>
      {!projects && !error && <p className="dim">Loading the sales…</p>}

      {projects && projects.length === 0 && (q || status !== 'open') && (
        <div className="card">
          <h2 className="section-h" style={{ marginTop: 0 }}>Nothing here</h2>
          <p className="small dim" style={{ marginBottom: '1rem' }}>
            {q ? <>No sale matches “{q}”.</> : <>No sale is in that state right now.</>}
          </p>
          <button className="btn btn-sm btn-ghost"
                  onClick={() => { setTyped(''); setQ(''); setStatus('all'); setPage(0) }}>
            Show every listing
          </button>
        </div>
      )}

      {projects && projects.length === 0 && !q && status === 'open' && (
        <div className="card">
          <h2 className="section-h" style={{ marginTop: 0 }}>No sale is open</h2>
          <p className="small dim" style={{ marginBottom: '1rem' }}>
            Nothing is taking buyers at the moment. Listing needs a tier that may
            list, and a project has to lock its tokens before its sale opens.
          </p>
          <div className="btn-row">
            <Link className="btn btn-sm" to="/launch">Launch a project</Link>
            <button className="btn btn-sm btn-ghost" onClick={() => { setStatus('all'); setPage(0) }}>
              See every listing
            </button>
          </div>
        </div>
      )}

      {projects && projects.length > 0 && (
        <div className="rows">
          {projects.map((p) => {
            const d = p.decimals ?? 8
            const price = p.sale ? priceLabel(p.sale.terms, d) + ' ' + payment.label : null
            return (
              <Link className="row" key={p.slug} to={'/p/' + p.slug}>
                <div>
                  <div className="row-name">
                    {p.name}<span className="row-ticker">{p.ticker}</span>
                  </div>
                  <div className="row-sum">{p.summary}</div>
                  {price && <div className="row-price-inline num small">{price} per {p.ticker}</div>}
                </div>
                <div className="row-hide num small">
                  {price ? (
                    <>
                      {price}
                      <div className="dim" style={{ fontSize: '.78rem' }}>per {p.ticker}</div>
                    </>
                  ) : <span className="dim">—</span>}
                </div>
                <div className="row-hide">
                  <Progress sale={p.sale} decimals={d} />
                </div>
                <div className="row-status">
                  <Status sale={p.sale} />
                  {p.sale && closeText(p.sale, height) && (
                    <div className="small dim" style={{ marginTop: '.4rem' }}>
                      {closeText(p.sale, height)}
                    </div>
                  )}
                </div>
              </Link>
            )
          })}
        </div>
      )}

      {total > PAGE && (
        <div className="pager">
          <button className="btn btn-sm btn-ghost" disabled={page === 0}
                  onClick={() => setPage((n) => Math.max(0, n - 1))}>Previous</button>
          <span className="small dim">
            {page * PAGE + 1}–{Math.min(total, (page + 1) * PAGE)} of {total}
          </span>
          <button className="btn btn-sm btn-ghost" disabled={(page + 1) * PAGE >= total}
                  onClick={() => setPage((n) => n + 1)}>Next</button>
        </div>
      )}
    </div>
  )
}
