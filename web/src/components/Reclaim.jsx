import { useState } from 'react'
import { api } from '../lib/api'
import { useStore } from '../lib/store'
import { amount, capitalise } from '../lib/format'
import { Copy, Notice } from './ui'

// After the close, whatever did not sell is the project's to sweep. The
// covenant will only allow it under the project's own reclaim key, so Levo
// builds the transaction and computes what has to be signed -- it cannot sign
// it, and neither can anyone else.

export default function Reclaim({ project }) {
  const { payment } = useStore()
  const sale = project.sale
  const decimals = project.decimals ?? 8
  const [form, setForm] = useState({ dest: '', inputs: '', fee: '100000' })
  const [built, setBuilt] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function build(e) {
    e.preventDefault()
    setError(null); setBusy(true)
    try {
      const fee_inputs = form.inputs.split(/\s+/).filter(Boolean).map((s) => {
        const [txid, vout] = s.split(':')
        if (!txid || vout === undefined || !/^\d+$/.test(vout)) throw new Error('Fee inputs look like txid:vout, one per line.')
        return { txid: txid.trim(), vout: Number(vout) }
      })
      if (!/^\d+$/.test(form.fee || '')) throw new Error('The fee is a whole number of ' + payment.label + ' atoms.')
      setBuilt(await api.reclaim(project.slug, {
        destination_address: form.dest.trim(),
        fee_inputs, fee_atoms: Number(form.fee),
      }))
    } catch (e) { setError(capitalise(e.message)) } finally { setBusy(false) }
  }

  return (
    <div className="card" style={{ marginTop: '1rem' }}>
      <h3>Reclaim what did not sell</h3>
      <p className="small dim">
        {amount(sale.locked_atoms, decimals)} {project.ticker} are still in the covenant.
        The reclaim path is open, and it needs your reclaim key. Until you sweep them, a
        buyer who builds the transaction can still fill the sale.
      </p>
      <form onSubmit={build}>
        <div className="field">
          <label htmlFor="rdest">Where the tokens go</label>
          <input id="rdest" className="mono" value={form.dest} required
                 onChange={(e) => setForm({ ...form, dest: e.target.value })}
                 placeholder="tb1… address" />
        </div>
        <div className="field">
          <label htmlFor="rin">Outputs to pay the fee from</label>
          <textarea id="rin" className="mono" rows={2} value={form.inputs}
                    onChange={(e) => setForm({ ...form, inputs: e.target.value })}
                    placeholder="txid:vout, one per line" />
          <div className="hint">
            The covenant holds only the sale token, so the fee comes from your own
            unblinded {payment.label} outputs.
          </div>
        </div>
        <div className="field">
          <label htmlFor="rfee">Fee, in {payment.label} atoms</label>
          <input id="rfee" className="mono" inputMode="numeric" value={form.fee} required
                 onChange={(e) => setForm({ ...form, fee: e.target.value })} />
        </div>
        <button className="btn btn-primary btn-sm" disabled={busy || !form.dest.trim()}>
          {busy ? 'Building…' : 'Build the reclaim'}
        </button>
      </form>
      {error && <Notice kind="bad" style={{ marginTop: '1rem' }}>{error}</Notice>}
      {built && (
        <div style={{ marginTop: '1rem' }}>
          <Notice style={{ marginBottom: '1rem' }}>
            The quickest way to finish: from your node, run{' '}
            <span className="mono">bin/levo reclaim {project.slug} --to {form.dest.trim()} --reclaim-key &lt;32-byte hex&gt;</span>.
            It signs the sighash below with your key, adds the witness, signs your fee
            inputs and broadcasts. Nothing below needs Levo.
          </Notice>
          <div className="field">
            <label htmlFor="rsh">Sighash to sign, BIP340, with key {built.signs_with.slice(0, 12)}… <Copy value={built.sighash} label="Copy the sighash" /></label>
            <div id="rsh" className="hex small">{built.sighash}</div>
          </div>
          <div className="field">
            <label htmlFor="rleaf">Reclaim leaf <Copy value={built.leaf} label="Copy the leaf" /></label>
            <div id="rleaf" className="hex small">{built.leaf}</div>
          </div>
          <div className="field">
            <label htmlFor="rcb">Control block <Copy value={built.control_block} label="Copy the control block" /></label>
            <div id="rcb" className="hex small">{built.control_block}</div>
          </div>
          <div className="field">
            <label htmlFor="runsigned">Unsigned transaction, locktime {built.locktime} <Copy value={built.unsigned_tx_hex} label="Copy the unsigned transaction" /></label>
            <textarea id="runsigned" className="mono" rows={3} readOnly
                      value={built.unsigned_tx_hex}
                      onFocus={(e) => e.target.select()} />
            <div className="hint">
              Put [signature, leaf, control block] in input 0's witness, sign your fee
              inputs with your wallet, and broadcast. Levo recognises the reclaim by its
              transaction id, {built.txid.slice(0, 12)}…, once it is on chain.
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
