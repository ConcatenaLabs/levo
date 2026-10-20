"""The capped sale's terms, list and address: no node, no seqc.

The address check is the one that matters: a capped sale Levo derives must be
the address sequentia-contracts' own golden vector names for the same terms,
list and state, or a project would fund somewhere the program cannot spend.
"""

import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import capped as CAP  # noqa: E402
import covenant as C  # noqa: E402
import secp256k1 as EC  # noqa: E402
import sequentia_address as SA  # noqa: E402

G1 = "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
G2 = "c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5"
G3 = "f9308a019258c31049344f85f89d5229b531c845836f99b08601f113bce036f9"


def _terms(allocations, **kw):
    # sequentia-contracts' first capped_sale vector, in Levo's terms: assets in
    # display order (the vector's are internal), a taproot treasury G3.
    base = dict(token_asset=bytes.fromhex("3451dc1e07263404995266b7ba2df763bae70984936b6edf7bd149d913d2f874")[::-1].hex(),
                payment_asset=bytes.fromhex("54a2cfc4ecd61c94bc1e51000139ea528f2835365ce123e0e1abd6e1bb3cdb4f")[::-1].hex(),
                price_num=1, price_den=4, treasury_prog=G3, min_lot=1_000_000,
                close_locktime=200_000, reclaim_xonly=G3, allocations=allocations,
                total_atoms=600_000_000)
    base.update(kw)
    return C.SaleTerms(**base)


def test_the_pin_holds(t):
    for name, want in CAP.PIN["files"].items():
        got = hashlib.sha256((HERE.parent / name).read_bytes()).hexdigest()
        t.eq(got, want, "%s is the pinned file" % name)
    t.eq(CAP.DESCRIPTOR["template_hash"], CAP.PIN["template_hash"], "the descriptor is the pinned template")


def test_a_capped_sale_derives_the_contracts_vector(t):
    terms = _terms([{"key": G1, "cap_atoms": "500000000"}, {"key": G2, "cap_atoms": "250000000"}])
    cov = C.derive(terms)
    t.ok(getattr(cov, "capped", False), "an allocation list makes a capped covenant")
    # templates/capped_sale/vectors.json at the pinned commit, vector 1 and 2.
    t.eq(SA.segwit_v1_address("tb", cov.script_pubkey[2:]),
         "tb1pldl8p0lluyva9x0vlk5kk77gkrrlpypnuukq9kzpyc4gn57ap3hq2g3068",
         "nothing bought: the vector's address")
    cov.bought = [120_000_000, 0]
    t.ok(cov.spk_hex != cov.first_spk_hex, "and a purchase moves it")


def test_the_terms_round_trip(t):
    terms = _terms([{"key": G1, "cap_atoms": "500000000"}])
    again = C.SaleTerms.from_json(terms.to_json())
    t.eq(again.allocations, terms.allocations, "the list survives to_json and back")
    t.eq(C.derive(again).spk_hex, C.derive(terms).spk_hex, "and so does the address")
    t.ok("allocations" not in C.SaleTerms.from_json({k: v for k, v in terms.to_json().items()
                                                       if k != "allocations"}).to_json(),
         "a sale without a list is the ordinary one")


def test_the_list_is_refused_when_it_cannot_bind(t):
    def refused(allocs, why):
        try:
            _terms(allocs)
            t.ok(False, why)
        except ValueError as e:
            t.ok(True, why, str(e))
    refused([], "an empty list")
    refused([{"key": G1, "cap_atoms": "500000000"}, {"key": G1, "cap_atoms": "1"}], "a key twice")
    refused([{"key": "00" * 32, "cap_atoms": "500000000"}], "a key that is not a point")
    refused([{"key": G1, "cap_atoms": "0"}], "a zero cap")
    refused([{"key": G1, "cap_atoms": "999999"}], "a cap below the minimum purchase")
    refused([{"key": G1[:62], "cap_atoms": "500000000"}], "a short key")


def test_a_purchase_is_followed_to_its_buyer(t):
    terms = _terms([{"key": G1, "cap_atoms": "500000000"}, {"key": G2, "cap_atoms": "250000000"}])
    cov = C.derive(terms)
    nxt = cov.after(1, 2_000_000)
    found = cov.follow(cov.spk(nxt).hex(), 2_000_000)
    t.eq(found, (1, nxt), "the one position whose next state derives the rest's script")
    t.eq(cov.follow("51" + "20" + "00" * 32, 2_000_000), None, "and none for a script no state derives")
    try:
        cov.after(1, 250_000_001)
        t.ok(False, "past the cap")
    except CAP.CappedError:
        t.ok(True, "past the cap is refused")
