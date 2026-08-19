import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { useStore } from '../lib/store'
import { amount, toAtoms } from '../lib/format'
import SignIn from '../components/SignIn'
import Beam from '../components/Beam'

const USDX = '2a515539da5e6a60caa7766ecd65bac0c10d15717ddd2088844ba58f4d04b9de'

// Listing is a two-step commitment, and the split is deliberate. Step one fixes
// the terms and derives an address from them. Step two is the project sending
// its tokens there. Between the two nothing is investable, because a sale
// nobody has funded is a promise rather than an offer.

export default function Launch() {
  const { signedIn, standing, tiers, mayList } = useStore()
  const [form, setForm] = useState({
    slug: '', name: '', ticker: '', summary: '', description: '',
    token_asset: '', treasury_prog: '', reclaim_xonly: '',
    total: '', price: '', min_lot: '', close: '',
  })
  const [created, setCreated] = useState(null)
  const [lock, setLock] = useState({ txid: '', vout: '0' })
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })

  async function submit(e) {
    e.preventDefault()
    setError(null); setBusy(true)
    try {
      const total = toAtoms(form.total, 8)
      const minLot = toAtoms(form.min_lot, 8)
      const priceAtoms = toAtoms(form.price, 8)
      if (!total || !minLot || !priceAtoms) throw new Error('amount, minimum and price are all required')
      const close = Math.floor(new Date(form.close + 'T23:59:59Z').getTime() / 1000)
      if (!close || Number.isNaN(close)) throw new Error('pick a close date')

      const terms = {
        token_asset: form.token_asset.trim().toLowerCase(),
        payment_asset: USDX,
        // price is USDX atoms per whole token; per token ATOM that is
        // priceAtoms / 1e8, kept as an exact fraction so the covenant's ceiling
        // arithmetic matches what is shown here.
        price_num: Number(priceAtoms),
        price_den: 100000000,
        treasury_prog: form.treasury_prog.trim().toLowerCase(),
        min_lot: Number(minLot),
        close_locktime: close,
        reclaim_xonly: form.reclaim_xonly.trim().toLowerCase(),
        total_atoms: Number(total),
      }
      const project = {
        slug: form.slug.trim().toLowerCase(), name: form.name.trim(),
        ticker: form.ticker.trim().toUpperCase(), summary: form.summary.trim(),
        description: form.description.trim(), links: {},
      }
      setCreated(await api.createProject(project, terms))
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function confirmLock(e) {
    e.preventDefault()
    setError(null); setBusy(true)
    try {
      const r = await api.lock(created.project.slug, lock.txid.trim(), Number(lock.vout))
      setCreated({ ...created, project: r, locked: true })
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  if (!signedIn) {
    return (
      <div className="wrap section" style={{ maxWidth: 640 }}>
        <div className="section-head">
          <h2>Launch a project</h2>
          <p>Listing is open to the top tier. Sign in and Levo will tell you where you stand.</p>
        </div>
        <div className="card"><SignIn /></div>
      </div>
    )
  }

  if (!mayList) {
    const top = tiers && tiers.tiers[tiers.tiers.length - 1]
    return (
      <div className="wrap section">
        <div className="section-head">
          <h2>Listing needs the top tier</h2>
          <p>
            Running a sale asks other people to commit money to you, so Levo
            asks you to commit stake first. You are {standing.tier.name}
            {top ? '; listing opens at ' + amount(top.min_stake_atoms) + ' SEQ staked.' : '.'}
          </p>
        </div>
        {tiers && <Beam tiers={tiers.tiers} stakeAtoms={standing.stake_atoms} />}
        <Link className="btn" style={{ marginTop: '2rem' }} to="/account">Link more stake</Link>
      </div>
    )
  }

  if (created) {
    const sale = created.project.sale || (created.project.sale === null ? null : null)
    const spk = created.lock ? created.lock.script_pubkey : (created.project.sale && created.project.sale.script_pubkey)
    const locked = created.locked
    return (
      <div className="wrap section" style={{ maxWidth: 760 }}>
        <p className="eyebrow">{locked ? 'Sale open' : 'Step 2 of 2'}</p>
        <h2>{locked ? 'Your sale is live' : 'Lock the tokens'}</h2>

        {!locked ? (
          <>
            <p style={{ maxWidth: '62ch' }}>
              Send exactly {created.lock ? amount(created.lock.atoms) : ''} of your
              token to the address below. Until that output exists and matches
              your terms, the sale stays a draft and nobody can buy.
            </p>
            <div className="notice" style={{ marginBottom: '1.5rem' }}>
              <strong>Sale address</strong>
              <div className="mono small" style={{ wordBreak: 'break-all', marginTop: '.5rem' }}>{spk}</div>
              <p className="small dim" style={{ margin: '.75rem 0 0' }}>
                This address is derived from your terms. It has no key path, so
                once funded you cannot reprice, redirect or withdraw the sale
                either. After the close date you can reclaim whatever did not sell.
              </p>
            </div>
            <form onSubmit={confirmLock}>
              <div className="field">
                <label htmlFor="txid">Funding transaction id</label>
                <input id="txid" className="mono" value={lock.txid}
                       onChange={(e) => setLock({ ...lock, txid: e.target.value })} />
              </div>
              <div className="field">
                <label htmlFor="vout">Output index</label>
                <input id="vout" className="mono" value={lock.vout}
                       onChange={(e) => setLock({ ...lock, vout: e.target.value })} />
              </div>
              {error && <div className="notice bad" style={{ marginBottom: '1rem' }}>{error}</div>}
              <button className="btn btn-primary" disabled={busy || !lock.txid.trim()}>
                {busy ? 'Checking the chain…' : 'Confirm the lock'}
              </button>
            </form>
          </>
        ) : (
          <>
            <div className="notice good">
              Levo read the output on chain and it matches your terms. The sale
              is open.
            </div>
            <Link className="btn btn-primary" style={{ marginTop: '1.5rem' }}
                  to={'/p/' + created.project.slug}>View the sale</Link>
          </>
        )}
      </div>
    )
  }

  return (
    <div className="wrap section" style={{ maxWidth: 760 }}>
      <p className="eyebrow">Step 1 of 2</p>
      <h2>Set the terms</h2>
      <p style={{ maxWidth: '62ch' }}>
        Every field below is compiled into the sale covenant and committed in
        its address. Once you lock the tokens none of it can change, so read it
        twice.
      </p>

      <form onSubmit={submit} style={{ marginTop: '2rem' }}>
        <h3 style={{ marginBottom: '1rem' }}>The project</h3>
        <div className="grid-2">
          <div className="field">
            <label htmlFor="name">Name</label>
            <input id="name" value={form.name} onChange={set('name')} />
          </div>
          <div className="field">
            <label htmlFor="ticker">Ticker</label>
            <input id="ticker" value={form.ticker} onChange={set('ticker')} placeholder="HLX" />
          </div>
        </div>
        <div className="field">
          <label htmlFor="slug">Address on Levo</label>
          <input id="slug" className="mono" value={form.slug} onChange={set('slug')}
                 placeholder="helios-grid" />
          <div className="hint">Lowercase letters, digits and hyphens.</div>
        </div>
        <div className="field">
          <label htmlFor="summary">One line</label>
          <input id="summary" value={form.summary} onChange={set('summary')} />
        </div>
        <div className="field">
          <label htmlFor="desc">What you are building</label>
          <textarea id="desc" value={form.description} onChange={set('description')} />
        </div>

        <h3 style={{ margin: '2rem 0 1rem' }}>The sale</h3>
        <div className="field">
          <label htmlFor="asset">Token asset id</label>
          <input id="asset" className="mono" value={form.token_asset} onChange={set('token_asset')}
                 placeholder="64 hex characters" />
        </div>
        <div className="grid-2">
          <div className="field">
            <label htmlFor="total">Tokens for sale</label>
            <input id="total" className="mono" value={form.total} onChange={set('total')} />
          </div>
          <div className="field">
            <label htmlFor="price">Price per token, USDX</label>
            <input id="price" className="mono" value={form.price} onChange={set('price')}
                   placeholder="0.25" />
          </div>
        </div>
        <div className="grid-2">
          <div className="field">
            <label htmlFor="min">Minimum purchase</label>
            <input id="min" className="mono" value={form.min_lot} onChange={set('min_lot')} />
            <div className="hint">
              The covenant refuses buys below this and refuses to leave less than
              this resting, which is what keeps the sale from being ground into dust.
            </div>
          </div>
          <div className="field">
            <label htmlFor="close">Closes</label>
            <input id="close" type="date" value={form.close} onChange={set('close')} />
            <div className="hint">After this you can reclaim whatever did not sell.</div>
          </div>
        </div>
        <div className="field">
          <label htmlFor="treasury">Treasury payout program</label>
          <input id="treasury" className="mono" value={form.treasury_prog} onChange={set('treasury_prog')}
                 placeholder="32-byte taproot witness program, hex" />
          <div className="hint">Where buyers' USDX is paid. The covenant checks every payment lands here.</div>
        </div>
        <div className="field">
          <label htmlFor="reclaim">Reclaim key</label>
          <input id="reclaim" className="mono" value={form.reclaim_xonly} onChange={set('reclaim_xonly')}
                 placeholder="32-byte x-only public key, hex" />
          <div className="hint">The only key that can sweep unsold tokens after the close.</div>
        </div>

        {error && <div className="notice bad" style={{ marginBottom: '1rem' }}>{error}</div>}
        <button className="btn btn-primary" disabled={busy}>
          {busy ? 'Deriving the sale address…' : 'Derive the sale address'}
        </button>
      </form>
    </div>
  )
}
