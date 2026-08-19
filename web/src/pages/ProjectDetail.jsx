import { useEffect, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { api } from '../lib/api'
import { useStore } from '../lib/store'
import { amount, closeLabel, compact, shortHex, toAtoms } from '../lib/format'
import SignIn from '../components/SignIn'
import Beam from '../components/Beam'

function Terms({ sale }) {
  const t = sale.terms
  return (
    <table className="terms">
      <tbody>
        <tr><th>Token</th><td>{t.token_asset}</td></tr>
        <tr><th>Paid in</th><td>{t.payment_asset}</td></tr>
        <tr>
          <th>Price</th>
          <td>{amount(Math.round((t.price_num / t.price_den) * 1e8))} USDX per token
            <div className="dim small" style={{ fontFamily: 'var(--body)' }}>
              exactly ceil(n × {t.price_num} / {t.price_den}) atoms for n token atoms
            </div>
          </td>
        </tr>
        <tr><th>Minimum purchase</th><td>{amount(t.min_lot)} tokens</td></tr>
        <tr><th>For sale</th><td>{t.total_atoms ? amount(t.total_atoms) : '—'} tokens</td></tr>
        <tr><th>Closes</th><td>{closeLabel(t.close_locktime)}</td></tr>
        <tr><th>Treasury</th><td>{t.treasury_prog}</td></tr>
        <tr><th>Reclaim key</th><td>{t.reclaim_xonly}</td></tr>
      </tbody>
    </table>
  )
}

function BuyPanel({ project, onBought }) {
  const { signedIn, standing, tiers, refresh } = useStore()
  const sale = project.sale
  const [tokens, setTokens] = useState('')
  const [plan, setPlan] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const open = sale && (sale.status === 'live' || sale.status === 'partial')

  async function quote(e) {
    e.preventDefault()
    setError(null); setPlan(null); setBusy(true)
    try {
      const atoms = toAtoms(tokens, 8)
      if (atoms === null || atoms <= 0n) throw new Error('enter how many tokens you want')
      const p = await api.buy(project.slug, { token_atoms: Number(atoms) })
      setPlan(p)
    } catch (e) {
      setError(e.message)
    } finally { setBusy(false) }
  }

  if (!open) {
    return (
      <div className="card">
        <h3>Not open</h3>
        <p className="small dim" style={{ marginBottom: 0 }}>
          {sale && sale.status === 'draft'
            ? 'The project has not locked its tokens yet, so there is nothing to buy.'
            : sale && sale.status === 'ghost'
            ? 'This sale’s funding was undone by a Bitcoin-driven reorg. It is not funded and cannot be bought.'
            : sale && sale.status === 'sold_out'
            ? 'Every token in this sale has been sold.'
            : 'This sale is closed.'}
        </p>
      </div>
    )
  }

  if (!signedIn) {
    return (
      <div className="card">
        <h3>Sign in to take a position</h3>
        <p className="small dim">
          Your tier decides how much you can put into this sale, and your tier
          comes from your stake.
        </p>
        <SignIn label="Sign in with your wallet" />
      </div>
    )
  }

  const tier = standing.tier
  const canInvest = tier.cap_atoms > 0

  return (
    <div className="card">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: '1rem' }}>
        <h3>Take a position</h3>
        <span className="pill">{tier.name}</span>
      </div>

      {tiers && (
        <Beam tiers={tiers.tiers} stakeAtoms={standing.stake_atoms} compactMode />
      )}

      {!canInvest ? (
        <>
          <p className="small dim">
            The Visitor tier has no allocation. Stake 40,000 SEQ, link the
            staking key, and the first tier opens.
          </p>
          <Link className="btn btn-sm" to="/account">Link a staking key</Link>
        </>
      ) : (
        <form onSubmit={quote}>
          <div className="field">
            <label htmlFor="qty">Tokens</label>
            <input id="qty" className="mono" inputMode="decimal" value={tokens}
                   onChange={(e) => setTokens(e.target.value)}
                   placeholder={'min ' + amount(sale.terms.min_lot)} />
            <div className="hint">
              Your cap in this sale is {amount(tier.cap_atoms)} USDX.
            </div>
          </div>
          <button className="btn btn-primary" disabled={busy}>
            {busy ? 'Working out the price…' : 'Price this purchase'}
          </button>
        </form>
      )}

      {error && <div className="notice bad" style={{ marginTop: '1rem' }}>{error}</div>}

      {plan && (
        <div style={{ marginTop: '1.25rem' }}>
          <div className="kv"><span>You receive</span><b>{amount(plan.token_atoms)} {project.ticker}</b></div>
          <div className="kv"><span>You pay</span><b>{amount(plan.payment_atoms)} USDX</b></div>
          <div className="kv"><span>Left resting after</span><b>{amount(plan.remainder_atoms)}</b></div>
          <div className="kv"><span>Cap remaining after</span><b>{amount(plan.allowance_after_atoms)} USDX</b></div>

          <div className="notice" style={{ marginTop: '1rem' }}>
            <strong>What your wallet must build.</strong> Spend the covenant
            output and satisfy these, and the chain delivers your tokens in the
            same transaction that pays the project.
            <table className="terms" style={{ marginTop: '.75rem' }}>
              <tbody>
                {plan.required_outputs.map((o) => (
                  <tr key={o.index}>
                    <th>Output {o.index}</th>
                    <td>
                      {o.role}
                      <div className="dim small" style={{ fontFamily: 'var(--body)' }}>
                        {o.exact_atoms !== undefined
                          ? 'exactly ' + amount(o.exact_atoms) + ' atoms'
                          : 'at least ' + amount(o.min_atoms) + ' atoms'}
                      </div>
                    </td>
                  </tr>
                ))}
                <tr>
                  <th>Covenant input</th>
                  <td>{plan.covenant.outpoint
                        ? shortHex(plan.covenant.outpoint.txid, 10, 8) + ':' + plan.covenant.outpoint.vout
                        : '—'}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <p className="small dim" style={{ marginTop: '.9rem', marginBottom: 0 }}>
            The {plan.cap.enforced_by === 'levo' ? 'cap above is Levo’s allocation policy' : ''}.
            The covenant enforces the price, the treasury, the token and the
            minimum lot. It does not enforce a per-buyer maximum, and it is
            permissionless by design.
          </p>
        </div>
      )}
    </div>
  )
}

