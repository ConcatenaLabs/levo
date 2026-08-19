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


def hrp_for(chain):
    return {"sequentia": "sq", "main": "bc", "test": "tb",
            "testnet": "tb", "regtest": "ert",
            "elementsregtest": "ert"}.get(str(chain).lower(), "tb")
