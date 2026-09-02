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

Three rules keep the challenge honest:

  * The challenge carries a random nonce and is single use. A signature captured
    off a user's screen cannot be replayed, because the nonce it names is spent
    the moment it is first redeemed.
  * The signed text must be the text Levo issued, word for word. A hostile site
    cannot obtain a Levo login by asking for a signature over "a test message"
    with a nonce hidden in it: the nonce alone is not enough, the whole
    statement has to match.
  * The statement names the site, the purpose and an expiry in text the user
    can read. What is signed is a sentence, not an opaque hash.

One consequence of recovery is worth knowing: a valid signature over ANY text
recovers to SOME key. If a wallet signs a version of the challenge whose bytes
differ from what Levo issued (a trailing newline lost in a shell, a stray
space), the signature is real but the recovered key belongs to nobody. Levo
refuses a text that differs from the issued one, and lets a caller name the
address it signed with so a mismatch is an error rather than a phantom account.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time

import address as ADDR
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
        raw = base64.b64decode(str(signature_b64).strip(), validate=True)
    except Exception:
        raise BadSignature("the signature is not valid base64")
    if len(raw) != 65:
        raise BadSignature("a recoverable signature is 65 bytes; this one is %d" % len(raw))

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


# --- keys and addresses -----------------------------------------------------

def hash160(b):
    return ripemd160(hashlib.sha256(b).digest())


def ripemd160(data):
    """RIPEMD-160, in Python when the interpreter's OpenSSL lacks it.

    Message signing addresses are hashes of keys, and comparing a recovered
    key to the address a user says they signed with needs this hash. Some
    Python builds ship without it, so it is carried here rather than assumed.
    """
    try:
        return hashlib.new("ripemd160", data).digest()
    except (ValueError, TypeError):
        return _ripemd160(data)


