import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { hasProvider, getAddress, getUtxos, getContractKey, signContractSpend,
         supportsContractEngine, friendly } from '../lib/wallet'
import { useStore } from '../lib/store'
import { amount, atomsArg, big, capitalise, shortHex, toAtoms } from '../lib/format'
import { pickFeeAsset } from '../lib/feeasset'
import { Hex, Notice } from './ui'

// A capped sale's purchase, signed by the buyer's own wallet.
//
// The sale sells only to the keys on its allocation list, each up to its cap,
// and its program checks the buyer's signature over the whole transaction. A
// wallet whose contract key is on the list builds that transaction itself:
// Levo hands it the sale's terms and state and the buyer's place on the list,
// and the wallet checks the cap, recomputes the sale's address, runs the
// program, shows the purchase and signs. Levo signs nothing, and no account is
// needed here: the cap is the chain's.
export default function CappedBuy({ project, onBought }) {
  const { payment, explorer } = useStore()
  const sale = project.sale
  const decimals = project.decimals ?? 8
  const [can, setCan] = useState(false)
  const [key, setKey] = useState(null)
  const [tokens, setTokens] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [sent, setSent] = useState(null)

  useEffect(() => { if (hasProvider()) supportsContractEngine().then(setCan) }, [])

  const list = (sale.capped && sale.capped.allocations) || []
  const mine = key ? list.find((a) => a.key === key) : null
  const left = mine ? big(mine.cap_atoms) - big(mine.bought_atoms) : 0n

  async function useWallet() {
    setError(null); setBusy(true)
    try { setKey(await getContractKey()) } catch (e) { setError(capitalise(friendly(e))) } finally { setBusy(false) }
  }

  async function buy(e) {
    e.preventDefault()
    if (busy) return
    setError(null); setSent(null); setBusy(true)
    try {
      const atoms = toAtoms(tokens, decimals)
      if (atoms === null || atoms <= 0n) throw new Error('Enter how many ' + project.ticker + ' you want, with at most ' + decimals + ' decimal places.')
      if (atoms > left) throw new Error('Your key may buy ' + amount(left, decimals) + ' ' + project.ticker + ' more from this sale; the sale refuses a purchase past its cap.')
      const body = { buyer_key: key, token_atoms: atomsArg(atoms), token_address: await getAddress() }
      let r = await api.walletPurchase(project.slug, body)
      if (r.needs_fee_asset) {
        const asset = pickFeeAsset(r.fee_rates, await getUtxos({}))
        if (!asset) throw new Error('This wallet holds no coin of an asset the network accepts for fees.')
        r = await api.walletPurchase(project.slug, { ...body, fee_asset: asset })
      }
      const txid = await signContractSpend({ templateHash: r.templateHash, instance: r.instance, request: r.request, fund: true })
      setSent(txid)
      if (onBought) onBought()
    } catch (e2) { setError(capitalise(friendly(e2))) } finally { setBusy(false) }
  }

  return (
    <div className="capped-buy">
      <p>
        This sale&apos;s caps are in its address. It sells only to the keys on its
        allocation list, each up to its own cap, and every buyer&apos;s running
        total is carried in the sale itself, so the chain refuses a purchase
        past a cap whoever builds it.
      </p>
      {can && !key && (
        <div>
          <p className="small dim">
            Your wallet&apos;s key on the list is its contract key. Your wallet
            builds the purchase itself, shows you what it buys and pays, and
            signs it with that key.
          </p>
          <button className="btn btn-primary btn-sm" onClick={useWallet} aria-disabled={busy} data-capped-wallet>
            Use my wallet
          </button>
        </div>
      )}
      {key && !mine && (
        <Notice kind="bad">
          This wallet&apos;s contract key, <span className="mono">{shortHex(key)}</span>, is not on this
          sale&apos;s allocation list, and the covenant sells only to the keys on it. Give the key to the
          project if it should be listed.
        </Notice>
      )}
      {mine && (
        <form onSubmit={buy}>
          <p className="small" data-capped-cap>
            Your key may buy {amount(mine.cap_atoms, decimals)} {project.ticker} from this sale; it has
            bought {amount(mine.bought_atoms, decimals)}, so {amount(left, decimals)} is still open.
            You pay {payment.label}, and the network fee is paid in it too where the network takes it.
          </p>
          <div className="field">
            <label htmlFor="capped-qty">{project.ticker} to buy</label>
            <input id="capped-qty" className="mono" inputMode="decimal" value={tokens} required
                   onChange={(e) => setTokens(e.target.value)}
                   placeholder={amount(sale.terms.min_lot, decimals)} />
          </div>
          <button className="btn btn-primary btn-sm" aria-disabled={busy} data-capped-buy>
            {busy ? 'Waiting for your wallet' : 'Buy with my wallet'}
          </button>
        </form>
      )}
      {sent && (
        <div data-capped-sent={sent}>
        <Notice kind="good">
          <strong>Your purchase is broadcast.</strong> Levo follows the sale to its next address once the
          chain has it.
          <div style={{ marginTop: '.4rem' }}>
            <Hex value={sent} href={explorer('tx', sent)} label="transaction id" />
          </div>
        </Notice>
        </div>
      )}
      {error && <Notice kind="bad">{error}</Notice>}
      <p className="small dim" style={{ marginTop: '1rem' }}>
        From your own node instead, with a key you hold yourself:
      </p>
      <pre className="small" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>levo buy {project.slug} --tokens 100 --buyer-key {'<'}your key{'>'} --seqc {'<'}path to seqc{'>'}</pre>
    </div>
  )
}
