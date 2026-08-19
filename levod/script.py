"""Script assembly and taproot construction, self-contained.

Only what a sale covenant needs: the opcodes its two leaves use, Bitcoin's
minimal push encoding, and the Elements-tagged taproot construction that turns
a pair of leaves into an address.

The encoding rules here are not Levo's to choose -- they are consensus. A push
encoded one byte differently produces a different leaf, a different merkle root,
and a different address, which is why `tests/test_covenant.py` checks every byte
this module produces against the frozen vectors rather than merely checking that
it runs.
"""

import struct

import secp256k1 as S

# Opcodes used by the sale covenant's leaves.
OP_0 = 0x00
OP_1 = 0x51
OP_1NEGATE = 0x4f
OP_IF = 0x63
OP_ELSE = 0x67
OP_ENDIF = 0x68
OP_VERIFY = 0x69
OP_DROP = 0x75
OP_DUP = 0x76
OP_NIP = 0x77
OP_SWAP = 0x7c
OP_ROT = 0x7b
OP_EQUAL = 0x87
OP_EQUALVERIFY = 0x88
OP_1ADD = 0x8b
OP_ADD = 0x93
OP_LESSTHAN = 0x9f
OP_CHECKSIG = 0xac
OP_CHECKLOCKTIMEVERIFY = 0xb1

# Elements introspection and 64-bit arithmetic.
OP_INSPECTINPUTVALUE = 0xc9
OP_INSPECTINPUTSCRIPTPUBKEY = 0xca
OP_PUSHCURRENTINPUTINDEX = 0xcd
OP_INSPECTOUTPUTASSET = 0xce
OP_INSPECTOUTPUTVALUE = 0xcf
OP_INSPECTOUTPUTSCRIPTPUBKEY = 0xd1
OP_INSPECTNUMOUTPUTS = 0xd5
OP_ADD64 = 0xd7
OP_SUB64 = 0xd8
OP_MUL64 = 0xd9
OP_DIV64 = 0xda
OP_GREATERTHANOREQUAL64 = 0xdf

LEAF_VERSION_TAPSCRIPT = 0xc4      # Elements tapscript

# BIP341 nothing-up-my-sleeve point: a taproot internal key with no known
# discrete log, so an output using it has no key-path spend at all.
NUMS = bytes.fromhex("50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0")


def bn2vch(v):
    """A number in Bitcoin's little-endian, sign-in-the-top-bit format."""
    n_bits = v.bit_length() + (v != 0)
    n_bytes = (n_bits + 7) // 8
    encoded = 0 if v == 0 else abs(v) | ((v < 0) << (n_bytes * 8 - 1))
    return encoded.to_bytes(n_bytes, "little")


def push(data):
    """Minimal PUSHDATA encoding for a byte string."""
    n = len(data)
    if n < 0x4c:
        return bytes([n]) + data
    if n <= 0xff:
        return b"\x4c" + bytes([n]) + data
    if n <= 0xffff:
        return b"\x4d" + struct.pack(b"<H", n) + data
    if n <= 0xffffffff:
        return b"\x4e" + struct.pack(b"<I", n) + data
    raise ValueError("data too long to push")


def script(items):
    """Assemble a script from opcodes (ints already in opcode range), integers
    to be pushed as numbers, and byte strings to be pushed as data.

    Integers follow CScript's rule: 0..16 become OP_0/OP_N, -1 becomes
    OP_1NEGATE, anything else is pushed as a minimally-encoded number. Opcodes
    are passed as `Op` so they are never confused with small integers.
    """
    out = bytearray()
    for it in items:
        if isinstance(it, Op):
            out.append(int(it))
        elif isinstance(it, (bytes, bytearray)):
            out += push(bytes(it))
        elif isinstance(it, int):
            if 0 <= it <= 16:
                out.append(OP_0 if it == 0 else OP_1 + it - 1)
            elif it == -1:
                out.append(OP_1NEGATE)
            else:
                out += push(bn2vch(it))
        else:
            raise TypeError("cannot put %r in a script" % (it,))
    return bytes(out)


class Op(int):
    """An opcode. Distinct from int so a small integer is never mistaken for one."""
    __slots__ = ()


def ops(*values):
    return [Op(v) for v in values]


def ser_string(b):
    """Compact-size length prefix followed by the bytes."""
    n = len(b)
    if n < 0xfd:
        return bytes([n]) + b
    if n <= 0xffff:
        return b"\xfd" + struct.pack("<H", n) + b
    if n <= 0xffffffff:
        return b"\xfe" + struct.pack("<I", n) + b
    return b"\xff" + struct.pack("<Q", n) + b


def tapleaf_hash(script_bytes, version=LEAF_VERSION_TAPSCRIPT):
    return S.tagged_hash("TapLeaf/elements", bytes([version]) + ser_string(script_bytes))


def tapbranch_hash(a, b):
    """BIP341 sorts the two children before hashing, so the branch does not
    depend on which side a leaf sits."""
    lo, hi = (a, b) if a < b else (b, a)
    return S.tagged_hash("TapBranch/elements", lo, hi)


class Taptree:
    """A two-leaf taproot commitment: everything needed to fund it and spend it."""

    def __init__(self, internal_key, leaves):
        if len(leaves) != 2:
            raise ValueError("this builder handles exactly two leaves")
        self.internal_key = internal_key
        self.leaves = dict(leaves)
        names = list(self.leaves)
        h = {n: tapleaf_hash(self.leaves[n]) for n in names}
        self.leaf_hashes = h
        self.merkle_root = tapbranch_hash(h[names[0]], h[names[1]])
        self.output_key, negated = S.tweak_add_pubkey(
            internal_key,
            S.tagged_hash("TapTweak/elements", internal_key + self.merkle_root))
        self.negflag = 1 if negated else 0
        # The sibling of each leaf is the other leaf: its control merkle branch.
        self.branches = {names[0]: h[names[1]], names[1]: h[names[0]]}
        self.script_pubkey = bytes([OP_1, 0x20]) + self.output_key

    def control_block(self, leaf_name):
        return (bytes([LEAF_VERSION_TAPSCRIPT + self.negflag])
                + self.internal_key + self.branches[leaf_name])

    def witness(self, leaf_name, *stack):
        """Witness stack for a script-path spend: any leaf inputs, then the leaf
        script itself, then its control block."""
        return list(stack) + [self.leaves[leaf_name], self.control_block(leaf_name)]