export default function ProjectDetail() {
  const { slug } = useParams()
  const [project, setProject] = useState(null)
  const [error, setError] = useState(null)

  const load = () => api.project(slug).then(setProject).catch((e) => setError(e.message))
  useEffect(() => { load() }, [slug])   // eslint-disable-line react-hooks/exhaustive-deps

  if (error) return <div className="wrap section"><div className="notice bad">{error}</div></div>
  if (!project) return <div className="wrap section"><p className="dim">Loading…</p></div>

  const sale = project.sale

  return (
    <div className="wrap section">
      <p className="eyebrow">{project.ticker}</p>
      <h1 style={{ fontSize: 'clamp(2rem, 4.5vw, 3.2rem)' }}>{project.name}</h1>
      <p className="hero-lede" style={{ marginTop: '1rem' }}>{project.summary}</p>

      <div className="split" style={{ marginTop: '2.5rem' }}>
        <div>
          {project.description && (
            <div style={{ marginBottom: '2.5rem' }}>
              {project.description.split('\n').filter(Boolean).map((para, i) => (
                <p key={i}>{para}</p>
              ))}
            </div>
          )}

          {sale && (
            <>
              <h2 style={{ marginBottom: '1rem' }}>Terms</h2>
              <p className="small dim" style={{ maxWidth: '60ch' }}>
                These are the values compiled into the covenant. Change any one
                of them and the sale address changes with it, which is what makes
                the address worth checking.
              </p>
              <Terms sale={sale} />

              {project.verify && (
                <div className="notice good" style={{ marginTop: '1.5rem' }}>
                  <strong>Verify before you buy.</strong> {project.verify.how}.
                  <table className="terms" style={{ marginTop: '.75rem' }}>
                    <tbody>
                      <tr><th>Sale address</th><td>{project.verify.script_pubkey}</td></tr>
                      <tr><th>Internal key</th>
                          <td style={{ fontFamily: 'var(--body)' }}>{project.verify.internal_key}</td></tr>
                      {sale.funding && (
                        <tr><th>Funded at</th>
                            <td>{sale.funding.txid}:{sale.funding.vout}</td></tr>
                      )}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>

        <div className="sticky">
          <BuyPanel project={project} onBought={load} />
          {sale && sale.terms.total_atoms && (
            <div className="card" style={{ marginTop: '1rem' }}>
              <div className="kv"><span>Sold</span><b>{compact(sale.sold_atoms)}</b></div>
              <div className="kv"><span>Still locked</span><b>{compact(sale.locked_atoms)}</b></div>
              <div className="kv"><span>Total for sale</span><b>{compact(sale.terms.total_atoms)}</b></div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
