import { useState } from 'react'
import { api } from '../lib/api'
import { useStore } from '../lib/store'
import { amount, capitalise, plain } from '../lib/format'
import { Copy, Hex, Notice } from './ui'

// Funding a sale is the project sending its tokens to the sale address and
// telling Levo where they landed. Levo can usually find them itself; the
// outpoint form is for a lock that is not confirmed yet.

export default function LockPanel({ project, onLocked }) {
  const { explorer } = useStore()
  const sale = project.sale
  const decimals = project.decimals ?? 8
  const ghost = sale.status === 'ghost'
  const [lock, setLock] = useState({ txid: '', vout: '0' })
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [manual, setManual] = useState(false)
  // The whole command, with nothing elided: a truncated asset id in a command
  // is a command that cannot be run.
  const lockCommand = 'sequentia-cli -named sendtoaddress' +
    ' address=' + project.address +
    ' amount=' + plain(sale.terms.total_atoms, decimals) +
    ' assetlabel=' + sale.terms.token_asset +
    ' fee_asset_label=<the asset you pay fees in>'

  async function confirm(e, named) {
    if (e) e.preventDefault()
    if (busy || (named && !lock.txid.trim())) return
    setError(null); setBusy(true)
    try {
      const r = named
        ? await api.lock(project.slug, lock.txid.trim(), Number(lock.vout))
        : await api.lock(project.slug)
      onLocked && onLocked(r)
    } catch (err) {
      setError(capitalise(err.message))
    } finally { setBusy(false) }
  }

  return (
    <div className="card">
      <h3>{ghost ? 'Lock the tokens again' : 'Lock the tokens'}</h3>
      {ghost ? (
        <p className="small dim">
          The chain does not have the output that funded this sale: either it never
          reached a block, or a Bitcoin-driven reorg took the block that held it.
          Either way the tokens are yours again — check your wallet, and if the old
          transaction is still sitting there unconfirmed, abandon it first. Then send
          them to the same sale address and confirm the new lock.
        </p>
      ) : (
        <p className="small dim">
          Send exactly {amount(sale.terms.total_atoms, decimals)} {project.ticker} to the
          address below, in one output, from a wallet you control. Until that output
          exists and matches your terms, the sale stays a draft and nobody can buy.
        </p>
      )}
      <div className="field">
        <div className="field-title" id="lockaddr-title">Sale address</div>
        <div className="hex small" aria-labelledby="lockaddr-title">
          <Hex value={project.address} href={explorer('address', project.address)} label="sale address" />
        </div>
      </div>
      <div className="kv"><span>Amount</span><b>{amount(sale.terms.total_atoms, decimals)} {project.ticker}</b></div>
      <div className="kv"><span>Asset</span><b>{sale.terms.token_asset.slice(0, 12)}… <Copy value={sale.terms.token_asset} label="Copy the asset id" /></b></div>
      <p className="small dim" style={{ marginTop: '.75rem' }}>
        Send this once, and nothing else, ever. The sell leaf reads the amount it
        spends and never which output that is, so every output at this address is
        buyable at the sale's price &mdash; a second lot of your own token included,
        out of a supply the board does not count. The address is unblinded, so the output will be explicit; do not
        wrap it in a confidential address, because tokens locked into a confidential
        output can never be sold.
      </p>
      <details className="small dim" style={{ marginBottom: '1rem' }}>
        <summary style={{ cursor: 'pointer' }}>Send it from a node instead</summary>
        <p style={{ marginTop: '.6rem' }}>
          <span className="mono">levo lock {project.slug}</span> sends the tokens and
          confirms the lock in one step, and it funds the send so that your change
          stays explicit. Use it if you can: an ordinary{' '}
          <span className="mono">sendtoaddress</span> blinds the change of every
          asset it touches, and a covenant cannot read a confidential output &mdash;
          so afterwards your own reclaim, and any purchase you make from your own
          sale, will say it cannot find unblinded funds. To do it by hand anyway,
          the command is below; it names the asset by its id, because a label only
          exists on a node that was told about it.
        </p>
        <div className="field" style={{ marginBottom: 0 }}>
          <label htmlFor="lockcmd">
            Command
          </label>
          <Copy value={lockCommand} label="Copy the lock command" />
          <textarea id="lockcmd" className="mono" rows={4} readOnly value={lockCommand}
                    onFocus={(e) => e.target.select()} />
          <div className="hint">
            Replace the fee asset with one your node accepts fees in, and use the
            wallet that holds the tokens. Your change comes back confidential: to
            get spendable-by-a-covenant outputs again, fund the send yourself
            instead &mdash; <span className="mono">createrawtransaction</span>, then{' '}
            <span className="mono">fundrawtransaction</span> with a{' '}
            <span className="mono">changeAddress</span> per asset, then sign and
            send. That is what <span className="mono">levo lock</span> does.
          </div>
        </div>
      </details>
      {error && (
        <Notice kind="bad" style={{ marginBottom: '1rem' }}>
          {error}
          {/^an output of/i.test(error) && (
            <div className="small" style={{ marginTop: '.5rem' }}>
              The sale wants the whole allocation in one output. Send the
              difference to the same address and the two will not merge, so the
              way out is to send the full amount again in a single output and
              confirm that one. Whatever is left over at the address is yours to
              take back with your reclaim key after the close.
            </div>
          )}
        </Notice>
      )}
      <div className="btn-row">
        <button className="btn btn-primary" onClick={() => confirm(null, false)} aria-disabled={busy}>
          {busy ? 'Looking on chain…' : 'Look for my tokens on chain'}
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => setManual((m) => !m)}>
          {manual ? 'Hide' : 'Name the outpoint instead'}
        </button>
      </div>
      {manual && (
        <form onSubmit={(e) => confirm(e, true)} style={{ marginTop: '1rem' }}>
          <p className="small dim">
            The scan sees confirmed outputs only. A lock that is still in the mempool
            can be confirmed by its transaction id and output index.
          </p>
          <div className="field">
            <label htmlFor="txid">Funding transaction id</label>
            <input id="txid" className="mono" value={lock.txid} required
                   onChange={(e) => setLock({ ...lock, txid: e.target.value })} />
          </div>
          <div className="field">
            <label htmlFor="vout">Output index</label>
            <input id="vout" className="mono" inputMode="numeric" value={lock.vout} required
                   onChange={(e) => setLock({ ...lock, vout: e.target.value })} />
          </div>
          <button className="btn btn-primary btn-sm" aria-disabled={busy || !lock.txid.trim()}>
            {busy ? 'Checking the chain…' : 'Confirm the lock'}
          </button>
        </form>
      )}
    </div>
  )
}
