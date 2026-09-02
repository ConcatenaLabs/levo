"""Address checks: the encoder against an address a live node printed, and the
decoder against the encoder, the reference vectors, and the mistakes a user
can make -- a confidential address, another chain's address, a typo."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import address as A  # noqa: E402

# The Helios Grid sale on the Sequentia testnet: the node printed this address
# for this scriptPubKey.
LIVE_SPK = "512096ec3dd929d60f1bdaca389a5461e2b044dc1c41561bcd92e3897327781c96a1"
LIVE_ADDR = "tb1pjmkrmkff6c83hkk28zd9gc0zkpzdc8zp2cdumyhr39ejw7quj6ssw5ug74"


def test_encoder_matches_the_node(t):
    t.eq(A.from_script_pubkey(LIVE_SPK, "tb"), LIVE_ADDR, "bech32m matches a live node")
    t.eq(A.from_script_pubkey("0014" + "ab" * 20, "tb")[:4], "tb1q", "v0 is bech32")
    t.eq(A.from_script_pubkey("6a04deadbeef", "tb"), None, "an OP_RETURN is not an address")


def test_decoder_inverts_the_encoder(t):
    for spk in (LIVE_SPK, "0014" + "ab" * 20, "0020" + "cd" * 32, "5120" + "ef" * 32):
        for hrp in ("tb", "bc", "ert"):
            addr = A.from_script_pubkey(spk, hrp)
            t.eq(A.to_script_pubkey(addr, hrp).hex(), spk, "round trip %s %s" % (hrp, spk[:6]))
    t.eq(A.to_script_pubkey(LIVE_ADDR.upper(), "tb").hex(), LIVE_SPK, "uppercase is accepted")


def test_reference_vectors(t):
    # BIP173 / BIP350 vectors: a v0 P2WPKH and a v1 P2TR.
    t.eq(A.to_script_pubkey("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4").hex(),
         "0014751e76e8199196d454941c45d1b3a323f1433bd6", "BIP173 v0 vector")
    t.eq(A.to_script_pubkey("bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0").hex(),
         "512079be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798",
         "BIP350 v1 vector")


def test_user_mistakes_get_a_reason(t):
    def refuses(addr, needle, what, hrp=None):
        try:
            A.to_script_pubkey(addr, hrp)
            t.ok(False, what + " (accepted)")
        except ValueError as e:
            t.ok(needle in str(e), what, str(e))

    refuses("tsqb1qqfw9w0z4lq8lqm0ma5m0q3e6rnwm6wqhhk9n4l0u3f6yg9d3rxkcpxyzhsrkcqnfcd0",
            "confidential", "a blinded address is refused with the reason")
    refuses(LIVE_ADDR[:-1] + "5", "checksum", "a typo is caught")
    refuses("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "begin tb1",
            "another chain's address is refused", hrp="tb")
    refuses("hello", "not a bech32", "a non-address is refused")
    refuses("", "no address", "an empty address is refused")
    refuses("tb1Qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx", "mixed", "mixed case is refused")


def test_treasury_must_be_taproot(t):
    t.eq(A.taproot_program(LIVE_ADDR, "tb"), LIVE_SPK[4:], "a v1 program comes back as hex")
    try:
        A.taproot_program(A.from_script_pubkey("0014" + "ab" * 20, "tb"), "tb")
        t.ok(False, "a v0 treasury address is refused")
    except ValueError as e:
        t.ok("taproot" in str(e), "a v0 treasury address is refused with the reason", str(e))


def test_hrp_for_chain(t):
    t.eq(A.hrp_for("test"), "tb", "testnet")
    t.eq(A.hrp_for("sequentia"), "bc", "mainnet")
    t.eq(A.hrp_for("elementsregtest"), "ert", "regtest")
    t.eq(A.hrp_for("some-new-chain"), "tb", "an unknown chain gets the default")
    t.eq(A.hrp_for("some-new-chain", default=None), None,
         "and a caller that would rather know says so")
