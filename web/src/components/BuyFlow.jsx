import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { hasProvider, getUtxos, getAddress, broadcastHex, signPset, broadcastPset,
         supportsPset, friendly } from '../lib/wallet'
import { useStore } from '../lib/store'
import { amount, atomsArg, big, capitalise, shortHex, timeLabel, toAtoms, treasurySpk } from '../lib/format'
import { Copy, Hex, Notice } from './ui'
import SignIn from './SignIn'

// What the buyer typed survives a session that ends mid-purchase, and a
// reload. Nothing kept here is secret: an amount, two of the buyer's own
// addresses, and the outpoints they mean to spend.
function draftKey(slug) { return 'levo.buy.' + slug }

function readDraft(slug) {
  try {
    const raw = sessionStorage.getItem(draftKey(slug))
    return raw ? JSON.parse(raw) : null
  } catch { return null }
}

function writeDraft(slug, value) {
  try { sessionStorage.setItem(draftKey(slug), JSON.stringify(value)) } catch { /* private mode */ }
}

function clearDraft(slug) {
  try { sessionStorage.removeItem(draftKey(slug)) } catch { /* private mode */ }
}

// What a built transaction has to agree with before it is worth signing. It
// catches a plan that moved under the buyer -- another purchase landing
// between pricing and building -- and any disagreement between what this page
// showed and what it is about to hand to a wallet.
function disagreements(built, plan, sale, project) {
  const out = []
  const d = project.decimals ?? 8
  const treasury = treasurySpk(sale.terms)
  const outputs = built.outputs || []
  const pay = outputs[0]
  if (!pay || pay.script_pubkey !== treasury) {
    out.push('the first output does not pay the treasury named in the terms')
  } else if (big(pay.atoms) !== big(plan.payment_atoms)) {
    out.push('the treasury would be paid an amount the quote did not say')
  }
  const mine = outputs.filter((x) => x.role === 'your tokens')
    .reduce((n, x) => n + big(x.atoms), 0n)
  if (mine !== big(built.token_atoms) || big(built.token_atoms) !== big(plan.token_atoms)) {
    out.push('the tokens it sends you are not the ' + amount(plan.token_atoms, d) + ' ' +
             project.ticker + ' that was priced')
  }
  return out
}

// Buying happens in three moves, and the interface shows all three because the
// buyer is the one who signs.
//
//   1. Price it.       Levo works out what the covenant will demand.
//   2. Build it.       Levo assembles the transaction. It signs nothing: the
//                      covenant input needs no signature, and the buyer's own
//                      inputs can only be signed by the buyer.
//   3. Sign and send.  Whatever holds the buyer's keys does this: the browser
//                      wallet signs the PSET in place, a node signs the hex.

function Step({ n, title, children, done }) {
  return (
    <div className="step">
      <div className={'step-n' + (done ? ' done' : '')} aria-label={'step ' + n + (done ? ', done' : '')}>
        {done ? '✓' : n}
      </div>
      <div className="step-body">
        <h4>{title}</h4>
        {children}
      </div>
    </div>
  )
}

