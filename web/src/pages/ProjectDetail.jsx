import { useEffect, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { api } from '../lib/api'
import { useStore } from '../lib/store'
import { amount, capitalise, closeIn, closeLabel, compact, priceLabel, shortHex, treasurySpk } from '../lib/format'
import { addressOf } from '../lib/bech32'
import { Hex, Notice, usePageTitle } from '../components/ui'
import SignIn from '../components/SignIn'
import Beam from '../components/Beam'
import BuyFlow from '../components/BuyFlow'
import Reclaim from '../components/Reclaim'
import LockPanel from '../components/LockPanel'
import { Status } from './Projects'

function Terms({ project, sale }) {
  const { payment, config, explorer } = useStore()
  const t = sale.terms
  const d = project.decimals ?? 8
  const treasuryAddr = addressOf(treasurySpk(t), config.hrp)
  return (
    <table className="terms">
      <tbody>
        <tr><th>Token</th><td>{project.ticker} <span className="dim">·</span> <Hex value={t.token_asset} href={explorer('asset', t.token_asset)} /></td></tr>
        <tr><th>Paid in</th><td>{payment.label} <span className="dim">·</span> <Hex value={t.payment_asset} href={explorer('asset', t.payment_asset)} /></td></tr>
        <tr>
          <th>Price</th>
          <td>{priceLabel(t, d)} {payment.label} per {project.ticker}
            <div className="dim small prose">
              exactly ceil(n × {t.price_num} / {t.price_den}) {payment.label} atoms for n token atoms
            </div>
          </td>
        </tr>
        <tr><th>Minimum purchase</th><td>{amount(t.min_lot, d)} {project.ticker}</td></tr>
        <tr><th>For sale</th>
          <td>{amount(t.total_atoms, d)} {project.ticker}
            <div className="dim small prose">
              checked against the chain when the lock was accepted; not part of
              the address
            </div>
          </td></tr>
        <tr><th>Reclaim opens</th><td>{closeLabel(t.close_locktime)}</td></tr>
        <tr><th>Treasury address</th><td><Hex value={treasuryAddr} href={explorer('address', treasuryAddr)} />
          <div className="dim small prose">
            {Number(t.treasury_ver ?? 1) === 1 ? 'taproot' : 'version-0'} witness program{' '}
            {shortHex(t.treasury_prog, 10, 6)}
          </div></td></tr>
        <tr><th>Registered as</th>
          <td>
            {!project.registry || !project.registry.checked
              ? <>Levo did not check this token against a registry.</>
              : project.registry.found
                ? <>{project.registry.name} ({project.registry.ticker}),
                    {project.registry.precision !== null && project.registry.precision !== undefined
                      ? <> {project.registry.precision} decimal places,</> : null}
                    {project.registry.domain ? <> registered to {project.registry.domain}</> : null}.</>
                : <>This token is not in the registry Levo reads.</>}
            <div className="dim small prose">
              {project.registry && project.registry.checked && project.registry.found
                ? <>a listing may not contradict a registered contract: the ticker
                    and the decimals above are the ones the asset itself carries,
                    which is what a wallet will show.</>
                : <>an unregistered token is an ordinary token, and the ticker and
                    decimals above are what the project typed. A registry entry is
                    a name bound to the asset id; without one, check the id.</>}
            </div>
          </td></tr>
        <tr><th>Supply</th>
          <td>
            {project.issuance_txid
              ? <>The project names{' '}
                  <Hex value={project.issuance_txid}
                       href={explorer('tx', project.issuance_txid)} /> as the
                  issuance. Levo does not check it.</>
              : <>The project has not named its issuance transaction.</>}
            <div className="dim small prose">
              a Sequentia asset can carry a reissuance token, and whoever holds
              that can create more of it. Levo cannot see whether one exists or
              who holds it, and nothing in the covenant limits the supply —
              only how much of it this sale holds. Look the asset up before you
              buy{explorer('asset', t.token_asset) ? <>, starting with{' '}
                <a href={explorer('asset', t.token_asset)} target="_blank"
                   rel="noopener noreferrer">its page on the explorer</a></> : ''}.
            </div>
          </td></tr>
        <tr><th>Reclaim key</th><td><Hex value={t.reclaim_xonly} />
          <div className="dim small prose">
            the project signs with this to take back what did not sell, from the
            close on. Levo never holds it
          </div></td></tr>
      </tbody>
    </table>
  )
}

function NotOpen({ sale }) {
  const text = {
    draft: 'The project has not locked its tokens yet, so there is nothing to buy. It can lock them at any time.',
    ghost: 'The chain does not have the output that funded this sale: either it never reached a block, or a Bitcoin-driven reorg took the block that held it. The sale is not funded and cannot be bought until the project locks its tokens again.',
    sold_out: 'Every token in this sale has been sold.',
    reclaimed: 'This sale closed and the project has taken back what did not sell.',
    closed: 'This sale is closed. Levo no longer plans purchases from it.',
  }[sale.status] || 'This sale is not open.'
  return (
    <div className="card">
      <h3>Not open</h3>
      <p className="small dim" style={{ marginBottom: 0 }}>{text}</p>
    </div>
  )
}

function BuyPanel({ project, onBought }) {
  const { signedIn, standing, tiers, loading, config, payment, stake } = useStore()
  const sale = project.sale
  const open = sale && (sale.status === 'live' || sale.status === 'partial')

  if (!open) return <NotOpen sale={sale} />
  if (loading) return <div className="card"><p className="dim" style={{ margin: 0 }}>Loading your account…</p></div>

  if (!signedIn) {
    return (
      <div className="card">
        <h3>Sign in to take a position</h3>
        <p className="small dim">
          Your tier decides how much you can put into this sale, and your tier
          comes from your stake.
        </p>
        <SignIn label="Sign in with your wallet" />
      </div>
    )
  }

  const tier = standing.tier
  const canInvest = tier.cap_atoms > 0
  const first = tiers && tiers.tiers.length > 1 ? tiers.tiers[1] : null

  return (
    <div className="card">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: '1rem' }}>
        <h3>Take a position</h3>
        <span className="pill">{tier.name}</span>
      </div>

      {tiers && (
        <Beam tiers={tiers.tiers} stakeAtoms={standing.stake_atoms} compactMode
              paymentLabel={payment.label} stakeLabel={stake.label} />
      )}

      {!canInvest ? (
        <>
          <p className="small dim">
            The {tier.name} tier cannot buy. Stake {first ? compact(first.min_stake_atoms) : compact(config.staking_floor_atoms)} {stake.label},
            sign in with the staking key or link it, and the first tier opens.
          </p>
          <Link className="btn btn-sm" to="/account">Link a staking key</Link>
        </>
      ) : (
        <BuyFlow project={project} tier={tier} onSettled={onBought} />
      )}
    </div>
  )
}

