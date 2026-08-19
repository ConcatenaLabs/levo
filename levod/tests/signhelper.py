"""A recoverable-signature signer, for tests only.

Levo's backend never signs anything -- it only verifies. But to test recovery we
need signatures that a real wallet would produce, so this reproduces the
65-byte compact form (header || r || s) that Bitcoin-style message signing uses.
"""
import base64
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import auth  # noqa: E402

import secp256k1 as S

G = S.G
N = S.N


def modinv(a, m):
    return pow(a % m, m - 2, m)


def pubkey_of(secret):
    return S.compressed(S.mul(G, secret)).hex()


def sign_recoverable(secret, message, compressed=True):
    z = int.from_bytes(auth.message_hash(message), "big")
    # Deterministic k, RFC6979-ish: enough for a test, never used to sign value.
    k = int.from_bytes(
        hashlib.sha256(secret.to_bytes(32, "big") + z.to_bytes(32, "big")).digest(),
        "big") % N
    if k == 0:
        k = 1
    R = S.affine(S.mul(G, k))
    r = R[0] % N
    s = (modinv(k, N) * (z + secret * r)) % N
    recid = (0 if (R[1] & 1) == 0 else 1) | (2 if R[0] >= N else 0)
    if s > N // 2:                      # low-s normalisation flips the y parity
        s = N - s
        recid ^= 1
    header = 27 + recid + (4 if compressed else 0)
    return base64.b64encode(bytes([header]) + r.to_bytes(32, "big")
                            + s.to_bytes(32, "big")).decode()