def _ripemd160(data):
    def rol(x, n):
        return ((x << n) | (x >> (32 - n))) & 0xffffffff

    K1 = (0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E)
    K2 = (0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0x00000000)
    R1 = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
          7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
          3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12,
          1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2,
          4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13)
    R2 = (5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12,
          6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
          15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13,
          8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14,
          12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11)
    S1 = (11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8,
          7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
          11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5,
          11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12,
          9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6)
    S2 = (8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6,
          9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
          9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5,
          15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8,
          8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11)

    def f(j, x, y, z):
        if j < 16:
            return x ^ y ^ z
        if j < 32:
            return (x & y) | (~x & z)
        if j < 48:
            return (x | ~y) ^ z
        if j < 64:
            return (x & z) | (y & ~z)
        return x ^ (y | ~z)

    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
    msg = bytearray(data)
    bitlen = len(data) * 8
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += struct.pack("<Q", bitlen)
    for off in range(0, len(msg), 64):
        X = struct.unpack("<16I", msg[off:off + 64])
        al, bl, cl, dl, el = h
        ar, br, cr, dr, er = h
        for j in range(80):
            t = (rol((al + f(j, bl, cl, dl) + X[R1[j]] + K1[j // 16]) & 0xffffffff, S1[j]) + el) & 0xffffffff
            al, el, dl, cl, bl = el, dl, rol(cl, 10), bl, t
            t = (rol((ar + f(79 - j, br, cr, dr) + X[R2[j]] + K2[j // 16]) & 0xffffffff, S2[j]) + er) & 0xffffffff
            ar, er, dr, cr, br = er, dr, rol(cr, 10), br, t
        t = (h[1] + cl + dr) & 0xffffffff
        h[1] = (h[2] + dl + er) & 0xffffffff
        h[2] = (h[3] + el + ar) & 0xffffffff
        h[3] = (h[4] + al + br) & 0xffffffff
        h[4] = (h[0] + bl + cr) & 0xffffffff
        h[0] = t
    return struct.pack("<5I", *h)


B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58check_decode(s):
    n = 0
    for ch in s:
        if ch not in B58:
            raise ValueError("not a base58 string")
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    raw = b"\x00" * (len(s) - len(s.lstrip("1"))) + raw
    if len(raw) < 25:
        raise ValueError("too short for a base58check address")
    body, check = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] != check:
        raise ValueError("bad base58 checksum")
    return body


def key_matches_address(pubkey_hex, address, hrp="tb"):
    """Whether a recovered key is the key behind an address the user names.

    `hrp` is the chain's address prefix, used only in the refusals: a reader
    told to find a `tb1q...` address on mainnet is being sent looking for
    something their wallet will never print.

    Message signing works with the key's hash, so a P2PKH (legacy) or P2WPKH
    (bech32, one of the chain's own 1q addresses) can be checked: its
    20-byte program is the
    hash160 of the key. A taproot address carries a tweaked key and cannot be
    checked this way; a confidential address is refused by the decoder.
    Raises ValueError when the address is not one that names a key hash.
    """
    pub = bytes.fromhex(pubkey_hex)
    # A key hash is the hash of a SERIALISATION, and there are two. Every
    # bech32 address uses the compressed one, but a legacy address made before
    # that was settled may hash the uncompressed form, and the signature says
    # nothing about which -- recovery yields the point either way. Both are
    # accepted so that an old address is not called a mismatch.
    wanted = [hash160(pub)]
    try:
        x, y = S.affine(S.decompress(pub))
        wanted.append(hash160(b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")))
    except Exception:
        pass
    want = wanted[0]
    a = str(address or "").strip()
    if not a:
        raise ValueError("no address given")
    try:
        got_hrp, ver, prog = ADDR.decode(a)
    except ValueError as bech_err:
        if "confidential" in str(bech_err):
            raise
        try:
            body = _b58check_decode(a)
        except ValueError:
            raise ValueError("%s is not an address message signing works with "
                             "(a legacy or a %s1q... address)" % (address, hrp))
        # Legacy payloads are <version><20-byte hash>; confidential legacy
        # forms carry a blinding key first, and the hash is still the tail.
        return any(hmac.compare_digest(body[-20:], w) for w in wanted)
    if ver == 0 and len(prog) == 20:
        # A version-0 program is always the compressed key's hash: an
        # uncompressed key cannot be spent from a witness output at all.
        return hmac.compare_digest(bytes(prog), want)
    if ver == 1:
        raise ValueError("%s is a taproot address, which carries a tweaked key that "
                         "message signing cannot be checked against; name the legacy "
                         "or %s1q... address of the key you signed with"
                         % (address, got_hrp or hrp))
    raise ValueError("%s is not an address message signing works with: use the "
                     "legacy or %s1q... address of the key you signed with"
                     % (address, got_hrp or hrp))


# --- challenges -------------------------------------------------------------

def _canonical(text):
    """The text a signature is compared against: line endings normalised and
    trailing whitespace dropped, which is every change a copy through a
    shell or a text box makes to a statement without changing what it says."""
    return "\n".join(line.rstrip() for line in str(text).replace("\r\n", "\n")
                     .replace("\r", "\n").split("\n")).strip()


class Challenges:
    """Single-use login challenges, held in memory with an expiry."""

    MAX_OPEN = 20_000

    def __init__(self, site="Levo", ttl=CHALLENGE_TTL, now=time.time, max_open=None):
        self.site = site
        self.ttl = ttl
        self._now = now
        self._open = {}                 # nonce -> (expires, issued text)
        self.max_open = max_open or self.MAX_OPEN

    def issue(self, purpose="Sign in to Levo", extra_lines=(), origin=None):
        """A fresh statement to sign. It ends without a newline, so the bytes a
        text box holds and the bytes a shell's `$(cat file)` yields are the
        same bytes, and the signature matches either way.

        The statement names the site it signs in to. A wallet shows the text
        it is asked to sign, so naming the origin is what lets the person
        holding the key see WHICH Levo they are signing in to -- and notice
        when the page asking is not that one at all.
        """
        nonce = secrets.token_hex(16)
        issued = self._now()
        expires = issued + self.ttl
        head = [self.site]
        if origin:
            head.append("Site: %s" % str(origin)[:120])
        lines = head + ["", purpose,
                 "This signature proves you control this wallet. It authorises "
                 "no payment and moves no funds.", ""]
        lines += list(extra_lines)
        lines += ["Nonce: %s" % nonce,
                  "Expires: %s UTC" % time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(expires))]
        text = "\n".join(lines)
        self._open[nonce] = (expires, text)
        self._sweep()
        return {"nonce": nonce, "message": text, "expires_at": int(expires)}

    def _sweep(self):
        """Drop expired challenges, and cap what is held.

        Issuing a challenge costs an anonymous caller nothing, so without a
        ceiling the open set is a memory sink anybody can fill. Over the cap the
        oldest go first: they are the closest to expiring anyway, and losing one
        costs its holder a retry rather than anything they hold.
        """
        now = self._now()
        for n, (exp, _) in list(self._open.items()):
            if exp < now:
                del self._open[n]
        excess = len(self._open) - self.max_open
        if excess > 0:
            for n, _ in sorted(self._open.items(), key=lambda kv: kv[1][0])[:excess]:
                del self._open[n]

    def redeem(self, message):
        """Spend the nonce named in `message`, and check the message is the
        statement that was issued with it. Raises if unknown, expired, or
        different.

        Spending happens BEFORE the signature is checked so that a failed
        attempt still burns the nonce; a challenge is worth exactly one try.
        """
        nonce = None
        for line in str(message).splitlines():
            if line.startswith("Nonce: "):
                nonce = line[len("Nonce: "):].strip()
                break
        if not nonce:
            raise ValueError("the signed text carries no nonce; sign the challenge "
                             "Levo issued, exactly as issued")
        entry = self._open.pop(nonce, None)
        if entry is None:
            raise ValueError("unknown or already-used challenge; ask for a new one")
        expires, issued = entry
        if expires < self._now():
            raise ValueError("the challenge has expired; ask for a new one")
        if _canonical(message) != _canonical(issued):
            raise ValueError("the signed text is not the challenge Levo issued. "
                             "Sign the statement exactly as shown; a signature "
                             "over different text logs nobody in")
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
        pk = body.get("pubkey")
        if not isinstance(pk, str) or len(pk) != 66:
            return None
        return pk
