import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { hasProvider, getUtxos, getAddress, broadcast } from '../lib/wallet'
import { amount, shortHex, toAtoms } from '../lib/format'

// Buying happens in three moves, and the interface shows all three because the
// buyer is the one who signs.
//
//   1. Price it.       Levo works out what the covenant will demand.
//   2. Build it.       Levo assembles the transaction. It signs nothing: the
//                      covenant input needs no signature, and the buyer's own
//                      inputs can only be signed by the buyer.
//   3. Sign and send.  Whatever holds the buyer's keys does this.

function Step({ n, title, children, done }) {
  return (
    <div className="step">
      <div className={'step-n' + (done ? ' done' : '')}>{n}</div>
      <div className="step-body">
        <h4>{title}</h4>
        {children}
      </div>
    </div>
  )
}

export default function BuyFlow({ project, tier, onSettled }) {
  const sale = project.sale
  const [rails, setRails] = useState(null)
  const [rail, setRail] = useState('usdx')
  const [tokens, setTokens] = useState('')
  const [plan, setPlan] = useState(null)
  const [built, setBuilt] = useState(null)
  const [signed, setSigned] = useState('')
  const [sentTxid, setSentTxid] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [funding, setFunding] = useState({ token_spk: '', change_spk: '', inputs: '', fee: '1000' })

  useEffect(() => { api.rails().then((r) => setRails(r.rails)).catch(() => {}) }, [])

  async function quote(e) {
    e.preventDefault()
    setError(null); setPlan(null); setBuilt(null); setSentTxid(null); setBusy(true)
    try {
      const atoms = toAtoms(tokens, 8)
      if (atoms === null || atoms <= 0n) throw new Error('enter how many tokens you want')
      setPlan(await api.buy(project.slug, { token_atoms: Number(atoms), rail }))
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function autofill() {
    setError(null); setBusy(true)
    try {
      const [u, a] = await Promise.all([getUtxos({}), getAddress({})])
      const list = (u && (u.utxos || u)) || []
      const usable = list.filter((x) => (x.asset || '').toLowerCase() === sale.terms.payment_asset && !x.blinded)
      if (!usable.length) throw new Error('your wallet has no unblinded ' +
        'outputs of the payment asset; a covenant buy cannot spend confidential ones')
      setFunding({
        ...funding,
        inputs: usable.slice(0, 3).map((x) => x.txid + ':' + x.vout).join('\n'),
        token_spk: (a && a.script_pubkey) || funding.token_spk,
        change_spk: (a && a.script_pubkey) || funding.change_spk,
      })
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function build(e) {
    e.preventDefault()
    setError(null); setBusy(true)
    try {
      const inputs = funding.inputs.split(/\s+/).filter(Boolean).map((s) => {
        const [txid, vout] = s.split(':')
        if (!txid || vout === undefined) throw new Error('inputs look like txid:vout, one per line')
        return { txid: txid.trim(), vout: Number(vout) }
      })
      setBuilt(await api.transaction(project.slug, {
        token_atoms: plan.token_atoms,
        buyer: {
          token_script_pubkey: funding.token_spk.trim(),
          change_script_pubkey: funding.change_spk.trim() || undefined,
          inputs,
          fee_atoms: Number(funding.fee || 0),
        },
      }))
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function send() {
    setError(null); setBusy(true)
    try {
      const txid = await broadcast(signed.trim())
      setSentTxid(typeof txid === 'string' ? txid : built.txid)
      await api.confirm(project.slug, {
        txid: built.txid,
        token_atoms: built.token_atoms,
        payment_atoms: built.payment_atoms,
      }).catch(() => {})
      onSettled && onSettled()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const btcRail = rails && rails.find((r) => r.id === 'btc')

  return (
    <div>
      {rails && (
        <div className="rail-pick">
          {rails.map((r) => (
            <button key={r.id} type="button"
                    className={'rail' + (rail === r.id ? ' on' : '')}
                    disabled={!r.available}
                    onClick={() => { setRail(r.id); setPlan(null); setBuilt(null) }}>
              <b>{r.label}</b>
              <span>{r.steps === 1 ? 'settles in the covenant' : 'two steps, via Lightning'}</span>
            </button>
          ))}
        </div>
      )}
      {rail === 'btc' && btcRail && !btcRail.available && (
        <div className="notice bad" style={{ marginBottom: '1rem' }}>
          {btcRail.unavailable_because}
        </div>
      )}

      <Step n="1" title="Price it" done={!!plan}>
        <form onSubmit={quote}>
          <div className="field">
            <label htmlFor="qty">Tokens</label>
            <input id="qty" className="mono" inputMode="decimal" value={tokens}
                   onChange={(e) => setTokens(e.target.value)}
                   placeholder={'min ' + amount(sale.terms.min_lot)} />
            <div className="hint">Your cap in this sale is {amount(tier.cap_atoms)} USDX.</div>
          </div>
          <button className="btn btn-primary btn-sm" disabled={busy}>
            {busy ? 'Working…' : 'Price this purchase'}
          </button>
        </form>
        {plan && (
          <div style={{ marginTop: '1rem' }}>
            <div className="kv"><span>You receive</span><b>{amount(plan.token_atoms)} {project.ticker}</b></div>
            {plan.quote && plan.quote.rail === 'btc' ? (
              <>
                <div className="kv"><span>You send</span><b>{plan.quote.send_btc} BTC</b></div>
                <div className="kv"><span>Rate</span><b>{(plan.quote.rate.payment_atoms_per_btc / 1e8).toLocaleString()} USDX/BTC</b></div>
              </>
            ) : (
              <div className="kv"><span>You pay</span><b>{amount(plan.payment_atoms)} USDX</b></div>
            )}
            <div className="kv"><span>Left resting after</span><b>{amount(plan.remainder_atoms)}</b></div>
            <div className="kv">
              <span>Your cap in this sale</span>
              <b>{amount(plan.cap.committed_atoms)} of {amount(plan.cap.per_sale_atoms)} used</b>
            </div>
            <p className="small dim" style={{ marginTop: '.75rem' }}>
              The cap is Levo's allocation policy. The covenant enforces the
              price, the treasury, the token and the minimum lot; it has no
              per-buyer maximum.
            </p>
            {plan.quote && plan.quote.steps && (
              <ol className="small dim" style={{ paddingLeft: '1.1rem', marginTop: '.75rem' }}>
                {plan.quote.steps.map((s, i) => <li key={i}>{s}</li>)}
              </ol>
            )}
          </div>
        )}
      </Step>

      {plan && (
        <Step n="2" title="Build the transaction" done={!!built}>
          <p className="small dim">
            Levo assembles it and signs nothing. The covenant input needs no
            signature; your inputs can only be signed by you.
          </p>
          {rail === 'btc' && (
            <div className="notice" style={{ marginBottom: '1rem' }}>
              <strong>Do step one first.</strong> The covenant is paid in USDX,
              so this transaction spends USDX, not BTC. Swap the amount quoted
              above over Lightning, then come back and spend the USDX it gave
              you.
            </div>
          )}
          <form onSubmit={build}>
            {hasProvider() && (
              <button type="button" className="btn btn-sm btn-ghost"
                      style={{ marginBottom: '.9rem' }}
                      onClick={autofill} disabled={busy}>
                Fill from my wallet
              </button>
            )}
            <div className="field">
              <label htmlFor="inp">Outputs you are spending</label>
              <textarea id="inp" className="mono" rows={3} value={funding.inputs}
                        onChange={(e) => setFunding({ ...funding, inputs: e.target.value })}
                        placeholder="txid:vout, one per line" />
              <div className="hint">
                They must be unblinded. A covenant reads the amounts it checks,
                so it cannot be filled with confidential outputs.
              </div>
            </div>
            <div className="field">
              <label htmlFor="tspk">Where your tokens go</label>
              <input id="tspk" className="mono" value={funding.token_spk}
                     onChange={(e) => setFunding({ ...funding, token_spk: e.target.value })}
                     placeholder="scriptPubKey, hex" />
            </div>
            <div className="field">
              <label htmlFor="cspk">Where your change goes</label>
              <input id="cspk" className="mono" value={funding.change_spk}
                     onChange={(e) => setFunding({ ...funding, change_spk: e.target.value })}
                     placeholder="scriptPubKey, hex" />
            </div>
            <div className="field">
              <label htmlFor="fee">Network fee, in atoms of the payment asset</label>
              <input id="fee" className="mono" value={funding.fee}
                     onChange={(e) => setFunding({ ...funding, fee: e.target.value })} />
            </div>
            <button className="btn btn-primary btn-sm" disabled={busy}>
              {busy ? 'Building…' : 'Build it'}
            </button>
          </form>
        </Step>
      )}

      {built && (
        <Step n="3" title="Sign it and send it" done={!!sentTxid}>
          <div className="field">
            <label>Unsigned transaction</label>
            <textarea className="mono" rows={4} readOnly value={built.unsigned_tx_hex}
                      onFocus={(e) => e.target.select()} />
            <div className="hint">
              Sign your own inputs, then broadcast. With a node:{' '}
              <span className="mono">sequentia-cli signrawtransactionwithwallet &lt;hex&gt;</span>
            </div>
          </div>
          <table className="terms" style={{ marginBottom: '1rem' }}>
            <tbody>
              {built.outputs.map((o) => (
                <tr key={o.index}>
                  <th>Output {o.index}</th>
                  <td>{o.role}<div className="dim small" style={{ fontFamily: 'var(--body)' }}>
                    {amount(o.atoms)} atoms</div></td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="field">
            <label htmlFor="sg">Signed transaction</label>
            <textarea id="sg" className="mono" rows={3} value={signed}
                      onChange={(e) => setSigned(e.target.value)}
                      placeholder="paste the signed hex" />
          </div>
          <button className="btn btn-primary btn-sm" onClick={send}
                  disabled={busy || !signed.trim()}>
            {busy ? 'Sending…' : 'Broadcast'}
          </button>
        </Step>
      )}

      {sentTxid && (
        <div className="notice good" style={{ marginTop: '1rem' }}>
          <strong>Sent.</strong> Your tokens and the project's payment moved in
          the same transaction.
          <div className="mono small" style={{ wordBreak: 'break-all', marginTop: '.4rem' }}>
            {sentTxid}
          </div>
        </div>
      )}

      {error && <div className="notice bad" style={{ marginTop: '1rem' }}>{error}</div>}
    </div>
  )
}
