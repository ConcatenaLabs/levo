#!/usr/bin/env python3
"""The Lightning sale's scripts against a real Sequentia node.

Starts sequentiad on a throwaway elementsregtest chain with -par=1, so a block
that fails a script names the failure, and walks a Lightning sale through
every path its scripts allow: a reservation, a claim with the payment's secret
into a vesting destination, the vested spend, an unpaid lot re-rested after
its timeout with the bond forfeited, and a direct purchase through the SELL
leaf in a Sequentia asset. Each spend the scripts must refuse is mined with
`generateblock` and the refusal's reason is checked, not just the refusal:
relay policy can hide a consensus flaw, and a refusal for an unrelated reason
proves nothing.

Skipped when there is no node (see test_node.find_node).
"""

import hashlib
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import covenant as C  # noqa: E402
import lnsale as L  # noqa: E402
import rpc as RPCMOD  # noqa: E402
import secp256k1 as EC  # noqa: E402
import tx as TX  # noqa: E402
from test_node import Checker, Rig, Wallet, find_node  # noqa: E402

COIN = 100_000_000
FEE = 5_000

RECEIVER = 0x1111111111111111111111111111111111111111111111111111111111111111
RECLAIM = 0x2222222222222222222222222222222222222222222222222222222222222222
BUYER = 0x3333333333333333333333333333333333333333333333333333333333333333
STRANGER = 0x4444444444444444444444444444444444444444444444444444444444444444
TREASURY = 0x5555555555555555555555555555555555555555555555555555555555555555


class StrictRig(Rig):
    """The shared rig, with scripts checked on one thread so a block's error
    names the script failure instead of `block-validation-failed`."""

    def _launch(self):
        if "-par=1" not in self.extra:
            self.extra = list(self.extra) + ["-par=1"]
        super()._launch()


