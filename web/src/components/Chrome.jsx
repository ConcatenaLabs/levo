import { NavLink, Link } from 'react-router-dom'
import { useStore } from '../lib/store'
import { shortHex } from '../lib/format'

function Mark() {
  // A weight resting on a fulcrum: the same figure the beam draws.
  return (
    <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
      <path d="M4 21h24" stroke="var(--brass)" strokeWidth="2" strokeLinecap="round" />
      <path d="M16 21l-5.5 7h11z" fill="var(--verdigris)" />
      <rect x="11" y="7" width="10" height="8" rx="1" fill="none"
            stroke="var(--brass)" strokeWidth="2" />
    </svg>
  )
}

export function Nav() {
  const { signedIn, tier, account } = useStore()
  return (
    <nav className="nav">
      <div className="wrap nav-in">
        <Link to="/" className="brand">
          <Mark />
          <span className="brand-word">Levo</span>
        </Link>
        <div className="nav-links">
          <NavLink to="/projects">Sales</NavLink>
          <NavLink to="/launch">Launch</NavLink>
          <NavLink to="/how-it-works">How it works</NavLink>
          <NavLink to="/account">
            {signedIn ? (
              <span className="mono" style={{ fontSize: '.82rem' }}>
                {tier ? tier.name : ''} · {shortHex(account, 6, 4)}
              </span>
            ) : 'Sign in'}
          </NavLink>
        </div>
      </div>
    </nav>
  )
}

export function Footer() {
  return (
    <footer className="footer">
      <div className="wrap footer-in">
        <div style={{ flex: '1 1 320px' }}>
          <div className="brand" style={{ marginBottom: '.6rem' }}>
            <Mark /><span className="brand-word">Levo</span>
          </div>
          <p style={{ maxWidth: '46ch', margin: 0 }}>
            A launchpad on Sequentia. Project tokens sit in a covenant from lock
            to delivery, so a sale settles without anyone holding both sides.
          </p>
        </div>
        <div>
          <Link to="/how-it-works">How it works</Link><br />
          <Link to="/projects">Sales</Link><br />
          <Link to="/launch">Launch a project</Link>
        </div>
        <div className="small">
          Sequentia testnet. Tokens here carry no value.
        </div>
      </div>
    </footer>
  )
}
