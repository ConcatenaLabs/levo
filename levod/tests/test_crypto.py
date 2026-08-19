"""Crypto checks: the curve, the message hash, and the login primitives.

Levo carries its own secp256k1 rather than importing one from a node source
tree, so these tests are the evidence that doing so was safe. The load-bearing
one is `test_node_signmessage_vector`: it takes a signature produced by
Sequentia's own `signmessagewithprivkey` and checks that Levo recovers exactly
the key that made it. If that passes, a real wallet's real signature logs in.
"""

import base64
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import auth  # noqa: E402
import secp256k1 as S  # noqa: E402
import signhelper as SH  # noqa: E402

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(s):
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + b


def test_node_signmessage_vector(t):
    """A signature made by the node's signmessagewithprivkey must recover the
    key that made it. Vector from test/functional/rpc_signmessagewithprivkey.py.
    """
    # A published test key from the node's own functional suite, not a key
    # anybody holds value under. It is here because the signature below was
    # produced by it, and reproducing that signature is the point.
    wif = "cUUuHBXPzhaFnny2gCBzZZzYtFyps9B1sDJbtJMC8ssjUhNMq9xk"
    message = "This is just a test message"
    signature = ("H9Dk+Y13ybD+hH7okYJemWs0N9cIJ23Zn5T+lDFo+ZO3G9QSb6Qla2TATXFji"
                 "29uip6vKsi8TJRZQHbCTu85/74=")
    raw = _b58decode(wif)
    body = raw[:-4]
    t.ok(hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] == raw[-4:],
         "WIF checksum")
    secret = int.from_bytes(body[1:33], "big")
    expected = S.compressed(S.mul(S.G, secret)).hex()
    t.eq(auth.recover_pubkey(message, signature), expected,
         "recovers the node's own signmessage vector")


def test_curve_basics(t):
    t.eq(S.affine(S.mul(S.G, 1)), S.G, "1*G is G")
    t.eq(S.mul(S.G, 0), None, "0*G is the identity")
    t.eq(S.affine(S.mul(S.G, S.N - 1)), S.negate(S.G), "(n-1)*G is -G")
    t.eq(S.decompress(S.compressed(S.G)), S.G, "compress/decompress round trip")
    # Associativity across a few random-ish scalars.
    for a, b in [(3, 5), (12345, 67890), (S.N // 3, S.N // 7)]:
        left = S.affine(S._add(S._to_jac(S.affine(S.mul(S.G, a))),
                               S._to_jac(S.affine(S.mul(S.G, b)))))
        t.eq(left, S.affine(S.mul(S.G, (a + b) % S.N)), "aG + bG == (a+b)G")
    t.eq(S.lift_x(0), None, "x=0 is not on the curve")


def test_message_hash_shape(t):
    """The digest is sha256d over a compact-size-prefixed magic and message,
    which is what makes it compatible with every Bitcoin-style wallet."""
    msg = b"hello"
    expect = hashlib.sha256(hashlib.sha256(
        bytes([24]) + b"Bitcoin Signed Message:\n" + bytes([5]) + msg).digest()).digest()
    t.eq(auth.message_hash("hello"), expect, "message hash matches the magic form")


def test_recovery_round_trip(t):
    for sec in (1, 0xdeadbeef, S.N - 2):
        msg = "Levo\n\nNonce: %d\n" % sec
        sig = SH.sign_recoverable(sec, msg)
        t.eq(auth.recover_pubkey(msg, sig), SH.pubkey_of(sec),
             "round trip for secret %#x" % sec)


def test_recovery_rejects_garbage(t):
    msg = "Levo\n\nNonce: x\n"
    for bad, why in [
        ("not base64!!", "non-base64"),
        (base64.b64encode(b"\x1f" + b"\x00" * 64).decode(), "zero r and s"),
        (base64.b64encode(b"\x63" + b"\x11" * 64).decode(), "header out of range"),
        (base64.b64encode(b"\x1f" + b"\x11" * 40).decode(), "wrong length"),
    ]:
        try:
            auth.recover_pubkey(msg, bad)
            t.ok(False, "should have rejected %s" % why)
        except auth.BadSignature:
            t.ok(True, "rejects %s" % why)


def test_tampering_yields_a_different_account(t):
    """Recovery always yields SOME key, so the security property is that a
    tampered message recovers a key the attacker does not control -- never the
    victim's."""
    sec = 0xabcdef
    msg = "Levo\n\nNonce: aaaa\n"
    sig = SH.sign_recoverable(sec, msg)
    t.ok(auth.recover_pubkey(msg + " ", sig) != SH.pubkey_of(sec),
         "a tampered message cannot recover the original signer")


def test_verify_signature_binds_to_a_named_key(t):
    sec = 0x5150
    msg = "Levo\n\nNonce: bbbb\n"
    sig = SH.sign_recoverable(sec, msg)
    t.ok(auth.verify_signature(msg, sig, SH.pubkey_of(sec)), "accepts the right key")
    t.ok(not auth.verify_signature(msg, sig, "02" + "11" * 32), "rejects another key")


def test_challenges_are_single_use_and_expire(t):
    c = auth.Challenges()
    ch = c.issue()
    t.ok("Nonce: " in ch["message"], "challenge carries a nonce")
    t.ok("authorises no payment" in ch["message"], "challenge says what it is not")
    c.redeem(ch["message"])
    try:
        c.redeem(ch["message"])
        t.ok(False, "replay should be refused")
    except ValueError:
        t.ok(True, "a challenge is single use")

    clock = [1000.0]
    c2 = auth.Challenges(ttl=10, now=lambda: clock[0])
    m = c2.issue()["message"]
    clock[0] += 11
    try:
        c2.redeem(m)
        t.ok(False, "expired challenge should be refused")
    except ValueError:
        t.ok(True, "a challenge expires")


def test_sessions(t):
    s = auth.Sessions(secret="a" * 32)
    pk = "02" + "cd" * 32
    tok = s.issue(pk)
    t.eq(s.verify(tok), pk, "a valid token names its account")
    t.eq(s.verify(tok[:-1] + ("0" if tok[-1] != "0" else "1")), None,
         "a tampered token is refused")
    t.eq(auth.Sessions(secret="b" * 32).verify(tok), None,
         "a token from another secret is refused")
    clock = [1000.0]
    s2 = auth.Sessions(secret="c" * 32, ttl=5, now=lambda: clock[0])
    tok2 = s2.issue(pk)
    clock[0] += 6
    t.eq(s2.verify(tok2), None, "a session expires")