def run(ok, rig, log):
    w, n = rig.w, rig.n
    genesis = n("getblockhash", 0)
    labels = n("dumpassetlabels")
    policy = labels["bitcoin"]
    token = w("issueasset", assetamount=1_000_000, tokenamount=0, blind=False, fee_asset="bitcoin")["asset"]
    bond_asset = w("issueasset", assetamount=1_000_000, tokenamount=0, blind=False, fee_asset="bitcoin")["asset"]
    pay = w("issueasset", assetamount=1_000_000, tokenamount=0, blind=False, fee_asset="bitcoin")["asset"]
    rig.mine()

    def spk_of(addr):
        return w("getaddressinfo", addr)["scriptPubKey"]

    def coin(asset, atoms, addr=None):
        """An explicit wallet coin of exactly `atoms`, confirmed."""
        addr = addr or w("getnewaddress", "", "bech32")
        txid = w("sendtoaddress", address=addr, amount="%.8f" % (atoms / COIN), assetlabel=asset,
                    fee_asset_label="bitcoin")
        rig.mine()
        raw = w("gettransaction", txid, True, True)["decoded"]
        for o in raw["vout"]:
            if o["scriptPubKey"].get("address") == addr or o["scriptPubKey"].get("hex") == spk_of(addr):
                if RPCMOD.to_atoms(o["value"]) == atoms:
                    return {"txid": txid, "vout": o["n"], "asset": asset, "atoms": atoms,
                            "spk": o["scriptPubKey"]["hex"]}
        raise RuntimeError("no coin found")

    def blocked(hexed, want, what):
        """Mine `hexed` into a block with generateblock; it must be refused,
        and for the reason named."""
        try:
            n("generateblock", w("getnewaddress"), [hexed])
            ok.ok(False, what, "the block was accepted")
            log.append("NOT REFUSED: %s" % what)
        except RuntimeError as e:
            msg = str(e)
            ok.ok(want in msg, what, msg)
            log.append("refused in a block: %s -> %s" % (what, msg))

    def mined(hexed, what):
        try:
            h = n("generateblock", w("getnewaddress"), [hexed])["hash"]
            ok.ok(True, what)
            tx = n("decoderawtransaction", hexed)
            log.append("mined: %s (%d vB, txid %s)" % (what, tx["vsize"], tx["txid"]))
            return h
        except RuntimeError as e:
            ok.ok(False, what, str(e))
            log.append("NOT MINED: %s -> %s" % (what, e))
            raise

    def wallet_sign(t):
        r = w("signrawtransactionwithwallet", t.hex())
        return r["hex"]

    def sig(t, spent, idx, leaf, secret):
        msg = TX.taproot_sighash(t, spent, idx, genesis, leaf_script=leaf)
        return EC.schnorr_sign(msg, secret)

    R = EC.xonly_pubkey(RECEIVER)
    B = EC.xonly_pubkey(BUYER)
    treasury_spk = bytes([0x51, 0x20]) + EC.xonly_pubkey(TREASURY)
    h0 = n("getblockcount")
    sell = C.SaleTerms(token, pay, 25, 100, EC.xonly_pubkey(TREASURY).hex(), 100 * COIN,
                       h0 + 10_000, EC.xonly_pubkey(RECLAIM).hex(), 10_000 * COIN)
    sale = L.LightningSale(token, R, 100 * COIN, h0 + 10_000, EC.xonly_pubkey(RECLAIM), sell_terms=sell)
    log.append("sale address %s, reserve leaf %d B, sell leaf %d B"
               % (sale.spk_hex, len(sale.reserve_leaf), len(sale.sell_leaf)))
    sale_addr_coin = coin(token, 10_000 * COIN, None)
    # fund the sale by sending the token to its scriptPubKey
    import address as ADDR
    sale_addr = ADDR.from_script_pubkey(sale.spk_hex, "ert")
    t = TX.Transaction()
    t.vin.append(TX.TxIn(sale_addr_coin["txid"], sale_addr_coin["vout"]))
    fee_c = coin(policy, 100_000)
    t.vin.append(TX.TxIn(fee_c["txid"], fee_c["vout"]))
    t.vout.append(TX.TxOut(token, 10_000 * COIN, sale.script_pubkey))
    t.vout.append(TX.TxOut(policy, 100_000 - FEE, bytes.fromhex(fee_c["spk"])))
    t.vout.append(TX.TxOut(policy, FEE, b""))
    fund_hex = wallet_sign(t)
    mined(fund_hex, "the sale is funded at %s" % sale_addr)
    sale_coin = (n("decoderawtransaction", fund_hex)["txid"], 0)

    def reserve(sale_in, locked, lot_atoms, lock, bond_atoms, signer=RECEIVER, mutate=None):
        bc = coin(bond_asset, bond_atoms + 1_000 * COIN)
        fc = coin(policy, 100_000)
        t = TX.Transaction()
        t.vin.append(TX.TxIn(sale_in[0], sale_in[1]))
        t.vin.append(TX.TxIn(bc["txid"], bc["vout"]))
        t.vin.append(TX.TxIn(fc["txid"], fc["vout"]))
        t.vout.append(TX.TxOut(token, lot_atoms, lock.lot.script_pubkey))
        rem = locked - lot_atoms
        if rem:
            t.vout.append(TX.TxOut(token, rem, sale.script_pubkey))
        t.vout.append(TX.TxOut(bond_asset, bond_atoms, lock.bond.script_pubkey))
        t.vout.append(TX.TxOut(bond_asset, 1_000 * COIN, bytes.fromhex(bc["spk"])))
        t.vout.append(TX.TxOut(policy, 100_000 - FEE, bytes.fromhex(fc["spk"])))
        t.vout.append(TX.TxOut(policy, FEE, b""))
        if mutate:
            mutate(t)
        spent = [(token, locked, sale.script_pubkey), (bond_asset, bc["atoms"], bytes.fromhex(bc["spk"])),
                 (policy, fc["atoms"], bytes.fromhex(fc["spk"]))]
        s = sig(t, spent, 0, sale.reserve_leaf, signer)
        hexed = wallet_sign(t)
        return TX.set_witness(hexed, 0, sale.tree.witness("reserve", s)), t.txid()

    # --- reservation 1, paid ------------------------------------------------
    preimage = hashlib.sha256(b"levo invoice one").digest()
    H = hashlib.sha256(preimage).digest()
    vest_time = int(time.time()) + 3 * 3600
    vest = L.Vesting(vest_time, B)
    lock = L.LotLock(sale, H, B, vest.script_pubkey, 6 * 3600)
    log.append("lot lock %s, bond lock %s, claim leaf %d B, re-rest leaf %d B, timeout %d s"
               % (lock.lot.script_pubkey.hex(), lock.bond.script_pubkey.hex(), len(lock.claim_leaf),
                  len(lock.rerest_leaf), lock.timeout_seconds))
    LOT, BOND, LOCKED = 1_000 * COIN, 10 * COIN, 10_000 * COIN

    bad, _ = reserve(sale_coin, LOCKED, LOT, lock, BOND, signer=STRANGER)
    blocked(bad, "Invalid Schnorr signature", "a reservation signed by a key other than the receiver's")

    def rem_elsewhere(t):
        t.vout[1] = TX.TxOut(token, LOCKED - LOT, treasury_spk)
    bad, _ = reserve(sale_coin, LOCKED, LOT, lock, BOND, mutate=rem_elsewhere)
    blocked(bad, "Script failed an OP_EQUALVERIFY operation",
            "a reservation that re-rests the remainder anywhere but the sale")

    good, rtxid = reserve(sale_coin, LOCKED, LOT, lock, BOND)
    mined(good, "reservation: %d tokens into the lot lock, the rest re-rests, a bond of %d"
          % (LOT // COIN, BOND // COIN))
    lot_in, rem_in, bond_in = (rtxid, 0), (rtxid, 1), (rtxid, 2)

    def claim(secret_for_lot, dest=None, lot_value=LOT, refund_signer=BUYER, refund_preimage=None,
              seq=0xffffffff, with_bond=True):
        fc = coin(policy, 100_000)
        buyer_back = w("getnewaddress", "", "bech32")
        t = TX.Transaction()
        t.vin.append(TX.TxIn(lot_in[0], lot_in[1], sequence=seq))
        if with_bond:
            t.vin.append(TX.TxIn(bond_in[0], bond_in[1], sequence=seq))
        t.vin.append(TX.TxIn(fc["txid"], fc["vout"]))
        t.vout.append(TX.TxOut(token, lot_value, dest if dest is not None else vest.script_pubkey))
        if lot_value != LOT:
            t.vout.append(TX.TxOut(token, LOT - lot_value, treasury_spk))
        if with_bond:
            t.vout.append(TX.TxOut(bond_asset, BOND, bytes.fromhex(spk_of(buyer_back))))
        t.vout.append(TX.TxOut(policy, 100_000 - FEE, bytes.fromhex(fc["spk"])))
        t.vout.append(TX.TxOut(policy, FEE, b""))
        spent = [(token, LOT, lock.lot.script_pubkey)]
        if with_bond:
            spent.append((bond_asset, BOND, lock.bond.script_pubkey))
        spent.append((policy, fc["atoms"], bytes.fromhex(fc["spk"])))
        hexed = wallet_sign(t)
        hexed = TX.set_witness(hexed, 0, lock.lot.witness("claim", secret_for_lot))
        if with_bond:
            s = sig(t, spent, 1, lock.refund_leaf, refund_signer)
            hexed = TX.set_witness(hexed, 1, lock.bond.witness(
                "refund", s, refund_preimage if refund_preimage is not None else preimage))
        return hexed

    blocked(claim(hashlib.sha256(b"a guess").digest(), with_bond=False),
            "Script failed an OP_EQUALVERIFY operation", "the lot taken without the secret")
    blocked(claim(preimage, dest=treasury_spk, with_bond=False),
            "Script failed an OP_EQUALVERIFY operation",
            "the lot taken with the secret, to a destination other than the buyer's")
    blocked(claim(preimage, lot_value=LOT - 1, with_bond=False),
            "Script evaluated without error but finished with a false/empty top stack element",
            "the lot taken with the secret, one atom short to the buyer")
    blocked(claim(preimage, refund_signer=RECEIVER), "Invalid Schnorr signature",
            "the bond kept after paying: the project signs the bond's refund with the secret")

    def forfeit_now(seq):
        fc = coin(policy, 100_000)
        t = TX.Transaction()
        t.vin.append(TX.TxIn(bond_in[0], bond_in[1], sequence=seq))
        t.vin.append(TX.TxIn(fc["txid"], fc["vout"]))
        t.vout.append(TX.TxOut(bond_asset, BOND, treasury_spk))
        t.vout.append(TX.TxOut(policy, 100_000 - FEE, bytes.fromhex(fc["spk"])))
        t.vout.append(TX.TxOut(policy, FEE, b""))
        spent = [(bond_asset, BOND, lock.bond.script_pubkey), (policy, fc["atoms"], bytes.fromhex(fc["spk"]))]
        s = sig(t, spent, 0, lock.forfeit_leaf, RECEIVER)
        return TX.set_witness(wallet_sign(t), 0, lock.bond.witness("forfeit", s))
    blocked(forfeit_now(lock.sequence), "bad-txns-nonfinal",
            "the bond kept after paying: the project's forfeit before the timeout (sequence set)")
    blocked(forfeit_now(0), "Locktime requirement not satisfied",
            "the bond kept after paying: the project's forfeit before the timeout (sequence unset)")

    mined(claim(preimage), "the claim: the secret delivers the lot to the vesting destination "
                           "and the buyer's signature takes the bond back")

    # --- the vested allocation ----------------------------------------------
    claim_tx = None
    for d in n("getblock", n("getbestblockhash"), 2)["tx"]:
        if any(o["scriptPubKey"]["hex"] == vest.script_pubkey.hex() for o in d["vout"]):
            claim_tx = d
    vest_in = (claim_tx["txid"], [o["n"] for o in claim_tx["vout"]
                                  if o["scriptPubKey"]["hex"] == vest.script_pubkey.hex()][0])

    def vest_spend(locktime, signer=BUYER):
        fc = coin(policy, 100_000)
        t = TX.Transaction(locktime=locktime)
        t.vin.append(TX.TxIn(vest_in[0], vest_in[1], sequence=0xfffffffe))
        t.vin.append(TX.TxIn(fc["txid"], fc["vout"], sequence=0xfffffffe))
        t.vout.append(TX.TxOut(token, LOT, bytes.fromhex(spk_of(w("getnewaddress", "", "bech32")))))
        t.vout.append(TX.TxOut(policy, 100_000 - FEE, bytes.fromhex(fc["spk"])))
        t.vout.append(TX.TxOut(policy, FEE, b""))
        spent = [(token, LOT, vest.script_pubkey), (policy, fc["atoms"], bytes.fromhex(fc["spk"]))]
        s = sig(t, spent, 0, vest.leaf, signer)
        return TX.set_witness(wallet_sign(t), 0, vest.tree.witness("vest", s))
    blocked(vest_spend(vest_time), "bad-txns-nonfinal", "the allocation spent before it vests")

    # --- reservation 2, never paid ------------------------------------------
    preimage2 = hashlib.sha256(b"levo invoice two").digest()
    lock2 = L.LotLock(sale, hashlib.sha256(preimage2).digest(), B,
                      bytes.fromhex(spk_of(w("getnewaddress", "", "bech32"))), 6 * 3600)
    LOT2 = 500 * COIN
    good2, r2 = reserve(rem_in, LOCKED - LOT, LOT2, lock2, BOND)
    mined(good2, "a second reservation, which nobody pays")
    lot2_in, rem2_in, bond2_in = (r2, 0), (r2, 1), (r2, 2)

    def rerest(seq, dest=None, value=LOT2):
        t = TX.Transaction()
        t.vin.append(TX.TxIn(lot2_in[0], lot2_in[1], sequence=seq))
        t.vin.append(TX.TxIn(bond2_in[0], bond2_in[1], sequence=seq))
        t.vout.append(TX.TxOut(token, value, dest if dest is not None else sale.script_pubkey))
        if value != LOT2:
            t.vout.append(TX.TxOut(token, LOT2 - value, treasury_spk))
        # the bond pays the fee; it is in an asset the node prices 1:1 here
        t.vout.append(TX.TxOut(bond_asset, BOND - FEE, treasury_spk))
        t.vout.append(TX.TxOut(bond_asset, FEE, b""))
        spent = [(token, LOT2, lock2.lot.script_pubkey), (bond_asset, BOND, lock2.bond.script_pubkey)]
        s = sig(t, spent, 1, lock2.forfeit_leaf, RECEIVER)
        hexed = t.hex()
        hexed = TX.set_witness(hexed, 0, lock2.lot.witness("rerest"))
        return TX.set_witness(hexed, 1, lock2.bond.witness("forfeit", s))

    n("setfeeexchangerates", {"bitcoin": COIN, bond_asset: COIN})
    blocked(rerest(lock2.sequence), "bad-txns-nonfinal", "a re-rest before the timeout (sequence set)")
    blocked(rerest(0), "Locktime requirement not satisfied", "a re-rest before the timeout (sequence unset)")

    # Let the timeout pass: move the clock and mine past the median.
    now = int(time.time())
    for i in range(12):
        n("setmocktime", now + lock2.timeout_seconds + 600 + i * 60)
        rig.mine()
    blocked(rerest(lock2.sequence, dest=treasury_spk), "Script failed an OP_EQUALVERIFY operation",
            "after the timeout, the lot sent anywhere but the sale")
    blocked(rerest(lock2.sequence, value=LOT2 - 1),
            "Script evaluated without error but finished with a false/empty top stack element",
            "after the timeout, the lot re-rested one atom short")
    mined(rerest(lock2.sequence), "after the timeout: the lot re-rests at the sale, the project takes the bond")
    rested = n("scantxoutset", "start", ["raw(%s)" % sale.spk_hex])
    amounts = sorted(RPCMOD.to_atoms(u["amount"]) for u in rested["unspents"])
    ok.eq(amounts, sorted([LOCKED - LOT - LOT2, LOT2]), "the sale address holds the remainder and the re-rested lot")
    log.append("sale address now holds %s tokens in %d coins" % ([a // COIN for a in amounts], len(amounts)))

    # --- the vested allocation, after its time ------------------------------
    n("setmocktime", max(now + lock2.timeout_seconds + 2000, vest_time + 3600))
    for _ in range(12):
        rig.mine()
    blocked(vest_spend(vest_time - 1), "Locktime requirement not satisfied",
            "once vested, a spend whose locktime names a time before the vesting time")
    mined(vest_spend(vest_time), "the allocation spent once vested")

    # --- the SELL leaf of the same address: a direct purchase in PAY --------
    pc = coin(pay, 1_000 * COIN)
    fc = coin(policy, 100_000)
    take = 400 * COIN
    price = sell.cost_for(take)
    t = TX.Transaction()
    t.vin.append(TX.TxIn(rem2_in[0], rem2_in[1]))
    t.vin.append(TX.TxIn(pc["txid"], pc["vout"]))
    t.vin.append(TX.TxIn(fc["txid"], fc["vout"]))
    t.vout.append(TX.TxOut(pay, price, treasury_spk))
    left = LOCKED - LOT - LOT2 - take
    t.vout.append(TX.TxOut(token, left, sale.script_pubkey))
    t.vout.append(TX.TxOut(token, take, bytes.fromhex(spk_of(w("getnewaddress", "", "bech32")))))
    t.vout.append(TX.TxOut(pay, 1_000 * COIN - price, bytes.fromhex(pc["spk"])))
    t.vout.append(TX.TxOut(policy, 100_000 - FEE, bytes.fromhex(fc["spk"])))
    t.vout.append(TX.TxOut(policy, FEE, b""))
    mined(TX.set_witness(wallet_sign(t), 0, sale.tree.witness("sell")),
          "a direct purchase through the SELL leaf of the same address, paid in a Sequentia asset")


def main():
    binary = find_node()
    if not binary:
        print("skipped: no sequentiad")
        return 0
    ok = Checker()
    log = []
    rig = StrictRig(binary)
    try:
        run(ok, rig, log)
    finally:
        rig.stop()
        out = os.environ.get("LEVO_LNSALE_LOG")
        if out:
            Path(out).write_text("\n".join(log) + "\n")
        for line in log:
            print("  " + line)
    return ok.report()


if __name__ == "__main__":
    sys.exit(main())
