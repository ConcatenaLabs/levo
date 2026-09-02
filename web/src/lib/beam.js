// The beam's geometry, kept apart from React so it can be tested on its own.

// The x axis is piecewise: each tier occupies an equal span, INCLUDING the
// top one. A true linear axis would collapse the lower tiers into the left
// edge, since the thresholds are orders of magnitude apart, and a top tier
// with no width would draw everybody at the edge and show the tier not at all.
export function xFor(stakeAtoms, stops) {
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

// A square-root height scale: the caps span two orders of magnitude, and on a
// linear scale the first tier's step would be invisible.
export function yFor(cap, maxCap, top, bottom) {
  const f = Math.sqrt(Math.max(0, cap) / Math.max(1, maxCap))
  return bottom - f * (bottom - top)
}
