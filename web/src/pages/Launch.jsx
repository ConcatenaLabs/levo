import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { useStore } from '../lib/store'
import { amount, atomsArg, capitalise, compact, toAtoms } from '../lib/format'
import { Notice, usePageTitle } from '../components/ui'
import SignIn from '../components/SignIn'
import Beam from '../components/Beam'

// Listing is a two-step commitment, and the split is deliberate. Step one fixes
// the terms and derives an address from them. Step two is the project sending
// its tokens there, which happens on the sale's own page so it survives a
// reload. Between the two nothing is investable, because a sale nobody has
// funded is a promise rather than an offer.

export default function Launch() {
  usePageTitle('Launch a project')
  const navigate = useNavigate()
  const { signedIn, standing, tiers, mayList, loading, payment, stake, config } = useStore()
  const [form, setForm] = useState({
    slug: '', name: '', ticker: '', summary: '', description: '', website: '',
    token_asset: '', decimals: '8', treasury_address: '', reclaim_xonly: '',
    total: '', price: '', min_lot: '', close: '',
  })
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [invalid, setInvalid] = useState({})

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })

  async function submit(e) {
    e.preventDefault()
    setError(null); setInvalid({}); setBusy(true)
    try {
      const decimals = Number(form.decimals)
      if (!Number.isInteger(decimals) || decimals < 0 || decimals > 8) throw bad('decimals', 'Decimals is a whole number from 0 to 8.')
      const total = toAtoms(form.total, decimals)
      const minLot = toAtoms(form.min_lot, decimals)
      const priceAtoms = toAtoms(form.price, payment.decimals)
      if (!total || total <= 0n) throw bad('total', 'Say how many tokens are for sale, with at most ' + decimals + ' decimals.')
      if (!minLot || minLot <= 0n) throw bad('min', 'Say the minimum purchase, with at most ' + decimals + ' decimals.')
      if (!priceAtoms || priceAtoms <= 0n) throw bad('price', 'Say the price per token in ' + payment.label + '.')
      if (!form.close) throw bad('close', 'Pick a close date.')
      const close = Math.floor(new Date(form.close + 'T23:59:59Z').getTime() / 1000)
      if (!close || Number.isNaN(close)) throw bad('close', 'Pick a close date.')

      // The covenant prices in payment atoms per token ATOM. A price typed per
      // whole token is that divided by 10^decimals, kept as an exact fraction
      // so the covenant's ceiling arithmetic matches what is shown here.
      const terms = {
        token_asset: form.token_asset.trim().toLowerCase(),
        payment_asset: payment.asset,
        price_num: atomsArg(priceAtoms),
        price_den: atomsArg(10n ** BigInt(decimals)),
        treasury_address: form.treasury_address.trim(),
        min_lot: atomsArg(minLot),
        close_locktime: close,
        reclaim_xonly: form.reclaim_xonly.trim().toLowerCase(),
        total_atoms: atomsArg(total),
      }
      const links = {}
      if (form.website.trim()) links.Website = form.website.trim()
      const project = {
        slug: form.slug.trim().toLowerCase(), name: form.name.trim(),
        ticker: form.ticker.trim().toUpperCase(), summary: form.summary.trim(),
        description: form.description.trim(), links, decimals,
      }
      const r = await api.createProject(project, terms)
      navigate('/p/' + r.project.slug)
    } catch (err) {
      setError(capitalise(err.message))
      if (err.field) setInvalid({ [err.field]: true })
    } finally { setBusy(false) }
  }

  if (loading) return <div className="wrap section"><h1 className="h2">Launch a project</h1><p className="dim">Loading your account…</p></div>

  if (!signedIn) {
    return (
      <div className="wrap section" style={{ maxWidth: 640 }}>
        <div className="section-head">
          <h1 className="h2">Launch a project</h1>
          <p>Listing is open to a tier that may list. Sign in and Levo will tell you where you stand.</p>
        </div>
        <div className="card"><SignIn /></div>
      </div>
    )
  }

  if (!mayList) {
    const listing = tiers && tiers.tiers.find((t) => t.may_list)
    return (
      <div className="wrap section">
        <div className="section-head">
          <h1 className="h2">Listing needs the {listing ? listing.name : 'top'} tier</h1>
          <p>
            Running a sale asks other people to commit money to you, so Levo
            asks you to commit stake first. You are {standing.tier.name}
            {listing ? '; listing opens at ' + amount(listing.min_stake_atoms, stake.decimals) + ' ' + stake.label + ' staked.' : '.'}
          </p>
        </div>
        {tiers && <Beam tiers={tiers.tiers} stakeAtoms={standing.stake_atoms}
                        paymentLabel={payment.label} stakeLabel={stake.label} />}
        <Link className="btn" style={{ marginTop: '2rem' }} to="/account">Link more stake</Link>
      </div>
    )
  }

  return (
    <div className="wrap section" style={{ maxWidth: 760 }}>
      <p className="eyebrow">Step 1 of 2</p>
      <h1 className="h2">Set the terms</h1>
      <p style={{ maxWidth: '62ch' }}>
        Every field under "The sale" is compiled into the sale covenant and
        committed in its address. Once you lock the tokens none of it can change,
        so read it twice. The next step, locking the tokens, happens on the sale's
        own page.
      </p>
      <Notice style={{ marginTop: '1.5rem' }}>
        <strong>Before you start you need:</strong> an issued asset, registered so
        wallets show its name; the whole allocation in a wallet you can send from; a
        taproot (<span className="mono">{config.hrp}1p…</span>) address for the treasury; and
        a reclaim key you can sign with outside a browser wallet.{' '}
        <span className="mono">bin/levo keygen</span> makes one; keep its secret offline.
      </Notice>

      <form onSubmit={submit} style={{ marginTop: '2rem' }} noValidate>
        <h3 style={{ marginBottom: '1rem' }}>The project</h3>
        <div className="grid-2">
          <div className="field">
            <label htmlFor="name">Name</label>
            <input id="name" value={form.name} onChange={set('name')} required maxLength={80} />
          </div>
          <div className="field">
            <label htmlFor="ticker">Ticker</label>
            <input id="ticker" value={form.ticker} onChange={set('ticker')} placeholder="HLX" required maxLength={12} />
          </div>
        </div>
        <div className="field">
          <label htmlFor="slug">Page name</label>
          <input id="slug" className="mono" value={form.slug} onChange={set('slug')}
                 placeholder="helios-grid" required aria-invalid={invalid.slug || undefined} />
          <div className="hint">Lowercase letters, digits and hyphens. The sale lives at /p/&lt;page name&gt;.</div>
        </div>
        <div className="field">
          <label htmlFor="summary">One line</label>
          <input id="summary" value={form.summary} onChange={set('summary')} maxLength={200} />
        </div>
        <div className="field">
          <label htmlFor="desc">What you are building</label>
          <textarea id="desc" value={form.description} onChange={set('description')} maxLength={8000} />
        </div>
        <div className="field">
          <label htmlFor="website">Website <span className="dim">(optional)</span></label>
          <input id="website" value={form.website} onChange={set('website')} placeholder="https://…" />
          <div className="hint">More links can be added on the sale's page after listing.</div>
        </div>

        <h3 style={{ margin: '2rem 0 1rem' }}>The sale</h3>
        <div className="grid-2">
          <div className="field">
            <label htmlFor="asset">Token asset id</label>
            <input id="asset" className="mono" value={form.token_asset} onChange={set('token_asset')}
                   placeholder="64 hex characters" required aria-invalid={invalid.token_asset || undefined} />
            <div className="hint">
              Issue it with a contract naming the token and its ticker, and
              register that contract in the Sequentia registry, so wallets show
              a name instead of a hex id. The contract is committed at issuance
              and cannot be added later.
            </div>
          </div>
          <div className="field">
            <label htmlFor="decimals">Decimal places</label>
            <input id="decimals" className="mono" inputMode="numeric" value={form.decimals} onChange={set('decimals')}
                   required aria-invalid={invalid.decimals || undefined} />
            <div className="hint">How the token is shown, 0 to 8. Match the precision in its registry contract.</div>
          </div>
        </div>
        <div className="grid-2">
          <div className="field">
            <label htmlFor="total">Tokens for sale</label>
            <input id="total" className="mono" inputMode="decimal" value={form.total} onChange={set('total')}
                   required aria-invalid={invalid.total || undefined} />
          </div>
          <div className="field">
            <label htmlFor="price">Price per token, {payment.label}</label>
            <input id="price" className="mono" inputMode="decimal" value={form.price} onChange={set('price')}
                   placeholder="0.25" required aria-invalid={invalid.price || undefined} />
          </div>
        </div>
        <div className="grid-2">
          <div className="field">
            <label htmlFor="min">Minimum purchase</label>
            <input id="min" className="mono" inputMode="decimal" value={form.min_lot} onChange={set('min_lot')}
                   required aria-invalid={invalid.min || undefined} />
            <div className="hint">
              The covenant refuses buys below this and refuses to leave less than
              this resting, which is what keeps the sale from being ground into dust.
            </div>
          </div>
          <div className="field">
            <label htmlFor="close">Reclaim opens</label>
            <input id="close" type="date" value={form.close} onChange={set('close')}
                   required aria-invalid={invalid.close || undefined} />
            <div className="hint">
              At the end of this day, UTC. From then on you can take back whatever did
              not sell. It does not stop selling: until you reclaim, the sale can still
              be filled.
            </div>
          </div>
        </div>
        <div className="field">
          <label htmlFor="treasury">Treasury address</label>
          <input id="treasury" className="mono" value={form.treasury_address} onChange={set('treasury_address')}
                 placeholder={config.hrp + '1p…'} required aria-invalid={invalid.treasury || undefined} />
          <div className="hint">
            Where buyers' {payment.label} is paid: any taproot address from your wallet.
            The covenant checks every payment lands here. Because this is your own key,
            you can always buy your own sale out at the published price.
          </div>
        </div>
        <div className="field">
          <label htmlFor="reclaim">Reclaim key</label>
          <input id="reclaim" className="mono" value={form.reclaim_xonly} onChange={set('reclaim_xonly')}
                 placeholder="32-byte x-only public key, hex" required aria-invalid={invalid.reclaim || undefined} />
          <div className="hint">
            The only key that can sweep unsold tokens after the close. Reclaiming
            signs a raw sighash, which browser wallets do not do, so use a key
            whose 32-byte secret you hold: <span className="mono">bin/levo keygen</span> prints
            a pair, and <span className="mono">bin/levo reclaim</span> signs with it on your machine.
          </div>
        </div>

        {error && <Notice kind="bad" style={{ marginBottom: '1rem' }}>{error}</Notice>}
        <button className="btn btn-primary" disabled={busy}>
          {busy ? 'Deriving the sale address…' : 'Derive the sale address'}
        </button>
      </form>
    </div>
  )
}

function bad(field, message) {
  const e = new Error(message)
  e.field = field
  return e
}
