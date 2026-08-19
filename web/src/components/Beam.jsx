import { compact } from '../lib/format'

// Levo's one drawing: the rule itself, plotted.
//
// Stake runs left to right, the allowance ceiling steps up with it, and your
// own stake sits on the step it has reached. It is a step function because that
// is genuinely what the rule is -- allowance does not rise smoothly with stake,
// it jumps at thresholds -- and drawing it any other way would flatter the
// design at the cost of describing the thing wrongly.
//
// The x axis is piecewise: each tier occupies an equal span. A true linear axis
// would collapse the lower tiers into the left edge, since the thresholds are
// orders of magnitude apart.

// Each tier owns an equal segment, INCLUDING the top one -- it is where the
// people with the most stake actually sit, so a scale that gives it no width
// draws everybody at the edge and shows the tier not at all.
function xFor(stakeAtoms, stops) {
  const segments = stops.length
  const s = Number(stakeAtoms || 0)
  let k = 0
  for (let i = 0; i < stops.length; i++) if (s >= stops[i]) k = i
  let f
  if (k + 1 < stops.length) {
    const lo = stops[k], hi = stops[k + 1]
    f = hi > lo ? (s - lo) / (hi - lo) : 0
  } else {
    // Above the top threshold there is no next one to measure against, so
    // position by how far past it the stake has gone, easing towards the end.
    const lo = stops[k] || 1
    f = Math.min(0.85, (s - lo) / (lo * 4))
  }
  return (k + Math.max(0, Math.min(1, f))) / segments
}

export default function Beam({ tiers, stakeAtoms = 0, compactMode = false, showMarker = true }) {
  if (!tiers || tiers.length < 2) return null

  const W = 1000
  const H = compactMode ? 104 : 168
  const padB = compactMode ? 26 : 34      // room for the threshold labels
  const padT = compactMode ? 20 : 30      // headroom, so the top step is not flush
  const n = tiers.length - 1
  const stops = tiers.map((t) => t.min_stake_atoms)
  const caps = tiers.map((t) => t.cap_atoms)
  const maxCap = Math.max(...caps, 1)

  // A square-root height scale: the caps span two orders of magnitude, and on a
  // linear scale the first tier's step would be invisible.
  const yFor = (cap) => {
    const top = padT
    const bottom = H - padB
    const f = Math.sqrt(cap / maxCap)
    return bottom - f * (bottom - top)
  }

  const seg = W / tiers.length
  const steps = tiers.map((t, i) => ({
    i, x0: i * seg, x1: (i + 1) * seg, y: yFor(caps[i]), tier: t,
  }))

  const stake = Number(stakeAtoms || 0)
  const mx = xFor(stake, stops) * W
  const reachedIndex = steps.reduce((acc, s, i) => (stake >= stops[i] ? i : acc), 0)
  const my = steps[reachedIndex].y

  const path = steps.map((s, i) =>
    `${i === 0 ? 'M' : 'L'} ${s.x0} ${s.y} L ${s.x1} ${s.y}`).join(' ')

  return (
    <div className={'beam' + (compactMode ? ' compact' : '')}>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
           role="img" aria-label="Allowance ceiling by amount staked">
        {/* the ground the steps stand on */}
        <line x1="0" y1={H - padB} x2={W} y2={H - padB}
              stroke="var(--line)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
        {/* filled area up to the reached step, so progress reads at a glance */}
        {steps.slice(0, reachedIndex + 1).map((s) => (
          <rect key={'f' + s.i} x={s.x0} y={s.y}
                width={(s.i === reachedIndex ? mx : s.x1) - s.x0}
                height={Math.max(0, H - padB - s.y)}
                fill="var(--brass)" opacity="0.14" />
        ))}
        {/* the rule itself */}
        <path d={path} fill="none" stroke="var(--brass)" strokeWidth="2"
              vectorEffect="non-scaling-stroke" strokeLinejoin="miter" />
        {/* risers at each threshold */}
        {steps.slice(1).map((s) => (
          <line key={'r' + s.i} x1={s.x0} y1={steps[s.i - 1].y} x2={s.x0} y2={s.y}
                stroke="var(--brass)" strokeWidth="2"
                vectorEffect="non-scaling-stroke" />
        ))}
        {/* threshold ticks down to the labels */}
        {steps.slice(1).map((s) => (
          <line key={'t' + s.i} x1={s.x0} y1={H - padB} x2={s.x0} y2={H - padB + 6}
                stroke="var(--line)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
        ))}
        {showMarker && (
          <g>
            <line x1={mx} y1={my} x2={mx} y2={H - padB}
                  stroke="var(--verdigris)" strokeWidth="1.5"
                  vectorEffect="non-scaling-stroke" strokeDasharray="3 3" />
            <circle cx={mx} cy={my} r="5" fill="var(--verdigris)"
                    stroke="var(--ground)" strokeWidth="2"
                    vectorEffect="non-scaling-stroke" />
          </g>
        )}
      </svg>
      {/* The step values live in HTML, not in the SVG: the geometry is scaled
          non-uniformly so the steps fill the width, and text inside it would be
          squashed with them. */}
      <div className="beam-caps">
        {steps.map((s) => (
          s.tier.cap_atoms > 0 ? (
            <span key={'cap' + s.i} className="beam-cap"
                  style={{ left: (s.x0 / W) * 100 + '%',
                           top: (s.y - (compactMode ? 15 : 19)) + 'px' }}>
              {compact(s.tier.cap_atoms)}
            </span>
          ) : null
        ))}
      </div>
      <div className="beam-cap-note">up to · USDX per sale</div>
      <div className="beam-labels">
        {tiers.map((t, i) => (
          <div key={t.level}
               className={'beam-label' + (stake >= t.min_stake_atoms ? ' reached' : '')}
               style={{ left: (i / tiers.length) * 100 + '%' }}>
            <b>{t.name}</b>
            <span>{t.min_stake_atoms === 0 ? 'no stake'
                                           : compact(t.min_stake_atoms) + ' SEQ'}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
