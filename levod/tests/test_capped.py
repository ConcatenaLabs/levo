#!/usr/bin/env python3
"""A capped sale, end to end, against a real node and a real levod.

A capped sale's caps are in its address: the `sequentia/capped-sale` covenant
(levod/capped.py) sells only to the keys on its allocation list, each up to
its cap, and carries every key's running total in the coin's state. This drive
lists one, locks it, buys from it through `bin/levo` with two listed keys, has
a third purchase built outside Levo entirely, and reclaims the rest after the
close, all on a throwaway regtest with Simplicity active. It checks that:

  - each purchase moves the coin to the address of the state it leaves, and
    levod follows it there, for its own purchases and for one it never built;
  - a buyer who has reached their cap is refused, by levod and by the program;
  - a key that is not on the list is refused;
  - the reclaim after the close takes what is left.

It needs a node (SEQUENTIAD, or SEQUENTIA_SRC) and the contracts toolchain's
`seqc` (LEVOD_SEQC), and reports itself skipped without either.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import capped as CAP  # noqa: E402
import covenant as C  # noqa: E402
import sale as S  # noqa: E402
import secp256k1 as K  # noqa: E402
import tx as TX  # noqa: E402
from test_cli import Checker, Levod  # noqa: E402
from test_node import COIN, Rig, find_node, free_port  # noqa: E402


class SimplicityRig(Rig):
    """The rig, with Simplicity active from genesis: a custom chain has it
    off unless told, and with `-par=1` a block's refusal names its script
    error."""

    def _launch(self):
        self.extra = list(self.extra) + [e for e in ("-evbparams=simplicity:-1:::", "-par=1")
                                         if e not in self.extra]
        Rig._launch(self)


class CappedLevod(Levod):
    def __init__(self, rig, port, payment_asset, seqc):
        # The watcher polls every two seconds here, so a purchase levod never
        # built is followed without anybody telling it.
        Levod.__init__(self, rig, port, payment_asset,
                       extra_env={"LEVOD_SEQC": seqc, "LEVOD_WATCH_SECONDS": "2"})


def wait_for(ok, what, fn, seconds=40, tick=None):
    end = time.time() + seconds
    last = None
    while time.time() < end:
        if tick:
            tick()
        try:
            last = fn()
            if last:
                return last
        except Exception as e:
            last = e
        time.sleep(0.5)
    ok.ok(False, what, repr(last)[:200])
    return None


def run(ok, rig, levod, env, seqc, log):
    w = rig.w

    def levo(*args, **kw):
        r = subprocess.run([sys.executable, str(ROOT / "bin" / "levo")] + list(args),
                           capture_output=True, text=True, env=dict(env, **kw.get("env", {})),
                           timeout=240)
        out = (r.stdout or "") + (r.stderr or "")
        log.write("$ levo %s\n%s\n" % (" ".join(args), out.strip()))
        if kw.get("expect_failure"):
            ok.ok(r.returncode != 0, "levo %s fails" % " ".join(args[:2]), out[-200:])
        else:
            ok.eq(r.returncode, 0, "levo %s" % " ".join(args[:2]))
        return out

    def show():
        return json.loads(levo("show", "capped"))["sale"]

    secrets_ = [int.from_bytes(os.urandom(32), "big") % K.N for _ in range(4)]
    xonly = [K.xonly_pubkey(s).hex() for s in secrets_]
    A, B, Cc, STRANGER = 0, 1, 2, 3
    keys = json.loads(levo("keygen"))
    token = w("issueasset", assetamount=100_000, tokenamount=0, blind=False, fee_asset="bitcoin")["asset"]
    rig.mine()
    treasury = w("getnewaddress", "", "bech32")
    spec = {
        "project": {"slug": "capped", "name": "Capped Sale", "ticker": "CAP", "decimals": 8,
                    "summary": "A sale whose caps are in its address.",
                    "description": "Listed, locked, bought within each cap and reclaimed."},
        "terms": {"token_asset": token, "price_num": 1, "price_den": 4,
                  "min_lot": 10 * COIN, "total_atoms": 1_000 * COIN,
                  "close_locktime": rig.n("getblockcount") + 40,
                  "treasury_address": treasury, "reclaim_xonly": keys["reclaim_xonly"],
                  "allocations": [{"key": xonly[A], "cap_atoms": str(300 * COIN)},
                                  {"key": xonly[B], "cap_atoms": str(200 * COIN)},
                                  {"key": xonly[Cc], "cap_atoms": str(1_000 * COIN)}]},
    }
    listing = Path(tempfile.mkdtemp()) / "listing.json"
    listing.write_text(json.dumps(spec))
    out = levo("create", str(listing))
    ok.ok("listed capped" in out, "create lists a capped sale", out[-200:])
    w("sendtoaddress", address=w("getnewaddress", "", "bech32"), amount=100_000, assetlabel=token,
      fee_asset_label="bitcoin")
    rig.mine()
    out = levo("lock", "capped")
    ok.ok("locked at" in out, "lock funds the capped sale at its first state's address", out[-200:])
    rig.mine()
    sale = show()
    ok.eq(sale["status"], "live", "the capped sale is live")
    ok.eq(sale["capped"]["enforced_by"], "consensus", "and says its caps are the chain's")
    first_spk = sale["script_pubkey"]
    ok.eq(sale["capped"]["funding_script_pubkey"], first_spk, "funded at the first state's address")

    # --- buyer A, through bin/levo, twice: 200 then exactly the rest of the cap
    out = levo("buy", "capped", "--tokens", "200", "--buyer-key", "%064x" % secrets_[A], "--seqc", seqc)
    ok.ok("bought." in out, "buyer A buys 200 through bin/levo", out[-300:])
    rig.mine()
    s1 = wait_for(ok, "levod follows the coin to its next state",
                  lambda: (lambda s: s if s["script_pubkey"] != first_spk and int(s["locked_atoms"]) == 800 * COIN else None)(show()))
    if s1:
        got = {a["key"]: int(a["bought_atoms"]) for a in s1["capped"]["allocations"]}
        ok.eq(got[xonly[A]], 200 * COIN, "A's running total is 200 in the state levod shows")
        f = s1["funding"]
        coin = rig.n("gettxout", f["txid"], f["vout"], True)
        ok.eq(coin["scriptPubKey"]["hex"], s1["script_pubkey"], "and the coin is at that state's address on chain")
    out = levo("buy", "capped", "--tokens", "100", "--buyer-key", "%064x" % secrets_[A], "--seqc", seqc)
    ok.ok("bought." in out, "buyer A buys exactly the rest of the cap", out[-300:])
    rig.mine()
    s2 = wait_for(ok, "levod follows the second purchase",
                  lambda: (lambda s: s if int(s["locked_atoms"]) == 700 * COIN else None)(show()))
    out = levo("buy", "capped", "--tokens", "10", "--buyer-key", "%064x" % secrets_[A], "--seqc", seqc,
               expect_failure=True)
    ok.ok("covenant refuses a purchase past it" in out, "A at the cap is refused one more lot", out[-300:])
    out = levo("buy", "capped", "--tokens", "10", "--buyer-key", "%064x" % secrets_[STRANGER], "--seqc", seqc,
               expect_failure=True)
    ok.ok("not on this sale's allocation list" in out, "a key not on the list is refused", out[-300:])

    # --- the program itself refuses A past the cap: a purchase built outside
    # Levo, signed by A, run against the transaction -------------------------
    terms = C.SaleTerms.from_json(json.loads(levo("show", "capped"))["sale"]["terms"])
    cov = C.derive(terms)
    cov.bought = [int(a["bought_atoms"]) for a in s2["capped"]["allocations"]]
    sale_obj = S.Sale("capped", terms, "outsider")
    sale_obj.cov = cov
    sale_obj.funding = dict(s2["funding"])
    sale_obj.locked_atoms = int(s2["locked_atoms"])

    def outside_purchase(who, atoms, bought_claim=None):
        """A purchase as a third party builds one: Levo's own builder and the
        vendored covenant, no levod. Returns (tx hex, spent outputs)."""
        pay = levod.payment_asset
        u = [x for x in w("listunspent", 1) if x.get("asset") == pay and float(x["amount"]) > 1000]
        u = u[0]
        info = rig.n("gettxout", u["txid"], u["vout"], True)
        ins = [{"txid": u["txid"], "vout": u["vout"], "asset": pay,
                "value_atoms": int(round(float(u["amount"]) * COIN)),
                "script_pubkey": info["scriptPubKey"]["hex"]}]
        plan = S.BuyPlan(sale_obj, "outsider", atoms, terms.cost_for(atoms),
                         sale_obj.locked_atoms - atoms, 0, position=who)
        if bought_claim is not None:
            saved = cov.caps[who]
            cov.caps[who] = bought_claim
        try:
            spk = lambda: bytes.fromhex(rig.n("getaddressinfo", w("getnewaddress", "", "bech32"))["scriptPubKey"])
            built = TX.build_buy(sale_obj, plan, {"token_script_pubkey": spk().hex(),
                                                  "change_script_pubkey": spk().hex(),
                                                  "inputs": ins, "fee_atoms": 5000, "fee_asset": pay})
        finally:
            if bought_claim is not None:
                cov.caps[who] = saved
        return built

    built = outside_purchase(A, 10 * COIN, bought_claim=310 * COIN)
    genesis = rig.n("getblockhash", 0)
    msg = cov.sighash(built["unsigned_tx_hex"], built["spent_outputs"], 0, genesis, seqc=seqc)
    sig = K.schnorr_sign(bytes.fromhex(msg), secrets_[A]).hex()
    try:
        cov.witness(built["unsigned_tx_hex"], built["spent_outputs"], 0, genesis, A, 10 * COIN, sig, seqc=seqc)
        ok.ok(False, "the program refuses A past the cap")
    except CAP.CappedError as e:
        ok.ok("refuses this purchase" in str(e), "the program refuses A past the cap", str(e))
        log.write("program refusal (A one lot past the cap): %s\n" % e)

    # --- buyer B, by a purchase levod never built ---------------------------
    built = outside_purchase(B, 50 * COIN)
    msg = cov.sighash(built["unsigned_tx_hex"], built["spent_outputs"], 0, genesis, seqc=seqc)
    sig = K.schnorr_sign(bytes.fromhex(msg), secrets_[B]).hex()
    stack = cov.witness(built["unsigned_tx_hex"], built["spent_outputs"], 0, genesis, B, 50 * COIN, sig, seqc=seqc)
    hexed = TX.set_witness(built["unsigned_tx_hex"], 0, [bytes.fromhex(x) for x in stack])
    signed = w("signrawtransactionwithwallet", hexed)
    ok.ok(signed.get("complete"), "the outsider's wallet signs its inputs around the covenant witness")
    txid = rig.n("sendrawtransaction", signed["hex"])
    log.write("outside purchase by B, 50 tokens: %s, %d vB\n"
              % (txid, rig.n("decoderawtransaction", signed["hex"])["vsize"]))
    rig.mine()
    s3 = wait_for(ok, "levod follows a purchase it never built, by finding which key made it",
                  lambda: (lambda s: s if int(s["locked_atoms"]) == 650 * COIN else None)(show()))
    if s3:
        got = {a["key"]: int(a["bought_atoms"]) for a in s3["capped"]["allocations"]}
        ok.eq(got[xonly[B]], 50 * COIN, "B's running total is 50")
        ok.eq(got[xonly[A]], 300 * COIN, "A's is still 300")

    # --- C buys at the board, through bin/levo -------------------------------
    out = levo("buy", "capped", "--tokens", "150", "--buyer-key", "%064x" % secrets_[Cc], "--seqc", seqc)
    ok.ok("bought." in out, "buyer C buys 150", out[-300:])
    rig.mine()
    s4 = wait_for(ok, "levod follows C's purchase",
                  lambda: (lambda s: s if int(s["locked_atoms"]) == 500 * COIN else None)(show()))
    out = levo("positions")
    ok.ok("capped" in out, "positions lists the capped sale", out[:300])

    # --- after the close, the project reclaims the rest ----------------------
    close = spec["terms"]["close_locktime"]
    while rig.n("getblockcount") < close + 1:
        rig.mine()
    out = levo("reclaim", "capped", "--reclaim-key", keys["reclaim_secret_hex"])
    ok.ok("reclaimed" in out.lower() or "swept" in out.lower(), "the reclaim after the close takes the rest", out[-300:])
    rig.mine()
    detail = wait_for(ok, "the sale reads as reclaimed",
                      lambda: (lambda s: s if s["status"] == "reclaimed" and int(s["locked_atoms"]) == 0 else None)(show()),
                      seconds=60, tick=rig.mine)
    if detail:
        log.write("final status: %s\n" % detail["status"])
    return s1, s2, s3, s4


def main():
    binary = find_node()
    seqc = os.environ.get("LEVOD_SEQC")
    if not binary or not seqc or not os.access(seqc, os.X_OK):
        print("no sequentiad or no seqc found; skipping the capped sale drive "
              "(set SEQUENTIAD or SEQUENTIA_SRC, and LEVOD_SEQC)")
        return 0
    ok = Checker()
    rig = SimplicityRig(binary)
    levod = None
    log = open(os.environ.get("LEVO_CAPPED_LOG") or os.devnull, "w")
    try:
        pay = rig.w("issueasset", assetamount=1_000_000, tokenamount=0, blind=False,
                    fee_asset="bitcoin")["asset"]
        rig.mine()
        rig.n("setfeeexchangerates", {"bitcoin": COIN, pay: COIN})
        Path(rig.root, "elements.conf").write_text(
            "chain=elementsregtest\n[elementsregtest]\n"
            "rpcport=%d\nrpcuser=levo\nrpcpassword=levo\n" % rig.port)
        levod = CappedLevod(rig, free_port(), pay, seqc)
        env = dict(os.environ, LEVO_URL=levod.url,
                   SEQUENTIA_CLI=str(Path(binary).parent / "sequentia-cli"),
                   SEQUENTIA_DATADIR=rig.root, SEQUENTIA_WALLET="levo",
                   LEVO_SESSION=os.path.join(tempfile.mkdtemp(), "session.json"))
        run(ok, rig, levod, env, seqc, log)
    except Exception:
        import traceback
        ok.failed.append("the drive raised:\n" + traceback.format_exc())
    finally:
        if levod:
            levod.log.seek(0)
            log.write("--- levod log ---\n" + levod.log.read()[-6000:])
            levod.stop()
        rig.stop()
        log.close()
    return ok.report()


if __name__ == "__main__":
    sys.exit(main())
