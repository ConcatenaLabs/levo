"""Login checks beyond recovery: the challenge binds the signature to the
text Levo issued, and a caller can name the address it signed with."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import address as ADDR  # noqa: E402
import auth  # noqa: E402
import signhelper as SH  # noqa: E402
import tiers as T  # noqa: E402

SEC = 0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef
G_PUB = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"


def test_ripemd160_matches_the_reference_vectors(t):
    for msg, want in ((b"", "9c1185a5c5e9fc54612808977ee8f548b2258d31"),
                      (b"abc", "8eb208f7e05d987a9b044a8e98c6b087f15a0bfc"),
                      (b"message digest", "5d0689ef49d2fae572b881b123a85ffa21595f36"),
                      (b"a" * 1000, "aa69deee9a8922e92f8105e007f76110f381e9cf")):
        t.eq(auth._ripemd160(msg).hex(), want, "ripemd160 of %r" % msg[:12])
    t.eq(auth.ripemd160(b"abc"), auth._ripemd160(b"abc"), "the fallback agrees with the library")


def test_a_key_matches_its_addresses(t):
    """BIP173's P2WPKH vector is hash160(G); the legacy form of the same key
    is the well-known 1BgGZ9... address."""
    t.ok(auth.key_matches_address(G_PUB, "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"),
         "a bech32 v0 address of the key matches")
    t.ok(auth.key_matches_address(G_PUB, "1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH"),
         "a legacy address of the key matches")
    t.ok(not auth.key_matches_address(SH.pubkey_of(SEC), "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"),
         "another key does not")
    for addr, needle in (("bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0", "taproot"),
                         ("hello", "not an address"),
                         ("tsqb1qqfw9w0z4lq8lqm0ma5m0q3e6rnwm6wqhhk9n4l0u3f6yg9d3rxkcpxyzhsrkcqnfcd0", "confidential")):
        try:
            auth.key_matches_address(G_PUB, addr)
            t.ok(False, "refuses %s" % needle)
        except ValueError as e:
            t.ok(needle.split()[0] in str(e).lower() or needle in str(e).lower(),
                 "refuses a %s address with a reason" % needle, str(e))


def test_the_challenge_binds_the_whole_statement(t):
    c = auth.Challenges(now=lambda: 1000.0)
    ch = c.issue()
    t.ok(not ch["message"].endswith("\n"), "issued without a trailing newline")
    t.ok(ch["message"].startswith("Levo\n\nSign in to Levo\n"), "and names the site and purpose")
    nonce_line = [l for l in ch["message"].splitlines() if l.startswith("Nonce")][0]
    try:
        c.redeem("Please approve this test\n" + nonce_line)
        t.ok(False, "other text carrying the nonce is refused")
    except ValueError as e:
        t.ok("not the challenge" in str(e), "other text carrying the nonce is refused")
    ch = c.issue()
    try:
        c.redeem(ch["message"].replace("Levo", "Evil", 1))
        t.ok(False, "an edited statement is refused")
    except ValueError:
        t.ok(True, "an edited statement is refused")
    ch = c.issue()
    t.eq(c.redeem(ch["message"] + "\n"), ch["nonce"], "a trailing newline added in transit is fine")
    ch = c.issue()
    t.eq(c.redeem(ch["message"].replace("\n", "\r\n")), ch["nonce"], "CRLF line endings are fine")
    ch = c.issue()
    t.eq(c.redeem("  " + ch["message"] + "  "), ch["nonce"], "surrounding whitespace is fine")


def test_a_link_statement_is_issued_the_same_way(t):
    c = auth.Challenges(now=lambda: 1000.0)
    acct, staker = "02" + "11" * 32, "03" + "22" * 32
    ch = c.issue(purpose=T.StakeLinks.PURPOSE, extra_lines=T.StakeLinks.binding_lines(acct, staker))
    t.ok("Account: %s\nStaking key: %s\nNonce: " % (acct, staker) in ch["message"],
         "the link statement names both parties before the nonce")
    t.eq(T.StakeLinks.binding_statement(acct, staker, ch["nonce"]),
         "\n".join(ch["message"].splitlines()[:-1]),
         "the composed statement matches the issued one, expiry aside")
    try:
        c.redeem(ch["message"].replace(staker, "03" + "33" * 32))
        t.ok(False, "a statement with the staking key swapped is refused")
    except ValueError:
        t.ok(True, "a statement with the staking key swapped is refused")


def test_sessions_reject_a_malformed_pubkey_claim(t):
    s = auth.Sessions(secret="s", now=lambda: 1000.0)
    tok = s.issue("02" + "ab" * 32)
    t.eq(s.verify(tok), "02" + "ab" * 32, "a good token verifies")
    import base64, hmac, hashlib, json
    body = base64.urlsafe_b64encode(json.dumps({"pubkey": 5, "exp": 2000}).encode()).decode().rstrip("=")
    mac = hmac.new(b"s", body.encode(), hashlib.sha256).hexdigest()[:32]
    t.eq(s.verify(body + "." + mac), None, "a token whose pubkey is not a key is refused")


def test_rate_limit_is_a_token_bucket_per_client(t):
    import server
    rl = server.RateLimit(per_minute=6)
    now = 1000.0
    allowed = [rl.allow("a", now) for _ in range(8)]
    t.eq(allowed, [True] * 6 + [False, False], "six in a burst, then no more")
    t.ok(rl.allow("b", now), "another client has its own bucket")
    t.ok(rl.allow("a", now + 10), "and a tenth of a minute later one token is back")
    t.ok(not rl.allow("a", now + 10), "but only one")
