import { Link } from 'react-router-dom'
import { useStore } from '../lib/store'
import { amount, compact } from '../lib/format'

export default function HowItWorks() {
  const { tiers } = useStore()
  return (
    <div className="wrap section" style={{ maxWidth: 820 }}>
      <p className="eyebrow">How it works</p>
      <h2>What Levo does, and what it cannot do</h2>
      <p style={{ marginTop: '1.25rem' }}>
        Levo is two things: a set of allocation rules, and a place to find
        sales that use them. It is not a custodian. Knowing exactly where the
        line falls is the difference between trusting a covenant and trusting
        an operator, so this page draws it plainly.
      </p>

      <h3 style={{ margin: '2.5rem 0 .75rem' }}>Signing in</h3>
      <p>
        Your account is a public key. Levo issues a challenge, your wallet signs
        it, and Levo recovers the key from the signature. That key is who you
        are. There is no password to steal, no account to create, and no
        signature Levo can produce on your behalf. The challenge names the site,
        carries a single-use nonce, and says in its own text that it authorises
        no payment.
      </p>

      <h3 style={{ margin: '2.5rem 0 .75rem' }}>Tiers</h3>
      <p>
        Your tier comes from Sequence staked under keys you have proven you
        control. Proving a key means signing a statement that names both the key
        and your account, so a signature collected for one purpose cannot be
        replayed to attach the same stake somewhere else. One key counts for one
        account.
      </p>
      {tiers && (
        <table className="terms" style={{ marginTop: '1.25rem' }}>
          <tbody>
            {tiers.tiers.map((t) => (
              <tr key={t.level}>
                <th>{t.name}</th>
                <td>
                  {t.min_stake_atoms === 0 ? 'no stake' : compact(t.min_stake_atoms) + ' SEQ staked'}
                  <div className="dim small" style={{ fontFamily: 'var(--body)' }}>
                    {t.cap_atoms ? amount(t.cap_atoms) + ' USDX per sale' : 'no allocation'}
                    {t.may_list ? ' · may list a project' : ''}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p style={{ marginTop: '1.25rem' }}>
        The first tier begins at 40,000 SEQ because that is the chain's own
        blocksigner floor. Below it, consensus ignores a staker's weight
        entirely. Levo did not invent a threshold; it borrowed the one already
        being enforced.
      </p>

      <h3 style={{ margin: '2.5rem 0 .75rem' }}>A sale</h3>
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
        keeps resting until it sells out or closes. Payment and delivery are
        therefore the same transaction: there is no state where the project has
        been paid and the buyer has not been delivered.
      </p>
      <p>
        <strong>Reclaiming.</strong> After the close date the project sweeps
        whatever did not sell, under its own signature.
      </p>

      <h3 style={{ margin: '2.5rem 0 .75rem' }}>What is enforced, and by whom</h3>
      <p>
        The covenant enforces the price, the treasury that gets paid, the token
        being sold, the minimum lot, and the close date. Once a sale is funded
        none of those can be changed, by the project or by us.
      </p>
      <p>
        Tier caps are different, and it would be dishonest to present them
        otherwise. The sell condition has a floor but no ceiling, and it is
        permissionless by design: anyone able to build the transaction can buy,
        whether or not they came through Levo. Caps are Levo's allocation
        policy, applied to every purchase it plans. They are not a consensus
        rule, and every cap the API returns says so.
      </p>

      <h3 style={{ margin: '2.5rem 0 .75rem' }}>Paying</h3>
      <p>
        USDX settles inside the covenant, which is what makes a purchase atomic
        end to end. BTC is native Bitcoin on the parent chain, not a token on
        Sequentia and not a pegged claim on one, so a Sequentia covenant cannot
        read a Bitcoin output. A BTC purchase settles in two linked legs instead:
        the buyer pays over Lightning, and the same preimage releases the leg
        that fills the covenant. Atomic per leg, with the preimage holding the
        seam. If you want the consensus guarantee with nothing at the seam, pay
        in USDX.
      </p>

      <h3 style={{ margin: '2.5rem 0 .75rem' }}>If Bitcoin reorgs</h3>
      <p>
        Sequentia follows its Bitcoin anchor, so a funding transaction can be
        un-made after a sale was already showing as open. Levo does not treat
        that as a brief loss of confirmation. The sale is not funded, it goes
        back to being a draft, and it stops being investable until it is locked
        again.
      </p>

      <h3 style={{ margin: '2.5rem 0 .75rem' }}>Checking Levo's work</h3>
      <p>
        A hostile or broken Levo could show a sale that is not funded, quote a
        price that is not the covenant's, or hide a listing. What it cannot do
        is move tokens. Every sale publishes the terms its address was derived
        from: rebuild the address from them, compare it to the funded output,
        and you have checked the only thing that matters without trusting the
        server about it.
      </p>

      <div style={{ display: 'flex', gap: '.75rem', marginTop: '2.5rem', flexWrap: 'wrap' }}>
        <Link className="btn btn-primary" to="/projects">See open sales</Link>
        <Link className="btn btn-ghost" to="/account">Check your tier</Link>
      </div>
    </div>
  )
}
