import { Link } from 'react-router-dom'
import { useStore } from '../lib/store'
import { usePageTitle } from '../components/ui'
import { amount, compact } from '../lib/format'

export default function HowItWorks() {
  usePageTitle('How it works')
  const { tiers, config, payment, stake, links } = useStore()
  const first = tiers && tiers.tiers.length > 1 ? tiers.tiers[1] : null
  const firstAtoms = first ? first.min_stake_atoms : config.first_tier_atoms
  const floor = compact(config.staking_floor_atoms)
  const hrp = config.hrp || 'tb'
  const source = config.source_url
  const wallet = links.Wallet || links.wallet
  const faucet = links.Faucet || links.faucet
  return (
    <div className="wrap section" style={{ maxWidth: 820 }}>
      <p className="eyebrow">How it works</p>
      <h1 className="h2">What Levo does, and what it cannot do</h1>
      <p>
        Levo is two things: a set of allocation rules, and a place to find
        sales that use them. It is not a custodian. Knowing exactly where the
        line falls is the difference between trusting a covenant and trusting
        an operator, so this page draws it plainly.
      </p>

      <h2 className="section-h">What you need</h2>
      <p>
        <strong>To buy:</strong> a Levo account, which is a key you can sign with
        {wallet ? <> (<a href={wallet} target="_blank" rel="noopener noreferrer">a wallet</a>, or any wallet that signs messages)</> : ' (a browser extension, or any wallet that signs messages)'};
        staked Sequence under a key you can prove you control, at or above the first
        tier; and unblinded {payment.label} to pay with — unblinded meaning the
        amount is written on the chain in the clear, which is how Sequentia pays
        by default{config.testnet && faucet ? <> (on the testnet, <a href={faucet} target="_blank" rel="noopener noreferrer">the faucet</a> hands both out)</> : ''}.
        A browser extension that can sign a transaction a website built does the
        whole purchase in place. A node signs it with{' '}
        <span className="mono">signrawtransactionwithwallet</span>, or{' '}
        <span className="mono">levo buy</span> does the lot.
      </p>
      <p>
        <strong>To list:</strong> a tier that may list; an issued asset, registered so
        wallets show its name; the whole allocation in a wallet you can send from; an
        address for the treasury, which is where buyers' payments land; and a
        reclaim key you can sign with outside a browser wallet, because reclaiming
        means signing a bare hash rather than a transaction a wallet would recognise.
        <span className="mono"> levo keygen</span> makes one.
      </p>

      <h2 className="section-h">Signing in</h2>
      <p>
        Your account is a public key. Levo issues a challenge, your wallet signs
        it, and Levo recovers the key from the signature. That key is who you
        are. There is no password to steal, no account to create, and no
        signature Levo can produce on your behalf. The challenge names the site,
        carries a single-use nonce, says in its own text that it authorises no
        payment, and must be signed exactly as issued: a signature over any
        other text signs nobody in.
      </p>

      <h2 className="section-h">Tiers</h2>
      <p>
        Your tier comes from Sequence staked under keys you have proven you
        control. Signing in with a staking key proves it. Proving another key
        means signing a statement that names both the key and your account, so a
        signature collected for one purpose cannot be replayed to attach the
        same stake somewhere else. One key counts for one account: the newest
        proof of control holds it. A stake delegated to a pool still counts for
        the person who owns it.
      </p>
      <p>
        Stake is made on a node, not here. {' '}
        <span className="mono">sequentia-cli registerstake &lt;staking pubkey&gt; &lt;amount&gt;</span>{' '}
        bonds coins to a staking key and locks them for the chain&rsquo;s
        unbonding delay;{' '}
        <span className="mono">sequentia-cli getstakerinfo true true</span>{' '}
        lists the keys a node holds and what each has bonded. Sign in with one
        of those keys and the tier it earns is on the first screen.
      </p>
      {tiers && (
        <table className="terms" style={{ marginTop: '1.25rem' }}>
          <tbody>
            {tiers.tiers.map((t) => (
              <tr key={t.level}>
                <th>{t.name}</th>
                <td>
                  {t.min_stake_atoms === 0 ? 'no stake' : compact(t.min_stake_atoms) + ' ' + stake.label + ' staked'}
                  <div className="dim small prose">
                    {t.cap_atoms ? 'up to ' + amount(t.cap_atoms, payment.decimals) + ' ' + payment.label + ' per sale' : 'cannot buy'}
                    {t.may_list ? ' · may list a project' : ''}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p style={{ marginTop: '1.25rem' }}>
        {config.first_tier_is_chain_floor
          ? 'The first tier begins at ' + floor + ' ' + stake.label + ' because that is the chain\'s own blocksigner floor. Below it, consensus ignores a staker\'s weight entirely. Levo did not invent a threshold; it borrowed the one already being enforced.'
          : 'The first tier begins at ' + compact(firstAtoms) + ' ' + stake.label + ' on this deployment. The chain\'s own blocksigner floor is ' + floor + ' ' + stake.label + ', below which consensus ignores a staker\'s weight entirely.'}
        {' '}The thresholds are Levo's configuration; the stake behind each key is read
        from the chain.
      </p>

      <h2 className="section-h">A sale</h2>
      <p>
        A project locks its whole allocation in one taproot output whose spending
        conditions are compiled from the sale's terms. The output has no key
        path, so there are exactly two ways out of it:
      </p>
      <p>
        <strong>Selling.</strong> Anyone may spend it, without a signature from
        the project or from Levo, provided the same transaction pays the
        treasury at least the agreed price for what is taken. A partial buy has
        to return the unsold remainder to the identical address, so the sale
        keeps resting until it sells out or is reclaimed. Payment and delivery are
        therefore the same transaction: there is no state where the project has
        been paid and the buyer has not been delivered.
      </p>
      <p>
        <strong>Reclaiming.</strong> From the close date on, the project may sweep
        whatever did not sell, under its own signature. The close opens that path;
        it does not shut the selling path, so a buyer who builds the transaction can
        still fill a sale after its close, until the project reclaims. Levo stops
        planning purchases at the close.
      </p>

      <h2 className="section-h">What is enforced, and by whom</h2>
      <p>
        The covenant enforces the price, the treasury that gets paid, the token a
        remainder must be, the minimum purchase, and the earliest moment the project can
        reclaim. Once a sale is funded none of those can be changed, by the project
        or by Levo. One consequence is worth stating: the treasury is the project's
        own key, so a project can always buy its own sale out at the published
        price, on chain and in the open, for the cost of the fee. What it cannot do
        is take the tokens by any other route.
      </p>
      <p>
        Tier caps are different. The sell condition has a minimum purchase and no
        maximum, and it takes no signature: anyone who can build the transaction
        can buy, whether or not they came through Levo. A cap is Levo's own
        allocation policy, applied to the purchases Levo plans. Nothing on chain
        enforces it, and a sale can be filled without Levo at all.
      </p>

      <h2 className="section-h">Paying</h2>
      <p>
        {payment.label} settles inside the covenant, which is what makes a purchase
        atomic end to end. BTC is native Bitcoin on the parent chain, not a token on
        Sequentia and not a pegged claim on one, so a Sequentia covenant cannot
        read a Bitcoin output. A BTC purchase is two separate steps instead. First
        your wallet swaps BTC for {payment.label} over Lightning, at a rate Levo
        quotes from the fee exchange-rate table of the node it reads. Then you fill
        the covenant with that {payment.label}, exactly as a {payment.label} buyer
        does. Each step is atomic on its own, but nothing ties them together:
        between them you hold {payment.label}, and you can stop there. Levo never
        holds either side. If you want the consensus guarantee end to end, pay in
        {' '}{payment.label}.
      </p>

      <h2 className="section-h">If Bitcoin reorgs</h2>
      <p>
        Sequentia follows its Bitcoin anchor, so a funding transaction can be
        un-made after a sale was already showing as open. Levo does not treat
        that as a brief loss of confirmation. The sale shows as not funded again,
        nothing can be bought, and it reopens when the project locks its tokens at
        the same address a second time.
      </p>

      <h2 className="section-h">Checking Levo's work</h2>
      <p>
        A hostile or broken Levo could show a sale that is not funded, quote a
        price that is not the covenant's, or hide a listing. What it cannot do
        is move tokens. Every sale publishes the terms its address was derived
        from: rebuild the address from them, compare it with the address the
        funding output pays, and you have checked the only thing that matters
        without trusting the server about it. <span className="mono">levo verify &lt;sale&gt;</span>
        {' '}does that on your own node.
      </p>

      <h2 className="section-h">The levo command</h2>
      <p>
        <span className="mono">levo</span> is a command-line client for everything
        on this site: it lists sales, rebuilds a sale address from its terms and
        compares it with the chain, and drives a purchase, a lock or a reclaim
        against your own Sequentia node. It holds no keys of its own — your node's
        wallet signs, on your machine.
        {source ? <> It lives in <a href={source} target="_blank" rel="noopener noreferrer">
          the Levo repository</a> at <span className="mono">bin/levo</span>.</> : null}
        {' '}Point it at this site by setting{' '}
        <span className="mono">LEVO_URL</span>, and it needs Python 3 and{' '}
        <span className="mono">sequentia-cli</span> on your path.
      </p>

      <h2 className="section-h">Words this page uses</h2>
      <dl className="glossary">
        <dt>Sequence ({stake.label})</dt>
        <dd>
          Sequentia's staking token. The network is Sequentia and the token is
          Sequence; {stake.label} is its ticker, and it is what every stake
          figure on this site is labelled with. Staking is the one thing only
          Sequence can do. For everything else it is an asset like any other on
          the chain, with no standing above the tokens sold here.
        </dd>
        <dt>{payment.label}</dt>
        <dd>
          The asset every sale on this Levo is priced and paid in: an ordinary
          token issued on Sequentia, no more privileged by the chain than the
          tokens being sold. Its asset id is on each sale page, beside
          &ldquo;Paid in&rdquo;. Pricing every sale in one asset is this
          deployment&rsquo;s own policy, so that any two sales can be compared;
          a covenant will hold terms in any asset at all.
        </dd>
        <dt>Treasury</dt>
        <dd>
          The address a sale&rsquo;s payments land at: the project&rsquo;s own,
          chosen before the sale opened and compiled into the sale address. The
          covenant will not part with a token unless that address is paid, and
          moving it would produce a different sale address.
        </dd>
        <dt>Leaf</dt>
        <dd>
          One of the spending conditions inside a taproot output. A sale
          publishes two: the sell leaf, which anyone may spend through by
          paying the treasury, and the reclaim leaf, which only the project can
          spend, and only after the close.
        </dd>
        <dt>Blocksigner floor</dt>
        <dd>
          The least stake Sequentia itself counts. Blocks here are produced by
          stakers, and stake below this floor is ignored when the chain decides
          who signs the next one. Levo&rsquo;s first tier begins there rather
          than at a threshold of its own.
        </dd>
        <dt>Atom</dt>
        <dd>
          The smallest unit an asset has, the way a satoshi is Bitcoin's. Every
          amount the chain holds is a whole number of atoms; the decimals a page
          shows are a display of that number.
        </dd>
        <dt>Covenant</dt>
        <dd>
          An output whose spending conditions are a program rather than a
          signature. A Levo sale is one: the conditions are compiled from the
          terms, so the address itself is the promise.
        </dd>
        <dt>Taproot</dt>
        <dd>
          The output type those conditions live in ({hrp}1p… addresses). A sale
          uses one with no usable key path, so the only ways to spend it are the
          two conditions it publishes.
        </dd>
        <dt>Unblinded</dt>
        <dd>
          An output whose asset and amount are written on chain in the clear.
          Sequentia is unblinded by default; confidentiality is something you turn
          on per payment. A covenant can only read what is in the clear, so a
          confidential output can neither fund a sale nor pay for one.
        </dd>
        <dt>Sighash</dt>
        <dd>
          The hash a signature is made over. A reclaim is signed as a bare hash
          because the key that signs it is the project's own, held outside any
          wallet that would recognise the transaction.
        </dd>
      </dl>

      <div className="btn-row" style={{ marginTop: '2.5rem' }}>
        <Link className="btn btn-primary" to="/projects">See the sales</Link>
        <Link className="btn btn-ghost" to="/account">Check your tier</Link>
      </div>
    </div>
  )
}
