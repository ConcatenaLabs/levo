import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { amount, closeLabel, compact } from '../lib/format'

function Status({ sale }) {
  if (!sale) return <span className="pill pill-draft">no sale</span>
  const s = sale.status
  const label = { live: 'open', partial: 'open', sold_out: 'sold out',
                  draft: 'not locked', closed: 'closed', ghost: 'lock reorged' }[s] || s
  return <span className={'pill pill-' + s}><i className="dot" />{label}</span>
}

function Progress({ sale }) {
  if (!sale || !sale.terms || !sale.terms.total_atoms) return null
  const total = Number(sale.terms.total_atoms)
  const sold = Number(sale.sold_atoms || 0)
  const pct = total ? Math.min(100, (sold / total) * 100) : 0
  return (
    <div>
      <div className="meter"><i style={{ width: pct + '%' }} /></div>
      <div className="small dim num" style={{ marginTop: '.35rem' }}>
        {compact(sold)} of {compact(total)} sold
      </div>
    </div>
  )
}

export default function Projects() {
  const [projects, setProjects] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.projects().then((r) => setProjects(r.projects)).catch((e) => setError(e.message))
  }, [])

  return (
    <div className="wrap section">
      <div className="section-head">
        <h2>Sales</h2>
        <p>
          Every sale here shows the address its tokens are locked at. Rebuild it
          from the terms before you buy, and you do not have to take Levo's word
          for any of them.
        </p>
      </div>

      {error && <div className="notice bad">{error}</div>}
      {!projects && !error && <p className="dim">Loading…</p>}

      {projects && projects.length === 0 && (
        <div className="card">
          <h3>No sales yet</h3>
          <p className="small dim" style={{ marginBottom: '1rem' }}>
            Nothing has been listed. A project needs the top tier to list, and
            has to lock its tokens before the sale opens.
          </p>
          <Link className="btn btn-sm" to="/launch">Launch a project</Link>
        </div>
      )}

      {projects && projects.length > 0 && (
        <div className="rows">
          {projects.map((p) => (
            <Link className="row" key={p.slug} to={'/p/' + p.slug}>
              <div>
                <div className="row-name">
                  {p.name}<span className="row-ticker">{p.ticker}</span>
                </div>
                <div className="row-sum">{p.summary}</div>
              </div>
              <div className="row-hide num small">
                {p.sale ? (
                  <>
                    {amount(Math.round((p.sale.terms.price_num / p.sale.terms.price_den) * 1e8))} USDX
                    <div className="dim" style={{ fontSize: '.78rem' }}>per token</div>
                  </>
                ) : <span className="dim">—</span>}
              </div>
              <div className="row-hide">
                <Progress sale={p.sale} />
              </div>
              <div style={{ textAlign: 'right' }}>
                <Status sale={p.sale} />
                {p.sale && (
                  <div className="small dim" style={{ marginTop: '.4rem' }}>
                    closes {closeLabel(p.sale.terms.close_locktime)}
                  </div>
                )}
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}
