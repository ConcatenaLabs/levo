"""Addresses: turning a scriptPubKey into something a person can check.

A sale's identity is its scriptPubKey, but nobody funds a scriptPubKey by hand.
This encodes the same bytes as the bech32m address a wallet will accept, so the
project can copy one string and the buyer can compare one string.

Sequentia's default addresses are unblinded and use the same bech32/bech32m
format as Bitcoin -- that is the point of being transparent by default, and it
is why a Sequentia address looks like a Bitcoin one. Confidential addresses are
a separate, opt-in format (blech32) and never appear here: a sale covenant's
outputs must be explicit, because the covenant reads them with introspection and
refuses anything it cannot read.
"""

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
BECH32M_CONST = 0x2bc830a3


def _polymod(values):
    gen = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def encode_segwit(hrp, witver, witprog):
    """The bech32 (v0) or bech32m (v1+) address for a witness program."""
    if witver < 0 or witver > 16:
        raise ValueError("witness version out of range")
    if len(witprog) not in (20, 32):
        raise ValueError("witness program must be 20 or 32 bytes")
    data = [witver] + _convertbits(list(witprog), 8, 5)
    const = 1 if witver == 0 else BECH32M_CONST
    values = _hrp_expand(hrp) + data
    polymod = _polymod(values + [0, 0, 0, 0, 0, 0]) ^ const
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(CHARSET[d] for d in data + checksum)


def _bech32_verify(hrp, data):
    """Which checksum a bech32 string carries: 'bech32', 'bech32m' or None."""
    const = _polymod(_hrp_expand(hrp) + data)
    if const == 1:
        return "bech32"
    if const == BECH32M_CONST:
        return "bech32m"
    return None


# The confidential (blech32) prefixes on the chains Levo runs on. A covenant
# reads the outputs it checks, so nothing it builds can be addressed to one.
CONFIDENTIAL_HRPS = ("tsqb", "sqb", "el", "lq", "tlq")


def decode(addr):
    """(hrp, witness version, program bytes) for a bech32 or bech32m address.

    Raises ValueError with a reason a user can act on: a confidential
    address, a checksum that does not match, a program of the wrong length,
    or something that is not an address at all.
    """
    a = str(addr or "").strip()
    if not a:
        raise ValueError("no address given")
    if a != a.lower() and a != a.upper():
        raise ValueError("an address is all lowercase or all uppercase, not mixed")
    a = a.lower()
    pos = a.rfind("1")
    if pos < 1 or pos + 7 > len(a) or len(a) > 90:
        raise ValueError("%r is not a bech32 address" % addr)
    hrp, body = a[:pos], a[pos + 1:]
    if hrp in CONFIDENTIAL_HRPS:
        raise ValueError(
            "%s is a confidential address. Everything a sale covenant touches "
            "has to be explicit, so use an unblinded address (the same wallet "
            "has one; on the Sequentia testnet it begins tb1)" % addr)
    if any(c not in CHARSET for c in body):
        raise ValueError("%r is not a bech32 address" % addr)
    data = [CHARSET.index(c) for c in body]
    kind = _bech32_verify(hrp, data)
    if kind is None:
        raise ValueError("%s has a bad checksum; check it for a typo" % addr)
    witver = data[0]
    prog = _convertbits(data[1:-6], 5, 8, False)
    if prog is None or witver > 16:
        raise ValueError("%s is not a valid witness address" % addr)
    if witver == 0 and kind != "bech32":
        raise ValueError("%s is a version-0 program with a bech32m checksum" % addr)
    if witver != 0 and kind != "bech32m":
        raise ValueError("%s is a version-%d program with a bech32 checksum" % (addr, witver))
    if witver == 0 and len(prog) not in (20, 32):
        raise ValueError("%s carries a witness program of the wrong length" % addr)
    if witver == 1 and len(prog) != 32:
        # A version-1 program of any other length is not taproot; the chain
        # treats it as anyone-can-spend, so paying one is giving the money to
        # whoever spends it first.
        raise ValueError(
            "%s is a version-1 address whose program is %d bytes rather than 32, "
            "which this chain treats as anyone-can-spend" % (addr, len(prog)))
    if witver > 1 and not 2 <= len(prog) <= 40:
        raise ValueError("%s carries a witness program of the wrong length" % addr)
    return hrp, witver, bytes(prog)