function EditPanel({ project, onSaved }) {
  const [form, setForm] = useState({
    name: project.name, summary: project.summary, description: project.description,
    links: Object.entries(project.links || {}).map(([k, v]) => k + ' ' + v).join('\n'),
  })
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState(false)

  async function save(e) {
    e.preventDefault()
    setError(null); setBusy(true)
    try {
      const links = {}
      for (const line of form.links.split('\n').map((l) => l.trim()).filter(Boolean)) {
        const i = line.lastIndexOf(' ')
        if (i < 1) throw new Error('Each link is a label, a space, then the URL.')
        links[line.slice(0, i).trim()] = line.slice(i + 1).trim()
      }
      const r = await api.updateProject(project.slug, {
        name: form.name, summary: form.summary, description: form.description, links,
      })
      setOpen(false)
      onSaved && onSaved(r)
    } catch (err) { setError(capitalise(err.message)) } finally { setBusy(false) }
  }

  if (!open) {
    return <button className="btn btn-sm btn-ghost" onClick={() => setOpen(true)}>Edit the listing's copy</button>
  }
  return (
    <form onSubmit={save} className="card" style={{ marginTop: '1rem' }}>
      <h3>Edit the listing</h3>
      <p className="small dim">The name, summary, description and links. Not the terms: those are compiled into the sale address, and changing one would be a different sale. Not the page name either, which every link to this sale is made of.</p>
      <div className="field"><label htmlFor="en">Name</label><input id="en" value={form.name} required onChange={(e) => setForm({ ...form, name: e.target.value })} /></div>
      <div className="field"><label htmlFor="es">One line</label><input id="es" value={form.summary} onChange={(e) => setForm({ ...form, summary: e.target.value })} /></div>
      <div className="field"><label htmlFor="ed">Description</label><textarea id="ed" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></div>
      <div className="field">
        <label htmlFor="el">Links</label>
        <textarea id="el" className="mono" rows={3} value={form.links} onChange={(e) => setForm({ ...form, links: e.target.value })}
                  placeholder={'Website https://…\nRegistry entry https://…'} />
        <div className="hint">One per line: a label, a space, then an http(s) URL.</div>
      </div>
      {error && <Notice kind="bad" style={{ marginBottom: '1rem' }}>{error}</Notice>}
      <div className="btn-row">
        <button className="btn btn-primary btn-sm" aria-disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => setOpen(false)}>Cancel</button>
      </div>
    </form>
  )
}

