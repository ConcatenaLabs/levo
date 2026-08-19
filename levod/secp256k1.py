"""secp256k1, self-contained.

Levo needs three things from the curve: recover the public key from a wallet's
message signature (that is the login), verify a signature against a key it was
told in advance (that is linking a staking key), and tweak an x-only key by a
taproot merkle root (that is deriving a sale address).

This is deliberately a standalone implementation rather than an import from a
node source tree. Levo talks to a Sequentia node over JSON-RPC and should not
also need a clone of its source checked out beside it.

Standing on its own puts the burden of proof here: `tests/test_crypto.py`
checks this module against BIP340 vectors and against every sale address in
`vectors.json`, so a mistake in the arithmetic shows up as an address mismatch
rather than as a subtly wrong signature check.
"""

import hashlib

P = 2**256 - 2**32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _inv(a, m):
    return pow(a % m, m - 2, m)


# --- points, in Jacobian coordinates -----------------------------------------
# A point is (X, Y, Z) meaning affine (X/Z^2, Y/Z^3); None is the identity.

def _to_jac(p):
    return None if p is None else (p[0], p[1], 1)


def affine(p):
    """(x, y), or None for the point at infinity."""
    if p is None:
        return None
    if len(p) == 2:
        return p
    x, y, z = p
    if z == 0:
        return None
    zi = _inv(z, P)
    zi2 = zi * zi % P
    return (x * zi2 % P, y * zi2 % P * zi % P)


def _double(p):
    if p is None:
        return None
    x, y, z = p
    if y == 0:
        return None
    a = x * x % P
    b = y * y % P
    c = b * b % P
    d = 2 * ((x + b) ** 2 - a - c) % P
    e = 3 * a % P
    f = e * e % P
    nx = (f - 2 * d) % P
    ny = (e * (d - nx) - 8 * c) % P
    nz = 2 * y * z % P
    return (nx, ny, nz)


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    x1, y1, z1 = p
    x2, y2, z2 = q
    z1z1 = z1 * z1 % P
    z2z2 = z2 * z2 % P
    u1 = x1 * z2z2 % P
    u2 = x2 * z1z1 % P
    s1 = y1 * z2 % P * z2z2 % P
    s2 = y2 * z1 % P * z1z1 % P
    if u1 == u2:
        if s1 != s2:
            return None                 # p == -q
        return _double(p)
    h = (u2 - u1) % P
    i = (2 * h) ** 2 % P
    j = h * i % P
    r = 2 * (s2 - s1) % P
    v = u1 * i % P
    nx = (r * r - j - 2 * v) % P
    ny = (r * (v - nx) - 2 * s1 * j) % P
    nz = ((z1 + z2) ** 2 - z1z1 - z2z2) % P * h % P
    return (nx, ny, nz)


def mul(point, k):
    """k * point, for an affine or Jacobian point."""
    k %= N
    if k == 0 or point is None:
        return None
    r = None
    acc = _to_jac(point) if len(point) == 2 else point
    while k:
        if k & 1:
            r = _add(r, acc)
        acc = _double(acc)
        k >>= 1
    return r


def mul_add(pairs):
    """sum(k_i * P_i), used by the verification equation."""
    r = None
    for point, k in pairs:
        r = _add(r, mul(point, k))
    return r


def lift_x(x):
    """The curve point with this x and EVEN y, or None if x is not on the curve."""
    if not (0 <= x < P):
        return None
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if y * y % P != y_sq:
        return None
    return (x, y if y % 2 == 0 else P - y)


def has_even_y(p):
    p = affine(p)
    return p is not None and p[1] % 2 == 0


def negate(p):
    p = affine(p)
    return None if p is None else (p[0], (P - p[1]) % P)


def compressed(p):
    p = affine(p)
    return bytes([0x02 + (p[1] & 1)]) + p[0].to_bytes(32, "big")


def decompress(sec):
    """Parse a 33-byte compressed SEC public key."""
    if len(sec) != 33 or sec[0] not in (0x02, 0x03):
        raise ValueError("not a 33-byte compressed public key")
    p = lift_x(int.from_bytes(sec[1:], "big"))
    if p is None:
        raise ValueError("public key is not on the curve")
    return negate(p) if sec[0] == 0x03 else p


# --- hashing -----------------------------------------------------------------

def tagged_hash(tag, *data):
    t = hashlib.sha256(tag.encode()).digest()
    h = hashlib.sha256(t + t)
    for d in data:
        h.update(d)
    return h.digest()


# --- taproot -----------------------------------------------------------------

def tweak_add_pubkey(xonly, tweak):
    """Return (tweaked x-only key, was_negated), as BIP341 output keys are formed."""
    if len(xonly) != 32 or len(tweak) != 32:
        raise ValueError("x-only key and tweak must both be 32 bytes")
    Pp = lift_x(int.from_bytes(xonly, "big"))
    if Pp is None:
        raise ValueError("internal key is not a valid x-only public key")
    t = int.from_bytes(tweak, "big")
    if t >= N:
        raise ValueError("tweak is out of range")
    Q = affine(_add(mul(G, t), _to_jac(Pp)))
    if Q is None:
        raise ValueError("tweak produced the point at infinity")
    return Q[0].to_bytes(32, "big"), (Q[1] % 2 != 0)


# --- ECDSA -------------------------------------------------------------------

def ecdsa_verify(point, r, s, z):
    if not (1 <= r < N and 1 <= s < N):
        return False
    w = _inv(s, N)
    R = affine(mul_add([(point, r * w % N), (G, z * w % N)]))
    return R is not None and R[0] % N == r


def ecdsa_recover(z, r, s, recid):
    """The public key that produced (r, s) over z, for the given recovery id.

    Returns an affine point. Raises ValueError if the recovery id does not
    describe a point on the curve.
    """
    if not (1 <= r < N and 1 <= s < N):
        raise ValueError("signature r/s out of range")
    if not 0 <= recid <= 3:
        raise ValueError("recovery id out of range")
    x = r + (N if (recid & 2) else 0)
    if x >= P:
        raise ValueError("recovery id implies an x beyond the field")
    R = lift_x(x)
    if R is None:
        raise ValueError("no curve point for the signature's r")
    if recid & 1:
        R = negate(R)
    rinv = _inv(r, N)
    Q = affine(mul_add([(R, s * rinv % N), (G, (N - z % N) * rinv % N)]))
    if Q is None:
        raise ValueError("recovered the point at infinity")
    return Q
