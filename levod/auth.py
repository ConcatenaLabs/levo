"""Levo login: prove control of a key by signing a challenge.

There is no password, no email, and no account record a user has to create. A
Levo account IS a secp256k1 public key, and you are logged in for exactly as
long as you can produce a signature under it.

The wallet signs with the same scheme Sequentia and Bitcoin wallets already
implement -- a 65-byte recoverable ECDSA signature over

    sha256d( varstr("Bitcoin Signed Message:\\n") || varstr(message) )

returned base64 -- which is what `window.sequentia.signMessage` and the node's
`signmessage` RPC both produce. Because the signature is RECOVERABLE, Levo does
not need to be told which key signed: it recovers the key from the signature,
and that key is the account. Nothing is registered in advance, and a forged
"login as someone else" would require forging a signature.

Two rules keep the challenge honest:

  * The challenge carries a random nonce and is single use. A signature captured
    off a user's screen cannot be replayed, because the nonce it names is spent
    the moment it is first redeemed.
  * The challenge names the site and an expiry in its human-readable text. The
    user is signing a statement they can read, not an opaque hash, so a hostile
    site cannot get a Levo login signed by asking for "a test message".
"""

import base64
import hashlib
import hmac
import json
import os
import secrets

import time



import secp256k1 as S

MESSAGE_MAGIC = b"Bitcoin Signed Message:\n"

CHALLENGE_TTL = 300          # seconds a challenge stays redeemable
SESSION_TTL = 12 * 3600      # seconds a session token stays valid


# --- message hashing --------------------------------------------------------

def _varstr(b):
    n = len(b)
    if n < 0xfd:
        prefix = bytes([n])
    elif n <= 0xffff:
        prefix = b"\xfd" + n.to_bytes(2, "little")
    elif n <= 0xffffffff:
        prefix = b"\xfe" + n.to_bytes(4, "little")
    else:
        prefix = b"\xff" + n.to_bytes(8, "little")
    return prefix + b


def message_hash(message):
    """The 32-byte digest a wallet actually signs for a text message."""
    if isinstance(message, str):
        message = message.encode("utf-8")
    payload = _varstr(MESSAGE_MAGIC) + _varstr(message)
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()


# --- signature recovery -----------------------------------------------------

class BadSignature(ValueError):
    """The signature is malformed, or does not sign this message."""


def recover_pubkey(message, signature_b64):
    """Recover the compressed public key that signed `message`.

    Returns 33-byte compressed SEC hex. Raises BadSignature if the signature is
    not a well-formed recoverable signature over this exact message.
    """
    try:
        raw = base64.b64decode(signature_b64, validate=True)
    except Exception:
        raise BadSignature("signature is not valid base64")
    if len(raw) != 65:
        raise BadSignature("recoverable signature must be 65 bytes, got %d" % len(raw))

    header = raw[0]
    if not (27 <= header <= 34):
        raise BadSignature("signature header byte %d out of range" % header)
    recid = (header - 27) & 3

    r = int.from_bytes(raw[1:33], "big")
    s = int.from_bytes(raw[33:65], "big")
    if not (1 <= r < S.N) or not (1 <= s < S.N):
        raise BadSignature("signature r/s out of range")

    z = int.from_bytes(message_hash(message), "big")

    try:
        Q = S.ecdsa_recover(z, r, s, recid)
    except ValueError as e:
        raise BadSignature(str(e))

    if not S.ecdsa_verify(Q, r, s, z):
        raise BadSignature("signature does not verify against the recovered key")

    return S.compressed(Q).hex()


def verify_signature(message, signature_b64, expect_pubkey):
    """Check that `signature_b64` over `message` was made by `expect_pubkey`.

    Used where the key is known in advance -- linking a staking key to an
    account -- as opposed to login, where the key is discovered by recovery.
    """
    got = recover_pubkey(message, signature_b64)
    want = str(expect_pubkey).lower()
    if len(want) == 66 and want.startswith(("02", "03")):
        return hmac.compare_digest(got, want)
    raise ValueError("expected a 33-byte compressed public key in hex")


# --- challenges -------------------------------------------------------------

class Challenges:
    """Single-use login challenges, held in memory with an expiry."""

    def __init__(self, site="Levo", ttl=CHALLENGE_TTL, now=time.time):
        self.site = site
        self.ttl = ttl
        self._now = now
        self._open = {}

    def issue(self, purpose="Sign in to Levo"):
        nonce = secrets.token_hex(16)
        issued = self._now()
        expires = issued + self.ttl
        text = (
            "%s\n\n"
            "%s\n"
            "This signature proves you control this wallet. It authorises no "
            "payment and moves no funds.\n\n"
            "Nonce: %s\n"
            "Expires: %s UTC\n"
        ) % (self.site, purpose,
             nonce, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(expires)))
        self._open[nonce] = expires
        self._sweep()
        return {"nonce": nonce, "message": text, "expires_at": int(expires)}

    def _sweep(self):
        now = self._now()
        for n, exp in list(self._open.items()):
            if exp < now:
                del self._open[n]

    def redeem(self, message):
        """Spend the nonce named in `message`. Raises if unknown or expired.

        Spending happens BEFORE the signature is checked so that a failed
        attempt still burns the nonce; a challenge is worth exactly one try.
        """
        nonce = None
        for line in str(message).splitlines():
            if line.startswith("Nonce: "):
                nonce = line[len("Nonce: "):].strip()
                break
        if not nonce:
            raise ValueError("challenge text carries no nonce")
        expires = self._open.pop(nonce, None)
        if expires is None:
            raise ValueError("unknown or already-used challenge")
        if expires < self._now():
            raise ValueError("challenge has expired")
        return nonce


# --- sessions ---------------------------------------------------------------

class Sessions:
    """Stateless bearer tokens: payload.hmac, signed with a server secret.

    Stateless because there is nothing worth storing -- the account is a public
    key, and a token that expires is simply a claim that has run out.
    """

    def __init__(self, secret=None, ttl=SESSION_TTL, now=time.time):
        self.secret = secret or os.environ.get("LEVOD_SECRET") or secrets.token_hex(32)
        self.ttl = ttl
        self._now = now

    def issue(self, pubkey):
        body = {"pubkey": pubkey, "exp": int(self._now() + self.ttl)}
        raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        b = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        mac = hmac.new(self.secret.encode(), b.encode(), hashlib.sha256).hexdigest()[:32]
        return "%s.%s" % (b, mac)

    def verify(self, token):
        """Return the account pubkey for a valid token, else None."""
        if not token or "." not in token:
            return None
        b, mac = token.rsplit(".", 1)
        want = hmac.new(self.secret.encode(), b.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(mac, want):
            return None
        try:
            pad = "=" * (-len(b) % 4)
            body = json.loads(base64.urlsafe_b64decode(b + pad))
        except Exception:
            return None
        if int(body.get("exp", 0)) < self._now():
            return None
        return body.get("pubkey")
