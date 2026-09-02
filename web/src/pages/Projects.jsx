import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { useStore } from '../lib/store'
import { Notice, usePageTitle } from '../components/ui'
import { amount, closeIn, closeLabel, compact, priceLabel } from '../lib/format'

export const STATUS_LABEL = {
  live: 'open', partial: 'open', sold_out: 'sold out', draft: 'not funded',
  closed: 'closed', ghost: 'not funded (reorg)', reclaimed: 'reclaimed',
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

export default function Projects() {
  usePageTitle('Sales')
  const { payment, health } = useStore()
  const [projects, setProjects] = useState(null)
  const [error, setError] = useState(null)
  const height = health && health.node ? health.node.height : null

  const load = () => {
    setError(null)
    api.projects().then((r) => setProjects(r.projects)).catch((e) => setError(e.message))
  }
  useEffect(() => { load() }, [])

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

      {error && (
        <Notice kind="bad">
          {error} <button className="btn btn-sm btn-ghost" style={{ marginLeft: '.75rem' }} onClick={load}>Try again</button>
        </Notice>
      )}
      {!projects && !error && <p className="dim">Loading the sales…</p>}

      {projects && projects.length === 0 && (
        <div className="card">
          <h3>No sales yet</h3>
          <p className="small dim" style={{ marginBottom: '1rem' }}>
            Nothing has been listed. Listing needs a tier that may list, and a
            project has to lock its tokens before its sale opens.
          </p>
          <Link className="btn btn-sm" to="/launch">Launch a project</Link>
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
    </div>
  )
}
