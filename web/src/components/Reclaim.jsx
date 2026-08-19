import { useState } from 'react'
import { api } from '../lib/api'
import { amount } from '../lib/format'

// After the close, whatever did not sell is the project's to sweep. The
// covenant will only allow it under the project's own reclaim key, so Levo
// builds the transaction and computes what has to be signed -- it cannot sign
// it, and neither can anyone else.

export default function Reclaim({ project }) {
  const sale = project.sale
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
        return { txid: txid.trim(), vout: Number(vout) }
      })
      setBuilt(await api.reclaim(project.slug, {
        destination_script_pubkey: form.dest.trim(),
        fee_inputs, fee_atoms: Number(form.fee || 0),
      }))
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  return (
    <div className="card" style={{ marginTop: '1rem' }}>
      <h3>Reclaim what did not sell</h3>
      <p className="small dim">
        {amount(sale.locked_atoms)} {project.ticker} are still in the covenant.
        The reclaim path opens after the close and needs your reclaim key.
      </p>
      <form onSubmit={build}>
        <div className="field">
          <label htmlFor="rdest">Where the tokens go</label>
          <input id="rdest" className="mono" value={form.dest}
                 onChange={(e) => setForm({ ...form, dest: e.target.value })}
                 placeholder="scriptPubKey, hex" />
        </div>
        <div className="field">
          <label htmlFor="rin">Outputs to pay the fee from</label>
          <textarea id="rin" className="mono" rows={2} value={form.inputs}
                    onChange={(e) => setForm({ ...form, inputs: e.target.value })}
                    placeholder="txid:vout, one per line" />
          <div className="hint">
            The covenant holds only the sale token, so the fee comes from
            elsewhere. These must be unblinded.
          </div>
        </div>
        <div className="field">
          <label htmlFor="rfee">Fee, in atoms</label>
          <input id="rfee" className="mono" value={form.fee}
                 onChange={(e) => setForm({ ...form, fee: e.target.value })} />
        </div>
        <button className="btn btn-primary btn-sm" disabled={busy}>
          {busy ? 'Building…' : 'Build the reclaim'}
        </button>
      </form>
      {error && <div className="notice bad" style={{ marginTop: '1rem' }}>{error}</div>}
      {built && (
        <div style={{ marginTop: '1rem' }}>
          <div className="field">
            <label>Sign this with your reclaim key</label>
            <input className="mono" readOnly value={built.sighash}
                   onFocus={(e) => e.target.select()} />
            <div className="hint">
              BIP340, under {built.signs_with.slice(0, 16)}… Then put
              [signature, leaf, control block] in input 0, sign your fee inputs,
              and broadcast.
            </div>
          </div>
          <div className="field">
            <label>Unsigned transaction</label>
            <textarea className="mono" rows={3} readOnly
                      value={built.unsigned_tx_hex}
                      onFocus={(e) => e.target.select()} />
          </div>
        </div>
      )}
    </div>
  )
}