def to_script_pubkey(addr, hrp=None):
    """The scriptPubKey bytes an address stands for.

    `hrp`, when given, is the chain's unblinded prefix, and an address from a
    different chain is refused: sending a purchase's tokens to a Bitcoin
    address that happens to share the format would burn them.

    Witness versions above 1 are refused as well. The chain accepts them as
    anyone-can-spend until a future rule gives them meaning, so tokens paid to
    one are not the buyer's, they are the first taker's.
    """
    got_hrp, witver, prog = decode(addr)
    if hrp and got_hrp != hrp:
        raise ValueError(
            "%s is a %s address, but this chain's addresses begin %s1"
            % (addr, got_hrp, hrp))
    if witver > 1:
        raise ValueError(
            "%s is a version-%d witness address, which this chain treats as "
            "anyone-can-spend: whatever is paid to it belongs to whoever "
            "spends it first. Use a %s1q or %s1p address"
            % (addr, witver, got_hrp, got_hrp))
    ver_byte = 0 if witver == 0 else 0x50 + witver
    return bytes([ver_byte, len(prog)]) + prog


def check_script_pubkey(spk_hex, what="that output"):
    """Refuse a raw scriptPubKey Levo should not pay to.

    A caller may give a scriptPubKey instead of an address, which skips every
    check the address form carries. Only the two witness versions the chain
    enforces today are accepted: a version-2 or later program is
    anyone-can-spend, and a bare or non-standard script would not relay.
    """
    text = str(spk_hex or "").lower()
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        raise ValueError("%s: scriptPubKey must be hex" % what)
    if len(raw) == 22 and raw[0] == 0x00 and raw[1] == 0x14:
        return text
    if len(raw) == 34 and raw[0] == 0x00 and raw[1] == 0x20:
        return text
    if len(raw) == 34 and raw[0] == 0x51 and raw[1] == 0x20:
        return text
    if len(raw) >= 2 and raw[0] in range(0x52, 0x61):
        raise ValueError(
            "%s: that is a version-%d witness program, which this chain treats "
            "as anyone-can-spend -- whatever is paid to it belongs to whoever "
            "spends it first" % (what, raw[0] - 0x50))
    raise ValueError(
        "%s: Levo pays to witness outputs -- a version-0 key or script hash, or "
        "a taproot output. Give the address your wallet shows instead" % what)


def witness_program(addr, hrp=None):
    """(witness version, program hex) for an address Levo can pay to.

    A sale's treasury may be either kind of witness output, so what matters is
    which one it is -- the version goes into the leaf beside the program.
    """
    got_hrp, witver, prog = decode(addr)
    if hrp and got_hrp != hrp:
        raise ValueError(
            "%s is a %s address, but this chain's addresses begin %s1"
            % (addr, got_hrp, hrp))
    if witver > 1:
        raise ValueError(
            "%s is a version-%d witness address, which this chain treats as "
            "anyone-can-spend: whatever is paid to it belongs to whoever spends "
            "it first" % (addr, witver))
    if witver == 0 and len(prog) not in (20, 32):
        raise ValueError("%s carries a witness program of the wrong length" % addr)
    if witver == 1 and len(prog) != 32:
        raise ValueError("%s is not a taproot output" % addr)
    return witver, prog.hex()


def taproot_program(addr, hrp=None):
    """The 32-byte program of a taproot (witness v1) address, as hex."""
    got_hrp, witver, prog = decode(addr)
    if hrp and got_hrp != hrp:
        raise ValueError(
            "%s is a %s address, but this chain's addresses begin %s1"
            % (addr, got_hrp, hrp))
    if witver != 1 or len(prog) != 32:
        raise ValueError(
            "%s is not a taproot address. The treasury has to be one, because "
            "the covenant checks a 32-byte version-1 program; ask your wallet "
            "for a taproot (bech32m, %s1p...) address" % (addr, got_hrp))
    return prog.hex()


def from_script_pubkey(spk, hrp="tb"):
    """The address for a witness scriptPubKey, or None if it is not one.

    `hrp` is the chain's unblinded prefix: `tb` on the Sequentia testnet, `bc`
    on mainnet, `ert` on the regtest the functional tests use.
    """
    b = bytes.fromhex(spk) if isinstance(spk, str) else bytes(spk)
    if len(b) < 4 or len(b) > 42:
        return None
    ver_byte = b[0]
    if ver_byte == 0:
        witver = 0
    elif 0x51 <= ver_byte <= 0x60:
        witver = ver_byte - 0x50
    else:
        return None
    if b[1] != len(b) - 2:
        return None
    prog = b[2:]
    if len(prog) not in (20, 32):
        return None
    return encode_segwit(hrp, witver, prog)


# The address prefix each chain Levo knows uses. Sequentia's unblinded
# addresses are Bitcoin's own format, so its prefixes are Bitcoin's.
CHAIN_HRP = {"sequentia": "bc", "main": "bc", "test": "tb", "testnet": "tb",
             "regtest": "ert", "elementsregtest": "ert"}


def hrp_for(chain, default="tb"):
    """The address prefix for a chain by name.

    A chain nobody named here gets `default`, and the caller decides what that
    is: guessing "tb" for an unknown mainnet-like chain would encode every
    address on it with a testnet prefix, and a wallet would refuse them all.
    """
    return CHAIN_HRP.get(str(chain).lower(), default)
