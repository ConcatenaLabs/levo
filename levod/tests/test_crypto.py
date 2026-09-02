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


def test_wallet_staking_signature_vector(t):
    """A signature the WALLET made with its staking key must recover to that key.

    This is one-click login in one assertion. The Sequentia browser wallet signs
    with the key at m/2/0 -- the key a stake is bonded to -- and Levo recovers
    exactly that key, so the account it creates is the one the stake belongs to
    and no linking step is needed.

    The second half matters as much: the SAME wallet signing the SAME text with
    its master key recovers to a different key entirely. That is why signing in
    with `signMessage` leaves a staker with no tier, and why the wallet needed a
    staking-key signer at all.

    Produced by lwk_wasm (Rust) from the BIP39 test mnemonic
    "abandon abandon ... about", verified here by Levo's own recovery (Python).
    """
    challenge = "Levo\n\nSign in to Levo\nNonce: 0123456789abcdef\n"
    staker_pubkey = "021b9257d07f88afb539909c87657aabebd95a324fb8dd900429bbb9e64ba20f50"
    staker_sig = ("H7qvxmJssLm46icRR3ZhYpsPU4zdi7A02okhgwuzrWnGIz/fj985MB6hAegV"
                  "3ato0RRITRgpVV2CmLV7tYK3foQ=")
    master_sig = ("H3a1cjdHILChPJxR81XExHEa07wa9lf4CF/jPhCrUS/yU1maG1mjwoPEIFEP"
                  "AvBSbSfSn/heN7PsdZjEqVLT+R8=")

    t.eq(auth.recover_pubkey(challenge, staker_sig), staker_pubkey,
         "the wallet's staking signature recovers to its staking key")
    t.ok(auth.recover_pubkey(challenge, master_sig) != staker_pubkey,
         "and the master key is a different key, which is the whole problem")
    t.ok(auth.verify_signature(challenge, staker_sig, staker_pubkey),
         "so a staking key can be linked from a wallet signature alone")


def test_schnorr_against_the_bip340_vectors(t):
    """The reclaim is signed with BIP340. The node's functional suite carries
    the official vectors; the first signing vectors are pinned here so the
    signer is checked against the standard, not against its own verifier."""
    vectors = [
        # (secret, pubkey, aux, message, signature)
        ("0000000000000000000000000000000000000000000000000000000000000003",
         "F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9",
         "0000000000000000000000000000000000000000000000000000000000000000",
         "0000000000000000000000000000000000000000000000000000000000000000",
         "E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA821525F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0"),
        ("B7E151628AED2A6ABF7158809CF4F3C762E7160F38B4DA56A784D9045190CFEF",
         "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
         "0000000000000000000000000000000000000000000000000000000000000001",
         "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
         "6896BD60EEAE296DB48A229FF71DFE071BDE413E6D43F917DC8DCF8C78DE33418906D11AC976ABCCB20B091292BFF4EA897EFCB639EA871CFA95F6DE339E4B0A"),
        ("C90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B14E5C9",
         "DD308AFEC5777E13121FA72B9CC1B7CC0139715309B086C960E18FD969774EB8",
         "C87AA53824B4D7AE2EB035A2B5BBBCCC080E76CDC6D1692C4B0B62D798E6D906",
         "7E2D58D8B3BCDF1ABADEC7829054F90DDA9805AAB56C77333024B9D0A508B75C",
         "5831AAEED7B44BB74E5EAB94BA9D4294C49BCF2A60728D8B4C200F50DD313C1BAB745879A5AD954A72C45A91C3A51D3C7ADEA98D82F8481E0E1E03674A6F3FB7"),
        ("0B432B2677937381AEF05BB02A66ECD012773062CF3FA2549E44F58ED2401710",
         "25D1DFF95105F5253C4022F628A996AD3A0D95FBF21D468A1B33F8C160D8F517",
         "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
         "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
         "7EB0509757E246F19449885651611CB965ECC1A187DD51B64FDA1EDC9637D5EC97582B9CB13DB3933705B32BA982AF5AF25FD78881EBB32771FC5922EFC66EA3"),
    ]
    for i, (sec, pub, aux, msg, sig) in enumerate(vectors):
        secret = int(sec, 16)
        t.eq(S.xonly_pubkey(secret).hex(), pub.lower(), "vector %d public key" % i)
        got = S.schnorr_sign(bytes.fromhex(msg), secret, aux=bytes.fromhex(aux))
        t.eq(got.hex(), sig.lower(), "vector %d signature" % i)
        t.ok(S.schnorr_verify(bytes.fromhex(msg), got, bytes.fromhex(pub)), "vector %d verifies" % i)
    # The BIP's negative vectors: a signature must fail on another key or message.
    msg = bytes.fromhex(vectors[1][3])
    sig = bytes.fromhex(vectors[1][4])
    t.ok(not S.schnorr_verify(msg, sig, bytes.fromhex(vectors[2][1])), "wrong key fails")
    t.ok(not S.schnorr_verify(b"\x00" * 32, sig, bytes.fromhex(vectors[1][1])), "wrong message fails")
    t.ok(not S.schnorr_verify(msg, sig[:-1] + b"\x00", bytes.fromhex(vectors[1][1])), "damaged signature fails")


def test_bip341_tweak_vector(t):
    """The taproot tweak that turns the NUMS key and a leaf tree into an
    address, checked against BIP341's key-path vector for an empty tree."""
    import script as K
    internal = bytes.fromhex("d6889cb081036e0faefa3a35157ad71086b123b2b144b649798b494c300a961d")
    # BIP341 vector 0 uses Bitcoin's tag; the arithmetic is what is pinned here,
    # so tweak with Bitcoin's tag and compare to the BIP's output key.
    tweak = S.tagged_hash("TapTweak", internal)
    out, _ = S.tweak_add_pubkey(internal, tweak)
    t.eq(out.hex(), "53a1f6e454df1aa2776a2814a721372d6258050de330b3c6d10ee8f4e0dda343",
         "BIP341 key-path vector 0 output key")
