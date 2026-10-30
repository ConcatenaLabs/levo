import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { hasProvider, getUtxos, getAddress, signPset, broadcastPset, friendly } from '../lib/wallet'
import { useStore } from '../lib/store'
import { amount, atomsArg, big, capitalise, toAtoms } from '../lib/format'
import { btcText } from '../lib/money'
import { Copy, Notice } from './ui'

// Paying a sale in BTC, over Lightning.
//
// The buyer names an amount, their wallet signs a small reservation, and the
// page shows an invoice. Paying it from any Lightning wallet delivers the
// tokens: the payment's secret is what unlocks them, and the project's
// receiver broadcasts the delivery the moment it is paid. levod builds the
// reservation; it never holds the tokens, the bond or the BTC.

const SAID = {
  reserving: 'Your wallet is sending the reservation.',
  reserved: 'Reserved. Pay the invoice below from any Lightning wallet.',
  paid: 'Paid. Your tokens are on their way.',
  delivered: 'Done: your tokens are in your wallet.',
  returned: 'The invoice was not paid in time, so the tokens went back to the sale and the bond to the project.',
  expired: 'The invoice expired before it was paid. Reserve again to buy.',
}

export function sats(terms, tokenAtoms) {
  const ln = terms.lightning
  const n = big(tokenAtoms) * big(ln.btc_num)
  const d = big(ln.btc_den)
  return (n + d - 1n) / d
}

export default function LightningBuy({ project, onSettled }) {
  const { money } = useStore()
  const sale = project.sale
  const ln = sale.terms.lightning
  const decimals = project.decimals ?? 8
  const [tokens, setTokens] = useState('')
  const [lot, setLot] = useState(null)
  const [status, setStatus] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const atoms = toAtoms(tokens, decimals)

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

  async function reserve(e) {
    e.preventDefault()
    if (busy || !atoms) return
    setError(null); setBusy(true)
    try {
      if (!hasProvider()) throw new Error('A browser wallet is needed to reserve: it signs the small bond that holds your lot while you pay.')
      const fee = big(ln.fee_allowance)
      const need = big(ln.bond_atoms) + fee
      const [list, addr] = await Promise.all([getUtxos({}), getAddress({})])
      const mine = list.filter((x) => (x.asset || '').toLowerCase() === ln.bond_asset)
      const checked = mine.length
        ? await api.checkOutputs(mine.slice(0, 32).map((x) => ({ txid: x.txid, vout: x.vout })))
        : { outputs: [] }
      const ok = new Set(checked.outputs.filter((x) => x.spendable).map((x) => x.txid + ':' + x.vout))
      const picked = []
      let total = 0n
      for (const u of mine) {
        if (!ok.has(u.txid + ':' + u.vout)) continue
        picked.push({ txid: u.txid, vout: u.vout })
        total += big(u.value)
        if (total >= need) break
      }
      if (total < need) {
        throw new Error('A reservation holds a bond of ' + money(ln.bond_atoms, ln.bond_asset) +
          ', which comes back to you when you pay, and your wallet does not hold enough of it.')
      }
      const r = await api.lightningReserve(project.slug, {
        token_atoms: atomsArg(atoms), token_address: addr, inputs: picked, fee_atoms: atomsArg(fee),
      })
      const signed = await signPset(r.pset)
      await broadcastPset(signed)
      setLot(r.lot)
    } catch (err) {
      setError(capitalise(friendly(err)))
    } finally { setBusy(false) }
  }

  const state = status ? status.state : lot ? 'reserving' : null
  return (
    <div className="card" style={{ marginBottom: '1rem' }}>
      <h3>Pay in BTC</h3>
      {!lot && (
        <form onSubmit={reserve}>
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
          <button className="btn btn-primary btn-sm" aria-disabled={busy || !atoms}>
            {busy ? 'Working…' : 'Reserve and get an invoice'}
          </button>
        </form>
      )}
      {lot && (
        <div>
          <div className="kv"><span>You pay</span><b>{btcText(lot.sats)}</b></div>
          <div className="kv"><span>You receive</span><b>{amount(lot.token_atoms, decimals)} {project.ticker}</b></div>
          <p className="small" role="status">{SAID[state] || ''}</p>
          {(state === 'reserved' || state === 'reserving') && (
            <div className="field">
              <label htmlFor="ln-invoice">Invoice</label>
              <textarea id="ln-invoice" className="mono" rows={4} readOnly value={lot.bolt11}
                        onFocus={(e) => e.target.select()} />
              <div style={{ display: 'flex', gap: '.5rem', marginTop: '.4rem' }}>
                <Copy value={lot.bolt11} label="Copy the invoice" />
                <a className="btn btn-sm" href={'lightning:' + lot.bolt11}>Open in a Lightning wallet</a>
              </div>
              {state === 'reserving' && (
                <p className="small dim">Pay once the reservation is confirmed; this page says so.</p>
              )}
            </div>
          )}
        </div>
      )}
      {error && <Notice kind="bad">{error}</Notice>}
    </div>
  )
}
