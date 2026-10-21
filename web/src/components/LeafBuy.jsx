import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { hasProvider, walletMode, takeOrder, friendly } from '../lib/wallet'
import { useStore } from '../lib/store'
import { amount, atomsArg, capitalise, shortHex, toAtoms } from '../lib/format'
import { Notice, useReread } from './ui'

// A purchase from leaves, in a wallet's developer mode.
//
// The sale's covenant is the order book's fill covenant, so the wallet's
// operator can fill it in a round: the round spends the sale once for every
// buyer in it, pays the treasury on the chain what the covenant asks for all
// of them together, rests the remainder at the sale address, and pays each
// buyer a leaf of its own share. The wallet reads the sale on its own node,
// shows the fill, the price and the operator's fee, and asks the operator;
// Levo records the take against this account and counts it once its round is
// in a block.
const SAID = {
  pending: 'waiting for the operator’s round',
  in_round: 'its round is broadcast, not yet in a block',
  confirmed: 'bought: its round is in a block',
  undone: 'its round’s block left the chain; it counts again once the round is mined again',
  void: 'not run',
}

export default function LeafBuy({ project, onTaken }) {
  const { config, payment } = useStore()
  const decimals = project.decimals ?? 8
  const [mode, setMode] = useState(null)
  const [order, setOrder] = useState(null)
  const [tokens, setTokens] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [takes, setTakes] = useState([])

  useEffect(() => { if (hasProvider()) walletMode().then(setMode) }, [])
  useEffect(() => {
    if (mode !== 'developer' || !config || !config.leaves) return
    api.leafOrder(project.slug).then(setOrder).catch(() => setOrder(null))
    api.myTakes(project.slug).then((r) => setTakes(r.takes || [])).catch(() => {})
  }, [mode, config, project.slug, project.sale && project.sale.funding && project.sale.funding.txid])
  useReread(() => {
    if (mode === 'developer') api.myTakes(project.slug).then((r) => setTakes(r.takes || [])).catch(() => {})
  }, 10000, [mode, project.slug])

  if (mode !== 'developer' || !config || !config.leaves || !order) return null

  async function take(e) {
    e.preventDefault()
    if (busy || !tokens) return
    setError(null); setBusy(true)
    try {
      const atoms = toAtoms(tokens, decimals)
      if (atoms === null || atoms <= 0n) throw new Error('Enter how many ' + project.ticker + ' you want, with at most ' + decimals + ' decimal places.')
      // Levo's own checks first: the minimum purchase, what rests, the cap.
      await api.buy(project.slug, { token_atoms: atomsArg(atoms) })
      const fresh = await api.leafOrder(project.slug)
      if (!fresh.fillable) throw new Error(fresh.why)
      const r = await takeOrder(fresh.order, atoms)
      await api.noteTake(project.slug, r.take)
      setTokens('')
      const mine = await api.myTakes(project.slug)
      setTakes(mine.takes || [])
      if (onTaken) onTaken()
    } catch (e2) { setError(capitalise(friendly(e2))) } finally { setBusy(false) }
  }

  return (
    <div className="leaf-buy" style={{ marginTop: '1.25rem' }}>
      <h4>Buy from leaves <span className="pill">developer mode</span></h4>
      {!order.fillable ? (
        <p className="small dim">{capitalise(order.why)}.</p>
      ) : (
        <>
          <p className="small dim">
            Your wallet asks its operator to fill this sale in its next round, with your leaves. The round pays
            the treasury on the chain for every buyer in it together and pays you a leaf of the tokens. Keep the
            wallet unlocked until the round is built: it hands its part over then, and a take whose part does
            not come in time is not run, your leaves yours again.
          </p>
          <form onSubmit={take} className="row" style={{ gap: '0.5rem', alignItems: 'flex-end' }}>
            <label className="field">
              <span>{project.ticker} to buy (at least {amount(order.min_lot, decimals)})</span>
              <input inputMode="decimal" value={tokens} onChange={(e) => setTokens(e.target.value)} readOnly={busy} data-leaf-amount />
            </label>
            <button className="btn btn-sm" type="submit" aria-disabled={busy || !tokens} data-leaf-buy>
              {busy ? 'Asking the wallet…' : 'Buy from leaves'}
            </button>
          </form>
        </>
      )}
      {error && <Notice kind="error">{error}</Notice>}
      {takes.length > 0 && (
        <ul className="small" style={{ marginTop: '0.75rem' }}>
          {takes.map((t) => (
            <li key={t.take}>
              {amount(t.token_atoms, decimals)} {project.ticker} for {amount(t.payment_atoms, payment.decimals)} {payment.label}: {SAID[t.state] || t.state}
              {t.round ? <> (round {shortHex(t.round)}, the sale at its input {t.input})</> : null}
              {t.state === 'void' && t.reason ? <>: {t.reason}</> : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
