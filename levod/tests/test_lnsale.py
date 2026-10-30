"""The Lightning sale's scripts: frozen addresses and the refusals.

A Lightning sale's addresses are derived from the scripts in lnsale.py. A lot
locked under one byte of difference sits at an address the new bytes no
longer derive, so the addresses are frozen here. A mismatch is a migration,
never a refresh: see CLAUDE.md on vectors. test_lnsale_node.py proves what the
scripts allow and refuse on a real chain.
"""

import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import covenant as C  # noqa: E402
import lnsale as L  # noqa: E402
import secp256k1 as EC  # noqa: E402

R = EC.xonly_pubkey(int("11" * 32, 16))
RECLAIM = EC.xonly_pubkey(int("22" * 32, 16))
B = EC.xonly_pubkey(int("33" * 32, 16))


def _sale(**kw):
    return L.LightningSale("aa" * 32, R, 100000, 1_000_000, RECLAIM, **kw)


def test_frozen_addresses(t):
    s = _sale()
    lock = L.LotLock(s, hashlib.sha256(b"vector").digest(), B, bytes([0x51, 0x20]) + B, 21600)
    t.eq(s.spk_hex, "5120809af3cf6733ffb09076ef19966f918e88993e2476763653e73f3cf14f391acc",
         "the Lightning sale address is unchanged")
    t.eq(lock.lot.script_pubkey.hex(),
         "5120582d4752b735f906fbe8a14ef26b344463f60893fd00b2c9209cad6fcea864db",
         "the lot lock is unchanged")
    t.eq(lock.bond.script_pubkey.hex(),
         "5120637d0c8930979c7d848cbfe16dc02d91da9cf41541fd291ff5e16db247df4a21",
         "the bond lock is unchanged")
    t.eq(L.Vesting(1_900_000_000, B).script_pubkey.hex(),
         "51201f3cfda141f7940e8fd6427976d788c2c825b979faf01e68979b964d310444c1",
         "the vesting destination is unchanged")


def test_sell_leaf_is_the_covenants(t):
    terms = C.SaleTerms("aa" * 32, "bb" * 32, 1, 4, "11" * 32, 100000, 1_000_000, RECLAIM.hex())
    s = _sale(sell_terms=terms)
    t.eq(s.sell_leaf, C.SaleCovenant(terms).sell_leaf,
         "the direct-settlement leaf is the ordinary sale's SELL leaf, byte for byte")
    t.ok(s.spk_hex != _sale().spk_hex, "and adding it gives a different address")


def test_refusals(t):
    def refused(fn, what):
        try:
            fn()
            t.ok(False, what)
        except ValueError:
            t.ok(True, what)
    refused(lambda: L.LightningSale("aa" * 32, b"\x00" * 32, 1, 1, RECLAIM),
            "a receiver key that is no curve point is refused")
    refused(lambda: L.timeout_units(0), "a zero timeout is refused")
    refused(lambda: L.timeout_units(0x10000 * 512), "a timeout past 16 bits of 512 s is refused")
    refused(lambda: L.LotLock(_sale(), b"\x00" * 31, B, bytes([0x51, 0x20]) + B, 600),
            "a payment hash that is not 32 bytes is refused")
    refused(lambda: L.LotLock(_sale(), b"\x00" * 32, B, bytes([0x52, 0x20]) + B, 600),
            "a destination that is no v0 or v1 witness output is refused")
    other = C.SaleTerms("aa" * 32, "bb" * 32, 1, 4, "11" * 32, 1, 1_000_000, RECLAIM.hex())
    refused(lambda: _sale(sell_terms=other),
            "direct-settlement terms with another minimum are refused")


def test_timeouts(t):
    t.eq(L.timeout_units(512), 1, "512 s is one unit")
    t.eq(L.timeout_units(513), 2, "and a second more rounds up")
    t.eq(L.csv_value(85), (1 << 22) | 85, "a time-based relative lock sets bit 22")
