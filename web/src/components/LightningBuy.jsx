import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { hasProvider, getAddress, friendly } from '../lib/wallet'
import { amount, atomsArg, big, capitalise, toAtoms } from '../lib/format'
import { btcText } from '../lib/money'
import { Copy, Notice } from './ui'
import { useStore } from '../lib/store'

// Paying a sale in BTC, over Lightning.
//
// The buyer names an amount and the address the tokens go to, and the page
// shows an invoice. Paying it from any Lightning wallet, or a Bitcoin Ark
// balance, is all there is: the project's receiver holds the payment, locks
// the lot for the buyer, delivers it with the payment's secret and only then
// takes the BTC. levod never holds the tokens or the BTC.

const SAID = {
  awaiting_payment: 'Pay the invoice below from any Lightning wallet.',
  paid: 'Payment received. Your tokens arrive with the next block, in about a minute.',
  delivered: 'Done: your tokens are at your address.',
  refunded: 'The payment could not be matched with tokens, so it went back to you.',
  returned: 'The payment went back to you, and the tokens to the sale.',
  expired: 'The invoice expired before it was paid. Order again to buy.',
}

export function sats(terms, tokenAtoms) {
  const ln = terms.lightning
  const n = big(tokenAtoms) * big(ln.btc_num)
  const d = big(ln.btc_den)
  return (n + d - 1n) / d
}

export default function LightningBuy({ project, onSettled }) {
  const { links } = useStore()
  // The deployment's wallet, when it names one, pays the invoice from a link.
  const wallet = (links && (links.Wallet || links.wallet)) || null
  const sale = project.sale
  const decimals = project.decimals ?? 8
  const [tokens, setTokens] = useState('')
  const [address, setAddress] = useState('')
  const [lot, setLot] = useState(null)
  const [status, setStatus] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const atoms = toAtoms(tokens, decimals)

  // A browser wallet, when there is one, fills in where the tokens go.
  useEffect(() => {
    if (!hasProvider()) return
    getAddress({}).then((a) => setAddress((v) => v || a || '')).catch(() => {})
  }, [])

  useEffect(() => {
    if (!lot) return undefined
    let alive = true
    const tick = () => api.lightningStatus(project.slug, lot.payment_hash)
      .then((s) => {
        if (!alive) return
        setStatus(s)
        if (s.state === 'delivered' && onSettled) onSettled()
      }).catch(() => {})
    tick()
    const t = setInterval(tick, 3000)
    return () => { alive = false; clearInterval(t) }
  }, [lot, project.slug])

  async function order(e) {
    e.preventDefault()
    if (busy || !atoms) return
    setError(null); setBusy(true)
    try {
      if (!address.trim()) throw new Error('Enter the Sequentia address your tokens go to.')
      const r = await api.lightningReserve(project.slug, {
        token_atoms: atomsArg(atoms), token_address: address.trim(),
      })
      setLot(r.lot)
    } catch (err) {
      setError(capitalise(friendly(err)))
    } finally { setBusy(false) }
  }

  const state = status ? status.state : lot ? 'awaiting_payment' : null
  return (
    <div className="card" style={{ marginBottom: '1rem' }}>
      <h3>Pay in BTC</h3>
      {!lot && (
        <form onSubmit={order}>
          <div className="field">
            <label htmlFor="ln-qty">{project.ticker} to buy</label>
            <input id="ln-qty" className="mono" inputMode="decimal" value={tokens}
                   onChange={(e) => setTokens(e.target.value)}
                   placeholder={amount(sale.terms.min_lot, decimals)} />
            <div className="hint">
              {atoms ? 'You pay ' + btcText(sats(sale.terms, atoms)) + ' over Lightning.' :
                'Minimum ' + amount(sale.terms.min_lot, decimals) + ' ' + project.ticker + '.'}
            </div>
          </div>
          <div className="field">
            <label htmlFor="ln-addr">Your Sequentia address</label>
            <input id="ln-addr" className="mono" value={address} spellCheck={false}
                   onChange={(e) => setAddress(e.target.value)} />
            <div className="hint">Where the tokens go. Your wallet's Receive tab shows it.</div>
          </div>
          <button className="btn btn-primary btn-sm" aria-disabled={busy || !atoms}>
            {busy ? 'Working…' : 'Get an invoice'}
          </button>
        </form>
      )}
      {lot && (
        <div>
          <div className="kv"><span>You pay</span><b>{btcText(lot.sats)}</b></div>
          <div className="kv"><span>You receive</span><b>{amount(lot.token_atoms, decimals)} {project.ticker}</b></div>
          <p className="small" role="status">{SAID[state] || ''}</p>
          {state === 'awaiting_payment' && (
            <div className="field">
              <label htmlFor="ln-invoice">Invoice</label>
              <textarea id="ln-invoice" className="mono" rows={4} readOnly value={lot.bolt11}
                        onFocus={(e) => e.target.select()} />
              <div style={{ display: 'flex', gap: '.5rem', marginTop: '.4rem' }}>
                <Copy value={lot.bolt11} label="Copy the invoice" />
                {wallet && (
                  <a className="btn btn-sm btn-primary" target="_blank" rel="noopener noreferrer"
                     href={wallet.replace(/#.*$/, '') + '#pay?invoice=' + encodeURIComponent(lot.bolt11)}>
                    Pay with your Sequentia wallet</a>
                )}
                <a className="btn btn-sm" href={'lightning:' + lot.bolt11}>Open in a Lightning wallet</a>
              </div>
              <p className="small dim">Until your tokens arrive, nobody has your BTC: if they cannot be
                delivered, the payment goes back to you.</p>
            </div>
          )}
        </div>
      )}
      {error && <Notice kind="bad">{error}</Notice>}
    </div>
  )
}