export default function BuyFlow({ project, tier, onSettled }) {
  const { payment, explorer, refresh, config } = useStore()
  const hrp = config.hrp || 'tb'
  const blinded = hrp === 'bc' ? 'sqb' : hrp === 'tb' ? 'tsqb' : hrp + 'b'
  const sale = project.sale
  const decimals = project.decimals ?? 8
  const label = payment.label
  const [rails, setRails] = useState(null)
  const [rail, setRail] = useState('usdx')
  const [tokens, setTokens] = useState(() => (readDraft(project.slug) || {}).tokens || '')
  const [plan, setPlan] = useState(null)
  const [built, setBuilt] = useState(null)
  const [signed, setSigned] = useState('')
  const [sentTxid, setSentTxid] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [needSignIn, setNeedSignIn] = useState(false)
  const [walletCanSign, setWalletCanSign] = useState(false)
  const [funding, setFunding] = useState(() => (readDraft(project.slug) || {}).funding
    || { token_addr: '', change_addr: '', inputs: '', fee: '' })
  const [now, setNow] = useState(Date.now() / 1000)
  const [mismatch, setMismatch] = useState(null)
  const [note, setNote] = useState(null)

  useEffect(() => { api.rails().then((r) => setRails(r.rails)).catch(() => {}) }, [])
  useEffect(() => { if (hasProvider()) supportsPset().then(setWalletCanSign) }, [])
  // What the buyer typed outlives a session that ends mid-purchase, and a
  // reload: signing in again costs a signature, not the whole form.
  useEffect(() => { writeDraft(project.slug, { tokens, funding }) }, [project.slug, tokens, funding])
  useEffect(() => {
    if (!plan || !plan.quote || !plan.quote.expires_at) return undefined
    const t = setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => clearInterval(t)
  }, [plan])

  function fail(e) {
    if (e && e.status === 401) { setNeedSignIn(true); setError(null); return }
    setError(capitalise(friendly(e)))
  }

  function reset() {
    setPlan(null); setBuilt(null); setSigned(''); setSentTxid(null); setError(null)
  }

  // Re-price without throwing away what has been built or typed: the BTC rate
  // goes stale while the buyer is off doing the swap it told them to do.
  async function requote() {
    if (busy) return
    setError(null); setBusy(true)
    try {
      const atoms = toAtoms(tokens, decimals)
      if (atoms === null || atoms <= 0n) return
      const p = await api.buy(project.slug, { token_atoms: atomsArg(atoms), rail })
      setPlan(p)
      setNow(Date.now() / 1000)
    } catch (e) { fail(e) } finally { setBusy(false) }
  }

  async function quote(e) {
    e.preventDefault()
    if (busy) return
    reset(); setBusy(true)
    try {
      const atoms = toAtoms(tokens, decimals)
      if (atoms === null || atoms <= 0n) throw new Error('Enter how many ' + project.ticker + ' you want, with at most ' + decimals + ' decimal places.')
      const p = await api.buy(project.slug, { token_atoms: atomsArg(atoms), rail })
      setPlan(p)
      setNow(Date.now() / 1000)
      if (!funding.fee && p.fee && p.fee.suggested_atoms) {
        setFunding((f) => ({ ...f, fee: amount(p.fee.suggested_atoms, payment.decimals) }))
      }
    } catch (e) { fail(e) } finally { setBusy(false) }
  }

  async function autofill() {
    if (busy) return
    setError(null); setBusy(true)
    try {
      const need = big(plan.payment_atoms) + (toAtoms(funding.fee, payment.decimals) || 0n)
      const [list, addr] = await Promise.all([getUtxos({}), getAddress({})])
      const right = list.filter((x) => (x.asset || '').toLowerCase() === sale.terms.payment_asset)
      if (!right.length) {
        throw new Error('Your wallet holds no ' + label + ' outputs. A purchase spends ' + label + '.')
      }
      // Which of them a covenant can actually spend is the node's answer, not
      // the wallet's: a confidential output commits to its value instead of
      // stating one, and the sell leaf reads the value it spends. The wallet's
      // own record does not distinguish them, so guessing here is how a buyer
      // gets told their ordinary funds are confidential.
      const checked = await api.checkOutputs(right.slice(0, 32).map((x) => ({ txid: x.txid, vout: x.vout })))
      const ok = new Map(checked.outputs.filter((x) => x.spendable).map((x) => [x.txid + ':' + x.vout, x]))
      const usable = right.filter((x) => ok.has(x.txid + ':' + x.vout))
      const hidden = right.length - usable.length
      if (!usable.length) {
        const why = (checked.outputs.find((x) => x.why) || {}).why
        throw new Error('None of your ' + label + ' outputs can fund a covenant purchase' +
          (why ? ': ' + why + '.' : '.') +
          ' Send the balance to one of your own ' + hrp + '1… addresses first, then come back.')
      }
      // Largest first, until the purchase and the fee are covered.
      usable.sort((a, b) => (big(b.value) > big(a.value) ? 1 : big(b.value) < big(a.value) ? -1 : 0))
      const picked = []
      let total = 0n
      for (const u of usable) {
        picked.push(u.txid + ':' + u.vout)
        total += big(u.value)
        if (total >= need) break
      }
      if (total < need) {
        throw new Error('Your wallet holds ' + amount(total, payment.decimals) + ' ' + label +
          ' in outputs a covenant can spend, and this purchase needs ' + amount(need, payment.decimals) + '.')
      }
      setFunding((f) => ({
        ...f,
        inputs: picked.join('\n'),
        token_addr: f.token_addr || addr,
        change_addr: f.change_addr || addr,
      }))
      setNote(hidden
        ? hidden + ' of your ' + label + ' ' + (hidden === 1 ? 'output was' : 'outputs were') +
          ' left out: the node says a covenant cannot spend ' + (hidden === 1 ? 'it' : 'them') + '.'
        : null)
    } catch (e) { fail(e) } finally { setBusy(false) }
  }

  async function build(e) {
    e.preventDefault()
    if (busy) return
    setError(null); setBuilt(null); setSigned(''); setSentTxid(null); setBusy(true)
    try {
      const inputs = funding.inputs.split(/\s+/).filter(Boolean).map((s) => {
        const [txid, vout] = s.split(':')
        if (!txid || vout === undefined || !/^\d+$/.test(vout)) throw new Error('Inputs look like txid:vout, one per line.')
        return { txid: txid.trim(), vout: Number(vout) }
      })
      const feeAtoms = toAtoms(funding.fee, payment.decimals)
      if (feeAtoms === null || feeAtoms <= 0n) {
        throw new Error('Enter the fee in ' + label + ', with at most ' + payment.decimals + ' decimal places.')
      }
      const b = await api.transaction(project.slug, {
        token_atoms: atomsArg(plan.token_atoms),
        buyer: {
          token_address: funding.token_addr.trim(),
          change_address: funding.change_addr.trim() || undefined,
          inputs,
          fee_atoms: Number(feeAtoms),
        },
      })
      const wrong = disagreements(b, plan, sale, project)
      setMismatch(wrong.length ? wrong : null)
      setBuilt(b)
    } catch (e) { fail(e) } finally { setBusy(false) }
  }

  async function record(txid) {
    setSentTxid(txid)
    try {
      await api.confirm(project.slug, {
        txid,
        token_atoms: atomsArg(built.token_atoms),
        payment_atoms: atomsArg(built.payment_atoms),
      })
    } catch (e) {
      // The purchase is on chain either way; the record is bookkeeping.
      setError('The purchase was broadcast, but recording it against your cap did not work: ' + e.message)
    }
    clearDraft(project.slug)
    await refresh()
    onSettled && onSettled()
  }

  async function signAndSendWithWallet() {
    if (busy) return
    setError(null); setBusy(true)
    try {
      const signedPset = await signPset(built.pset)
      const txid = await broadcastPset(signedPset)
      await record(typeof txid === 'string' ? txid : built.txid)
    } catch (e) { fail(e) } finally { setBusy(false) }
  }

  async function sendHex() {
    if (busy || !signed.trim()) return
    setError(null); setBusy(true)
    try {
      const txid = await broadcastHex(signed.trim())
      await record(typeof txid === 'string' ? txid : built.txid)
    } catch (e) { fail(e) } finally { setBusy(false) }
  }

  async function recordOwnBroadcast() {
    setError(null); setBusy(true)
    try { await record(built.txid) } catch (e) { fail(e) } finally { setBusy(false) }
  }

  const btcRail = rails && rails.find((r) => r.id === 'btc')
  const expired = plan && plan.quote && plan.quote.expires_at && now > plan.quote.expires_at
  const remaining = plan && plan.cap ? big(plan.cap.per_sale_atoms) - big(plan.cap.committed_atoms) : null

  if (needSignIn) {
    return (
      <div>
        <Notice kind="bad" style={{ marginBottom: '1rem' }}>
          Your session ended. Sign in again to continue; what you typed is kept.
        </Notice>
        <SignIn onDone={() => setNeedSignIn(false)} />
      </div>
    )
  }

  return (
    <div>
      {rails && (
        <div className="rail-pick" role="radiogroup" aria-label="Pay with">
          {rails.map((r, i) => (
            <button key={r.id} type="button" role="radio" aria-checked={rail === r.id}
                    className={'rail' + (rail === r.id ? ' on' : '')}
                    disabled={!r.available}
                    tabIndex={rail === r.id ? 0 : -1}
                    onKeyDown={(e) => {
                      const step = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1
                        : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0
                      if (!step) return
                      e.preventDefault()
                      const usable = rails.filter((x) => x.available)
                      if (usable.length < 2) return
                      const at = Math.max(0, usable.findIndex((x) => x.id === rail))
                      const next = usable[(at + step + usable.length) % usable.length]
                      setRail(next.id); reset()
                      const el = e.currentTarget.parentNode.querySelector('[data-rail="' + next.id + '"]')
                      if (el) el.focus()
                    }}
                    data-rail={r.id}
                    onClick={() => { setRail(r.id); reset() }}>
              <b>{r.label}</b>
              <span>{r.steps === 1 ? 'settles in the covenant' : 'two steps, via Lightning'}</span>
            </button>
          ))}
        </div>
      )}
      {rail === 'btc' && btcRail && !btcRail.available && (
        <Notice kind="bad" style={{ marginBottom: '1rem' }}>{btcRail.unavailable_because}</Notice>
      )}

      <Step n="1" title="Price it" done={!!plan}>
        <form onSubmit={quote}>
          <div className="field">
            <label htmlFor="qty">{project.ticker} to buy</label>
            <input id="qty" className="mono" inputMode="decimal" value={tokens} required
                   aria-describedby="qty-hint"
                   onChange={(e) => setTokens(e.target.value)}
                   placeholder={amount(sale.terms.min_lot, decimals)} />
            <div className="hint" id="qty-hint">
              Minimum {amount(sale.terms.min_lot, decimals)} {project.ticker}. Your cap in this sale
              is {amount(tier.cap_atoms, payment.decimals)} {label}
              {remaining !== null ? ', of which ' + amount(remaining, payment.decimals) + ' is still open' : ''}.
            </div>
          </div>
          <button className="btn btn-primary btn-sm" aria-disabled={busy}>
            {busy && !plan ? 'Working…' : 'Price this purchase'}
          </button>
        </form>
        {plan && (
          <div style={{ marginTop: '1rem' }}>
            <div className="kv"><span>You receive</span><b>{amount(plan.token_atoms, decimals)} {project.ticker}</b></div>
            {plan.quote && plan.quote.rail === 'btc' ? (
              <>
                <div className="kv"><span>You swap</span><b>{amount(plan.quote.send_sats, 8)} BTC</b></div>
                <div className="kv"><span>For</span><b>{amount(plan.payment_atoms, payment.decimals)} {label}</b></div>
                <div className="kv"><span>Rate</span><b>{amount(plan.quote.rate.payment_atoms_per_btc, payment.decimals, 2)} {label}/BTC</b></div>
                <div className="kv"><span>Quote valid until</span>
                  <b>{expired ? 'stale; refresh it below' : timeLabel(plan.quote.expires_at)}</b></div>
              </>
            ) : (
              <div className="kv"><span>You pay</span><b>{amount(plan.payment_atoms, payment.decimals)} {label}</b></div>
            )}
            <div className="kv"><span>Left in the sale after this buy</span><b>{amount(plan.remainder_atoms, decimals)} {project.ticker}</b></div>
            <div className="kv">
              <span>Your cap in this sale</span>
              <b>{amount(plan.cap.committed_atoms, payment.decimals)} of {amount(plan.cap.per_sale_atoms, payment.decimals)} {label} used</b>
            </div>
            <p className="small dim" style={{ marginTop: '.75rem' }}>
              The cap is Levo's allocation policy. The covenant enforces the
              price, the treasury, the token and the minimum lot; it has no
              per-buyer maximum.
            </p>
            {plan.quote && plan.quote.rail === 'btc' && (
              <>
                <ol className="small dim" style={{ paddingLeft: '1.1rem', marginTop: '.75rem' }}>
                  {plan.quote.steps.map((s, i) => <li key={i}>{capitalise(s)}</li>)}
                </ol>
                <p className="small dim">{plan.quote.note}</p>
                <p className="small dim">
                  The rate is what a swap would have cost when this quote was made,
                  and the swap happens in your own wallet, not here. Levo neither
                  holds the BTC nor guarantees the rate: what the covenant checks is
                  the {label} that reaches the treasury.
                </p>
              </>
            )}
          </div>
        )}
      </Step>

      {plan && (
        <Step n="2" title="Build the transaction" done={!!built}>
          {expired && (
            <Notice style={{ marginBottom: '1rem' }}>
              <strong>The BTC rate above is stale.</strong> What you pay the
              covenant has not changed -- it is fixed by the sale's own price --
              only the sats a swap would cost. If you have already swapped, carry
              on and spend the {label} you received.
              <div style={{ marginTop: '.5rem' }}>
                <button type="button" className="btn btn-sm btn-ghost"
                        onClick={requote} aria-disabled={busy}>
                  Refresh the BTC rate
                </button>
              </div>
            </Notice>
          )}
          <p className="small dim">
            Levo assembles it and signs nothing. The covenant input needs no
            signature; your inputs can only be signed by you.
          </p>
          {rail === 'btc' && (
            <Notice style={{ marginBottom: '1rem' }}>
              <strong>Swap first.</strong> The covenant is paid in {label}, so this
              transaction spends {label}, not BTC. Swap {amount(plan.quote.send_sats, 8)} BTC
              for {amount(plan.payment_atoms, payment.decimals)} {label} over Lightning in your
              wallet's own swap, then come back and spend the {label} it gave you.
            </Notice>
          )}
          <form onSubmit={build}>
            {hasProvider() && (
              <button type="button" className="btn btn-sm btn-ghost"
                      style={{ marginBottom: '.9rem' }}
                      onClick={autofill} aria-disabled={busy}>
                {busy ? 'Asking your wallet…' : 'Fill from my wallet'}
              </button>
            )}
            <div className="field">
              <label htmlFor="inp">Outputs you are spending</label>
              <textarea id="inp" className="mono" rows={3} value={funding.inputs} required
                        aria-describedby="inp-hint"
                        onChange={(e) => setFunding({ ...funding, inputs: e.target.value })}
                        placeholder="txid:vout, one per line" />
              <div className="hint" id="inp-hint">
                {label} outputs, unblinded: anything you received at a {hrp}1… address is;
                anything received at a confidential {blinded}1… address is not, and a
                covenant cannot read it. Send those to a {hrp}1… address first.
              </div>
            </div>
            <div className="field">
              <label htmlFor="taddr">Where your tokens go</label>
              <input id="taddr" className="mono" value={funding.token_addr} required
                     onChange={(e) => setFunding({ ...funding, token_addr: e.target.value })}
                     placeholder={hrp + '1… address'} />
            </div>
            <div className="field">
              <label htmlFor="caddr">Where your change goes</label>
              <input id="caddr" className="mono" value={funding.change_addr}
                     onChange={(e) => setFunding({ ...funding, change_addr: e.target.value })}
                     placeholder={hrp + '1… address (defaults to where your tokens go)'} />
            </div>
            <div className="field">
              <label htmlFor="fee">Network fee, in {label}</label>
              <input id="fee" className="mono" inputMode="decimal" value={funding.fee} required
                     aria-describedby="fee-hint"
                     onChange={(e) => setFunding({ ...funding, fee: e.target.value })}
                     placeholder={plan.fee && plan.fee.suggested_atoms
                       ? amount(plan.fee.suggested_atoms, payment.decimals) : ''} />
              <div className="hint" id="fee-hint">
                {plan.fee && plan.fee.min_atoms ? (
                  <>
                    This node relays a transaction of about {plan.fee.vsize_estimate} vB for{' '}
                    {amount(plan.fee.min_atoms, payment.decimals)} {label} or more.
                    Suggested: {amount(plan.fee.suggested_atoms, payment.decimals)} {label}, which
                    leaves room for a busy pool. Fees here are payable in any asset the
                    node accepts, and this one is paid in {label}.
                  </>
                ) : (
                  <>The node did not quote a rate just now. Ask your own node for its
                     relay floor rather than guessing.</>
                )}
              </div>
            </div>
            {note && <Notice style={{ margin: '0 0 1rem' }}>{note}</Notice>}
            <button className="btn btn-primary btn-sm" aria-disabled={busy}>
              {busy && !built ? 'Building…' : 'Build it'}
            </button>
          </form>
        </Step>
      )}

      {built && (
        <Step n="3" title="Sign it and send it" done={!!sentTxid}>
          <table className="outputs">
            <thead>
              <tr><th>Output</th><th>What</th><th className="num">Amount</th></tr>
            </thead>
            <tbody>
              {built.outputs.map((o) => (
                <tr key={o.index}>
                  <th>{o.index}</th>
                  <td>{o.role}</td>
                  <td className="num">
                    {amount(o.atoms, o.asset === sale.terms.token_asset ? decimals : payment.decimals)}{' '}
                    {o.asset === sale.terms.token_asset ? project.ticker
                      : o.asset === sale.terms.payment_asset ? label : shortHex(o.asset, 6, 4)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="small dim">
            Check the treasury credit and your tokens before you sign. Estimated
            size {built.vsize_estimate} vB.
          </p>
          {mismatch && (
            <Notice kind="bad" style={{ marginBottom: '1rem' }}>
              <strong>This does not match what was priced.</strong> Price it again
              before signing anything.
              <ul className="small" style={{ margin: '.4rem 0 0', paddingLeft: '1.1rem' }}>
                {mismatch.map((m, i) => <li key={i}>{capitalise(m)}</li>)}
              </ul>
            </Notice>
          )}
          {!sentTxid && !mismatch && walletCanSign && built.pset && (
            <div style={{ marginBottom: '1rem' }}>
              <button className="btn btn-primary btn-sm" onClick={signAndSendWithWallet}
                      aria-disabled={busy}>
                {busy ? 'Waiting for your wallet…' : 'Sign and broadcast with my wallet'}
              </button>
              <p className="small dim" style={{ margin: '.6rem 0 0' }}>
                Your wallet signs only your own inputs. The covenant's witness is already in place.
              </p>
            </div>
          )}
          {!sentTxid && hasProvider() && !walletCanSign && (
            <Notice style={{ marginBottom: '1rem' }}>
              Your wallet did not say it can sign a transaction this site built,
              so this purchase is signed with a node instead.
            </Notice>
          )}
          {!sentTxid && (
            <details open={!walletCanSign}>
              <summary className="small dim" style={{ cursor: 'pointer', marginBottom: '.75rem' }}>
                {walletCanSign ? 'Or sign it with a node' : 'Sign it with a node'}
              </summary>
              <div className="field">
                <label htmlFor="unsigned">Unsigned transaction</label>
                <Copy value={built.unsigned_tx_hex} label="Copy the unsigned transaction" />
                <textarea id="unsigned" className="mono" rows={4} readOnly value={built.unsigned_tx_hex}
                          onFocus={(e) => e.target.select()} />
                <div className="hint">
                  Your node's wallet must hold the outputs listed above. Run{' '}
                  <span className="mono">sequentia-cli signrawtransactionwithwallet &lt;hex&gt;</span>
                  {hasProvider() ? (
                    <>, paste the signed hex below, and broadcast from here.</>
                  ) : (
                    <>, then <span className="mono">sequentia-cli sendrawtransaction &lt;signed hex&gt;</span>{' '}
                      and tell Levo below.</>
                  )}
                  {' '}Or let{' '}
                  <span className="mono">levo buy {project.slug} --tokens {amount(built.token_atoms, decimals)}</span>{' '}
                  do the whole purchase from your node.
                </div>
              </div>
              {hasProvider() ? (
                <>
                  <div className="field">
                    <label htmlFor="sg">Signed transaction</label>
                    <textarea id="sg" className="mono" rows={3} value={signed}
                              onChange={(e) => setSigned(e.target.value)}
                              placeholder="paste the signed hex" />
                  </div>
                  <button className="btn btn-primary btn-sm" onClick={sendHex}
                          aria-disabled={busy || !signed.trim()}>
                    {busy ? 'Sending…' : 'Broadcast'}
                  </button>
                </>
              ) : (
                <div className="btn-row">
                  <button className="btn btn-primary btn-sm" onClick={recordOwnBroadcast} aria-disabled={busy}>
                    I broadcast it from my node
                  </button>
                  <span className="small dim">Records the purchase against your cap.</span>
                </div>
              )}
            </details>
          )}
        </Step>
      )}

      {sentTxid && (
        <Notice kind="good" style={{ marginTop: '1rem' }}>
          <strong>Broadcast.</strong> Your tokens and the project's payment are in
          one transaction; the purchase settles when it confirms.
          <div style={{ marginTop: '.4rem' }}>
            <Hex value={sentTxid} href={explorer('tx', sentTxid)} />
          </div>
        </Notice>
      )}

      {error && <Notice kind="bad" style={{ marginTop: '1rem' }}>{error}</Notice>}
      <p aria-live="polite" className="visually-hidden">
        {busy ? 'Working.' : sentTxid ? 'Broadcast.' : built ? 'Transaction ready to sign.'
          : plan ? 'Priced.' : ''}
      </p>
    </div>
  )
}