function LedgerPanel({ project }) {
  const { payment, explorer } = useStore()
  const [page, setPage] = useState(null)
  const [error, setError] = useState(null)
  const [open, setOpen] = useState(false)
  const d = project.decimals ?? 8

  useEffect(() => {
    if (!open || page) return
    api.purchases(project.slug, { limit: 25 })
      .then(setPage)
      .catch((e) => setError(capitalise(e.message)))
  }, [open, page, project.slug])

  if (!open) {
    return (
      <button className="btn btn-sm btn-ghost" style={{ marginTop: '.75rem' }}
              onClick={() => setOpen(true)}>
        See what Levo recorded
      </button>
    )
  }

  return (
    <div className="card" style={{ marginTop: '1rem' }}>
      <h3>What Levo recorded</h3>
      <p className="small dim">
        Levo's own ledger for this sale, which is what the per-buyer caps are
        measured against. The chain is the authority on what the sale holds: the
        sell leaf takes no signature, so a purchase can happen without Levo and
        appear nowhere here.
      </p>
      {error && <Notice kind="bad">{error}</Notice>}
      {!page && !error && <p className="dim small">Reading…</p>}
      {page && page.purchases.length === 0 && (
        <p className="small dim" style={{ marginBottom: 0 }}>
          Nothing recorded yet.{' '}
          {project.sale && project.sale.sold_atoms > 0
            ? 'The chain says some of this sale has sold, so those purchases were made without Levo.'
            : ''}
        </p>
      )}
      {page && page.purchases.length > 0 && (
        <>
          <table className="terms">
            <tbody>
              {page.purchases.map((e) => (
                <tr key={e.txid + e.account}>
                  <th style={{ fontWeight: 400 }}>
                    <span className="mono">{shortHex(e.account, 6, 4)}</span>
                    <div className="dim small">{timeLabel(e.at)}</div>
                  </th>
                  <td>
                    {amount(e.token_atoms, d)} {project.ticker} for{' '}
                    {amount(e.payment_atoms, payment.decimals)} {payment.label}
                    <div className="dim small prose">
                      <Hex value={e.txid} href={explorer('tx', e.txid)} />
                      {e.verified === true ? ' · treasury payment checked on chain'
                        : e.verified === false ? ' · the treasury payment did not check out'
                          : ' · not checked on chain'}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="small dim" style={{ marginTop: '.75rem', marginBottom: 0 }}>
            {page.purchases.length} of {page.total} shown.
          </p>
        </>
      )}
    </div>
  )
}

function FlagPanel({ project, onFlagged }) {
  const [notice, setNotice] = useState(project.notice || '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function apply(hidden) {
    if (busy) return
    setError(null); setBusy(true)
    try {
      await api.flag(project.slug, { hidden, notice: notice.trim() })
      onFlagged && onFlagged()
    } catch (e) { setError(capitalise(e.message)) } finally { setBusy(false) }
  }

  return (
    <div className="card" style={{ marginTop: '1rem' }}>
      <h3>Operator</h3>
      <p className="small dim">
        You can take this listing off the board and put a notice on its page.
        That is all it reaches: the sale is a covenant on chain, and anyone
        holding its terms can still buy from it.
      </p>
      <div className="field">
        <label htmlFor="opnotice">Notice on this page</label>
        <textarea id="opnotice" rows={2} value={notice} maxLength={400}
                  aria-describedby="opnotice-hint"
                  onChange={(e) => setNotice(e.target.value)}
                  placeholder="Under review." />
        <div className="hint" id="opnotice-hint">
          Shown at the top of the page, above the summary. Leave it empty to
          take it down.
        </div>
      </div>
      {error && <Notice kind="bad" style={{ marginBottom: '1rem' }}>{error}</Notice>}
      <div className="btn-row">
        <button className="btn btn-sm" aria-disabled={busy}
                onClick={() => apply(project.hidden)}>
          {busy ? 'Saving…' : 'Save the notice'}
        </button>
        <button className="btn btn-sm btn-ghost" aria-disabled={busy}
                onClick={() => apply(!project.hidden)}>
          {project.hidden ? 'Put it back on the board' : 'Take it off the board'}
        </button>
      </div>
      {project.hidden && (
        <p className="small dim" style={{ margin: '.75rem 0 0' }}>
          This listing is off the board. Its page still answers, and the sale is
          untouched.
        </p>
      )}
    </div>
  )
}

export default function ProjectDetail() {
  const { slug } = useParams()
  const { account, explorer, chainHeight, payment, standing, stake } = useStore()
  const [project, setProject] = useState(null)
  const [error, setError] = useState(null)
  const [notFound, setNotFound] = useState(false)
  const [withdrawError, setWithdrawError] = useState(null)
  const [withdrawn, setWithdrawn] = useState(false)
  const [confirmWithdraw, setConfirmWithdraw] = useState(false)
  usePageTitle(project ? project.name + ' (' + project.ticker + ')' : 'Sale')

  const load = () => {
    setError(null); setNotFound(false)
    return api.project(slug).then(setProject).catch(fail)
  }
  function fail(e) {
    if (e && e.status === 404) setNotFound(true)
    else setError(e.message)
  }
  useEffect(() => {
    let alive = true
    setError(null); setNotFound(false); setProject(null); setWithdrawn(false)
    api.project(slug).then((p) => alive && setProject(p)).catch((e) => alive && fail(e))
    return () => { alive = false }
  }, [slug])

  // The countdown has to be current: a sale that closes at a block would
  // otherwise go on claiming the same number of blocks left all afternoon. The
  // store keeps the height fresh for the whole app.
  const height = chainHeight

  if (withdrawn) {
    return (
      <div className="wrap section" style={{ maxWidth: 560 }}>
        <h1 className="h2">Listing withdrawn</h1>
        <p className="dim">The draft is gone. Nothing was ever locked, so nothing is on chain.</p>
        <Link className="btn" to="/launch">Launch another</Link>
      </div>
    )
  }
  if (notFound) {
    return (
      <div className="wrap section" style={{ maxWidth: 560 }}>
        <h1 className="h2">No sale at this address</h1>
        <p className="dim">
          Levo has no listing called <span className="mono">{slug}</span>. It may
          have been withdrawn before it was funded, or the link may be mistyped.
        </p>
        <div className="btn-row">
          <Link className="btn" to="/projects">See the open sales</Link>
          <Link className="btn btn-ghost" to="/launch">Launch one</Link>
        </div>
      </div>
    )
  }
  if (error) {
    return (
      <div className="wrap section">
        <h1 className="h2">Sale</h1>
        <Notice kind="bad" style={{ marginTop: '1rem' }}>
          {capitalise(error)} <button className="btn btn-sm btn-ghost" style={{ marginLeft: '.75rem' }} onClick={load}>Try again</button>
        </Notice>
      </div>
    )
  }
  if (!project) return <div className="wrap section"><h1 className="h2">Sale</h1><p className="dim">Loading…</p></div>

  const sale = project.sale
  const d = project.decimals ?? 8
  const issuer = account && account === project.issuer_account
  const operator = !!(standing && standing.operator)
  const open = sale && (sale.status === 'live' || sale.status === 'partial')
  const needsLock = sale && (sale.status === 'draft' || sale.status === 'ghost')
  const closeLeft = sale && open ? closeIn(sale.terms.close_locktime, height) : ''
  const links = Object.entries(project.links || {})

  async function withdraw() {
    setWithdrawError(null)
    try { await api.withdraw(project.slug); setWithdrawn(true) } catch (e) { setWithdrawError(capitalise(e.message)) }
  }

  return (
    <div className="wrap section">
      <p className="eyebrow">{project.ticker} <span className="dim">·</span> <Status sale={sale} /></p>
      <h1 style={{ fontSize: 'clamp(2rem, 4.5vw, 3.2rem)' }}>{project.name}</h1>
      <p className="hero-lede" style={{ marginTop: '1rem' }}>{project.summary}</p>
      {project.issuer && (
        <p className="small dim" style={{ marginTop: '.75rem' }}>
          Listed by <span className="mono">{shortHex(project.issuer.account, 8, 6)}</span>
          {project.issuer.tier ? <>, {project.issuer.tier}</> : null}
          {project.issuer.stake_atoms
            ? <>, with {compact(project.issuer.stake_atoms)} {stake.label} staked</>
            : null}. That stake is the only thing on this page the project did
          not write itself.
        </p>
      )}
      {project.notice && (
        <Notice kind="bad" style={{ marginTop: '1rem' }}>
          <strong>From the operator of this Levo:</strong> {project.notice}
          <div className="small" style={{ marginTop: '.4rem' }}>
            This is a note on the page. The sale is a covenant on chain: Levo can
            neither stop it nor change its terms.
          </div>
        </Notice>
      )}
      {links.length > 0 && (
        <ul className="links-list">
          {links.map(([label, href]) => (
            <li key={label}><a href={href} target="_blank" rel="noopener noreferrer">{label} ↗</a></li>
          ))}
        </ul>
      )}

      <div className="split split-action-first" style={{ marginTop: '1.5rem' }}>
        <div>
          {project.description && (
            <div style={{ marginBottom: '2.5rem' }}>
              {project.description.split('\n').filter(Boolean).map((para, i) => (
                <p key={i}>{para}</p>
              ))}
            </div>
          )}

          {sale && (
            <>
              <h2 style={{ marginBottom: '1rem' }}>Terms</h2>
              <p className="small dim" style={{ maxWidth: '60ch' }}>
                Most of these are compiled into the covenant: change one and the
                sale address changes with it, which is what makes the address
                worth checking. The amount for sale is not. Levo checked it
                against the chain when it accepted the lock, and after that the
                covenant simply sells whatever it holds.
              </p>
              <Terms project={project} sale={sale} />

              {project.verify && (
                <Notice kind="good" style={{ marginTop: '1.5rem' }}>
                  <strong>{sale.funding ? (open ? 'Verify before you buy.' : 'Verify the lock.')
                                          : 'Check the address before you send anything.'}</strong>{' '}
                  {sale.funding
                    ? <>Rebuild the sale address from the terms above and compare it with the
                        address the funding output pays. If they match, the tokens are locked
                        under exactly these terms.</>
                    : <>Nothing is locked here yet. Rebuild the address from the terms above
                        before sending tokens to it: the address is what the terms make, and
                        only a derived address is safe to pay.</>}{' '}
                  <span className="mono">levo verify {project.slug}</span> does it on your own node.
                  <table className="terms" style={{ marginTop: '.75rem' }}>
                    <tbody>
                      <tr><th>Sale address</th>
                          <td><Hex value={project.address} href={explorer('address', project.address)} /></td></tr>
                      <tr><th>scriptPubKey</th><td><Hex value={project.verify.script_pubkey} /></td></tr>
                      <tr><th>Internal key</th>
                          <td className="prose">{project.verify.internal_key}</td></tr>
                      {sale.funding && (
                        <tr><th>{sale.sold_atoms > 0 ? 'Resting at' : 'Locked at'}</th>
                            <td><Hex value={sale.funding.txid + ':' + sale.funding.vout} href={explorer('tx', sale.funding.txid)} />
                              {sale.sold_atoms > 0 && (
                                <div className="dim small prose">
                                  a partial buy re-rests what is left at the same address,
                                  so this is where the covenant sits now, not where it was funded
                                </div>
                              )}
                            </td></tr>
                      )}
                    </tbody>
                  </table>
                </Notice>
              )}
              {sale.funding && sale.funding.unverifiable && (
                <Notice kind="bad" style={{ marginTop: '1rem' }}>
                  <strong>Levo cannot place this sale's funding in the chain.</strong>{' '}
                  Its own record of where the tokens were locked was lost, and the
                  transaction is in none of the recent blocks it can see. The sale
                  is shown exactly as it was last known, which may be out of date.
                  Rebuild the address from the terms above and look it up yourself
                  before acting on anything here.
                </Notice>
              )}
              {sale.strays && sale.strays.length > 0 && (
                <Notice kind="bad" style={{ marginTop: '1rem' }}>
                  <strong>Other assets are resting at the sale address.</strong> They are not
                  for sale, but the sell leaf does not check what asset it spends, so anyone
                  can take them at the sale's price. After the close the project can spend
                  them under its reclaim key, in a transaction it builds itself: Levo's own
                  reclaim sweeps the sale token and nothing else.
                  <ul className="small" style={{ margin: '.5rem 0 0', paddingLeft: '1.1rem' }}>
                    {sale.strays.map((s) => (
                      <li key={s.txid + s.vout}>
                        {amount(s.atoms, s.asset === sale.terms.payment_asset ? payment.decimals : 8)}{' '}
                        {s.asset === sale.terms.payment_asset ? payment.label : <span className="mono">{shortHex(s.asset, 8, 6)}</span>}
                        {' '}at <Hex value={s.txid + ':' + s.vout}
                                    href={explorer('tx', s.txid)} short={14} />
                      </li>
                    ))}
                  </ul>
                </Notice>
              )}
            </>
          )}
        </div>

        <div className="sticky">
          {issuer && needsLock ? (
            <LockPanel project={project} onLocked={load} />
          ) : (
            <BuyPanel project={project} onBought={load} />
          )}
          {issuer && sale && (sale.status === 'closed') && sale.locked_atoms > 0 && (
            <Reclaim project={project} />
          )}
          {sale && sale.terms.total_atoms && (
            <div className="card" style={{ marginTop: '1rem' }}>
              <div className="kv"><span>Sold</span><b>{amount(sale.sold_atoms, d)} {project.ticker}</b></div>
              <div className="kv"><span>Still locked</span><b>{amount(sale.locked_atoms, d)} {project.ticker}</b></div>
              <div className="kv"><span>Total for sale</span><b>{amount(sale.terms.total_atoms, d)} {project.ticker}</b></div>
              {typeof sale.buyers === 'number' && <div className="kv"><span>Buyers through Levo</span><b>{sale.buyers}</b></div>}
              {closeLeft && <div className="kv"><span>Reclaim opens</span><b>{closeLeft}</b></div>}
            </div>
          )}
          {operator && <FlagPanel project={project} onFlagged={load} />}
          {issuer && (
            <div style={{ marginTop: '1rem' }}>
              <EditPanel project={project} onSaved={setProject} />
              <LedgerPanel project={project} />
              {needsLock && (
                <div style={{ marginTop: '.75rem' }}>
                  {!confirmWithdraw ? (
                    <button className="btn btn-sm btn-ghost"
                            onClick={() => setConfirmWithdraw(true)}>
                      Withdraw this listing
                    </button>
                  ) : (
                    <Notice kind="bad">
                      <strong>Withdrawing deletes the terms.</strong> The sale
                      address is made of them, so anything already sent there can
                      only be recovered with your reclaim key after the close —
                      and rebuilding the address needs these exact values. Copy
                      them first if you have sent anything.
                      <div className="btn-row" style={{ marginTop: '.75rem' }}>
                        <button className="btn btn-sm" onClick={withdraw}>
                          Withdraw it anyway
                        </button>
                        <button className="btn btn-sm btn-ghost"
                                onClick={() => setConfirmWithdraw(false)}>
                          Keep it
                        </button>
                      </div>
                    </Notice>
                  )}
                  {withdrawError && <Notice kind="bad" style={{ marginTop: '.5rem' }}>{withdrawError}</Notice>}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
