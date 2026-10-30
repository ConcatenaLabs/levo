#!/usr/bin/env python3
"""A Lightning sale end to end: real chains, real Lightning, levod and the receiver.

Starts a Sequentia node (elementsregtest, -par=1), a Bitcoin regtest node and
two Core Lightning nodes on it (the project's receiver and a buyer's wallet,
with a channel between them), the project's receiver (receiver.py) and Levo's
platform, then:

  * lists a sale priced in BTC that also settles in a Sequentia asset;
  * buyer A reserves a lot through levod, the wallet signs its own coins in
    and broadcasts, pays the invoice in BTC over Lightning, and the receiver
    delivers the tokens with the payment's secret: A did nothing after paying;
  * buyer B pays in the sale's Sequentia asset through the SELL leaf;
  * buyer C reserves and never pays: past the timeout the receiver puts the
    lot back in the sale and keeps the bond;
  * levod refuses an invoice priced off the published BTC price.

Skipped without sequentiad, bitcoind or lightningd ($LEVO_LIGHTNINGD, then
lightningd on PATH).
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import market as M  # noqa: E402
import receiver as RCV  # noqa: E402
import rpc as RPCMOD  # noqa: E402
import secp256k1 as EC  # noqa: E402
import store as ST  # noqa: E402
from test_lnsale_node import StrictRig  # noqa: E402
from test_node import Checker, Founder, find_node  # noqa: E402

COIN = 100_000_000
RECEIVER_SECRET = "66" * 32


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_lightningd():
    for c in (os.environ.get("LEVO_LIGHTNINGD"), shutil.which("lightningd")):
        if c and os.access(c, os.X_OK):
            return c
    return None


class Lightning:
    """bitcoind regtest and two lightningd, a channel from the buyer to the receiver."""

    def __init__(self, lightningd):
        self.root = tempfile.mkdtemp(prefix="levo-ln-")
        self.bin = Path(lightningd).parent
        self.lightningd = lightningd
        self.cli_bin = str(self.bin / "lightning-cli")
        self.port = free_port()
        bd = os.path.join(self.root, "btc")
        os.makedirs(bd)
        Path(bd, "bitcoin.conf").write_text(
            "regtest=1\nserver=1\nfallbackfee=0.0002\n[regtest]\nrpcuser=u\nrpcpassword=p\n"
            "rpcport=%d\nport=%d\nlisten=0\n" % (self.port, free_port()))
        self.bd = bd
        self.procs = [subprocess.Popen(["bitcoind", "-datadir=" + bd], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)]
        for _ in range(100):
            try:
                self.btc("getblockcount")
                break
            except Exception:
                time.sleep(0.2)
        self.btc("createwallet", "m")
        self.mine(101)
        self.nodes = []
        for i in (1, 2):
            d = os.path.join(self.root, "ln%d" % i)
            os.makedirs(d)
            port = free_port()
            args = [lightningd, "--network=regtest", "--lightning-dir=" + d,
                    "--bitcoin-datadir=" + bd, "--bitcoin-rpcuser=u", "--bitcoin-rpcpassword=p",
                    "--bitcoin-rpcport=%d" % self.port, "--bitcoin-cli=" + shutil.which("bitcoin-cli"),
                    "--addr=127.0.0.1:%d" % port, "--log-file=" + os.path.join(d, "log"),
                    "--developer", "--dev-fast-gossip", "--allow-deprecated-apis=true"]
            self.procs.append(subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            self.nodes.append((d, port))
        for i in (0, 1):
            for _ in range(150):
                try:
                    self.cli(i, "getinfo")
                    break
                except Exception:
                    time.sleep(0.2)
        addr = self.cli(1, "newaddr")["bech32"]
        self.btc("-rpcwallet=m", "sendtoaddress", addr, "1")
        self.mine(6)
        self.wait(lambda: self.cli(1, "listfunds")["outputs"], "the buyer's funds")
        rid = self.cli(0, "getinfo")["id"]
        self.cli(1, "connect", rid, "127.0.0.1", str(self.nodes[0][1]))
        self.cli(1, "fundchannel", rid, "5000000")
        self.mine(6)
        self.wait(lambda: any(c["state"] == "CHANNELD_NORMAL"
                              for c in self.cli(1, "listpeerchannels")["channels"]), "the channel")

    def receiver_command(self):
        return "%s --network=regtest --lightning-dir=%s" % (self.cli_bin, self.nodes[0][0])

    def btc(self, *args):
        r = subprocess.run(["bitcoin-cli", "-datadir=" + self.bd] + list(args),
                           capture_output=True, text=True, timeout=60)
        if r.returncode:
            raise RuntimeError(r.stderr.strip())
        return r.stdout.strip()

    def mine(self, n):
        a = self.btc("-rpcwallet=m", "getnewaddress") if self._has_wallet() else None
        if a:
            self.btc("generatetoaddress", str(n), a)

    def _has_wallet(self):
        try:
            self.btc("-rpcwallet=m", "getwalletinfo")
            return True
        except Exception:
            return False

    def cli(self, i, *args):
        r = subprocess.run([self.cli_bin, "--network=regtest", "--lightning-dir=" + self.nodes[i][0]]
                           + list(args), capture_output=True, text=True, timeout=120)
        if r.returncode:
            raise RuntimeError(r.stdout.strip() or r.stderr.strip())
        return json.loads(r.stdout)

    def wait(self, cond, what, secs=90):
        end = time.time() + secs
        while time.time() < end:
            try:
                if cond():
                    return
            except Exception:
                pass
            self.mine(1)
            time.sleep(1)
        raise RuntimeError("timed out waiting for %s" % what)

    def stop(self):
        for i in (0, 1):
            try:
                self.cli(i, "stop")
            except Exception:
                pass
        try:
            self.btc("stop")
        except Exception:
            pass
        for p in self.procs:
            try:
                p.wait(timeout=30)
            except Exception:
                p.kill()
        shutil.rmtree(self.root, ignore_errors=True)


def run(ok, rig, ln, log):
    w, n = rig.w, rig.n
    node = RPCMOD.NodeRPC(url=rig.url, user="levo", password="levo")
    node.chain_info_ttl = 0
    token = w("issueasset", assetamount=1_000_000, tokenamount=0, blind=False, fee_asset="bitcoin")["asset"]
    usd = w("issueasset", assetamount=1_000_000, tokenamount=0, blind=False, fee_asset="bitcoin")["asset"]
    rig.mine()
    n("setfeeexchangerates", {"bitcoin": COIN, usd: COIN})

    plat = M.Platform(ST.Store(os.path.join(rig.root, "levo-state.json")), Founder(), None, node,
                      hrp="ert", payment_asset=usd, payment_label="USD")
    rport = free_port()
    project_spk = w("getaddressinfo", w("getnewaddress", "", "bech32"))["scriptPubKey"]
    rcv = RCV.Receiver(RECEIVER_SECRET, RCV.Cln(ln.receiver_command()), node,
                       lambda slug: plat.projects[slug].sale.terms,
                       os.path.join(rig.root, "receiver.json"), project_spk, ["helios"],
                       invoice_expiry=600)
    srv = ThreadingHTTPServer(("127.0.0.1", rport), RCV.make_handler(rcv))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    issuer = "02" + "11" * 32
    treasury = EC.xonly_pubkey(0x55).hex()
    h0 = node.chain_height()
    # 1 token = 1,000 sats (1e8 atoms -> 1e3 sats: 1/100000 sat per atom), and
    # 10 USD a token in the sale's own asset.
    terms = {"token_asset": token, "payment_asset": usd, "price_num": 10, "price_den": 1,
             "treasury_prog": treasury, "min_lot": 10 * COIN, "close_locktime": h0 + 5000,
             "reclaim_xonly": EC.xonly_pubkey(0x77).hex(), "total_atoms": 10_000 * COIN,
             "lightning": {"receiver_xonly": rcv.xonly, "receiver_url": "http://127.0.0.1:%d" % rport,
                           "btc_num": 1, "btc_den": 100000, "bond_asset": usd,
                           "bond_atoms": 5 * COIN, "fee_allowance": 20_000,
                           "timeout_seconds": 3600}}
    plat.list_project(issuer, {"slug": "helios", "name": "Helios", "ticker": "HLX",
                               "summary": "s", "description": "d"}, terms)
    P = plat.projects["helios"]
    w("sendtoaddress", address=plat.sale_address(P.sale), amount=10_000, assetlabel=token,
      fee_asset_label="bitcoin")
    rig.mine()
    plat.confirm_lock(issuer, "helios")
    ok.eq(P.sale.status, "live", "a sale priced in BTC is listed and live")
    log.append("sale %s at %s, priced %d sats per token" % ("helios", plat.sale_address(P.sale),
                                                           P.sale.terms.lightning.sats_for(COIN)))

    def usd_coin(atoms):
        a = w("getnewaddress", "", "bech32")
        txid = w("sendtoaddress", address=a, amount="%.8f" % (atoms / COIN), assetlabel=usd,
                 fee_asset_label="bitcoin")
        rig.mine()
        tx = w("gettransaction", txid, True, True)["decoded"]
        for o in tx["vout"]:
            if o["scriptPubKey"].get("address") == a:
                return {"txid": txid, "vout": o["n"]}

    def sign_send(pset):
        proc = w("walletprocesspsbt", pset)
        fin = w("finalizepsbt", proc["psbt"])
        ok.ok(fin["complete"], "the wallet completes the reservation")
        return n("sendrawtransaction", fin["hex"])

    def reserve(tokens, buyer):
        return plat.lightning_reserve(buyer, "helios", {
            "token_atoms": str(tokens * COIN), "token_address": w("getnewaddress", "", "bech32"),
            "inputs": [usd_coin(6 * COIN)], "fee_atoms": 30_000})

    def watch(h):
        return plat.lightning_status("helios", h)

    # --- A pays in BTC over Lightning ---------------------------------------
    A = "02" + "aa" * 32
    r = reserve(100, A)
    lot = r["lot"]
    ok.eq(lot["sats"], 100_000, "100 tokens at 1,000 sats are 100,000 sats")
    ok.eq(watch(lot["payment_hash"])["state"], "reserving", "before broadcast the lot is reserving")
    txid = sign_send(r["pset"])
    ok.eq(txid, lot["reserve_txid"], "the reservation broadcast is the one levod built")
    rig.mine()
    ok.eq(watch(lot["payment_hash"])["state"], "reserved", "in a block, the lot is reserved")
    log.append("A reserved 100 HLX: reservation %s, invoice %s sats" % (txid, lot["sats"]))
    paid = ln.cli(1, "pay", lot["bolt11"])
    ok.eq(paid["status"], "complete", "A pays the invoice in BTC over Lightning")
    log.append("A paid over Lightning: preimage %s..." % paid["payment_preimage"][:16])
    ok.eq(watch(lot["payment_hash"])["state"], "paid", "the lot reads paid")
    done = rcv.poll()
    ok.ok(done and done[0][0] == "claimed", "the receiver delivers the lot with the secret", str(done))
    rig.mine()
    st = watch(lot["payment_hash"])
    ok.eq(st["state"], "delivered", "and the lot reads delivered")
    claim = n("getrawtransaction", st["claim_txid"], True, n("getbestblockhash"))
    got = [o for o in claim["vout"] if o.get("asset") == token]
    ok.ok(got and RPCMOD.to_atoms(got[0]["value"]) == 100 * COIN, "A holds 100 HLX on chain")
    log.append("A's tokens delivered by the receiver: claim %s (%d vB), no signature"
               % (st["claim_txid"], claim["vsize"]))

    # --- B pays in the sale's Sequentia asset --------------------------------
    B = "02" + "bb" * 32
    rig.mine()
    plat.confirm_lock(issuer, "helios") if P.sale.status not in ("live", "partial") else None
    from test_node import Wallet  # noqa
    import watcher as W
    W.Watcher(plat, node, hrp="ert").poll()
    built = plat.build_buy(B, "helios", {"token_atoms": str(50 * COIN)},
                           {"token_address": w("getnewaddress", "", "bech32"),
                            "inputs": [usd_coin(600 * COIN)], "fee_atoms": 30_000, "fee_asset": usd})
    proc = w("walletprocesspsbt", built["pset"])
    fin = w("finalizepsbt", proc["psbt"])
    btx = n("sendrawtransaction", fin["hex"])
    rig.mine()
    ok.ok(btx, "B buys 50 HLX in the sale's asset through the SELL leaf")
    log.append("B bought 50 HLX for 500 USD in one transaction: %s" % btx)

    # --- C reserves and never pays --------------------------------------------
    W.Watcher(plat, node, hrp="ert").poll()
    C_ = "02" + "cc" * 32
    r = reserve(40, C_)
    c_lot = r["lot"]
    sign_send(r["pset"])
    rig.mine()
    ok.eq(rcv.poll(), [], "before the timeout the receiver leaves an unpaid lot alone")
    now = int(time.time())
    for i in range(12):
        n("setmocktime", now + 3600 + 900 + i * 60)
        rig.mine()
    done = rcv.poll()
    ok.ok(done and done[0][0] == "rerested", "past the timeout the lot re-rests", str(done))
    rig.mine()
    st = watch(c_lot["payment_hash"])
    ok.eq(st["state"], "returned", "and C's lot reads returned")
    spk = P.sale.script_pubkey
    rested = n("scantxoutset", "start", ["raw(%s)" % spk])
    ok.ok(any(RPCMOD.to_atoms(u["amount"]) == 40 * COIN for u in rested["unspents"]),
          "the 40 HLX rest at the sale address again")
    log.append("C's unpaid lot re-rested: %s; the sale holds %s"
               % (st.get("rerest_txid"), sorted(RPCMOD.to_atoms(u["amount"]) // COIN
                                                 for u in rested["unspents"])))

    # --- an invoice off the published price is refused ------------------------
    real = rcv.invoice

    def cheat(*a, **k):
        out = real(*a, **k)
        out["amount_msat"] += 1000
        return out
    rcv.invoice = cheat
    W.Watcher(plat, node, hrp="ert").poll()
    try:
        reserve(20, "02" + "dd" * 32)
        ok.ok(False, "an invoice off the published price is refused")
    except M.PlatformError as e:
        ok.ok("published BTC price" in str(e), "an invoice off the published price is refused", str(e))
        log.append("refused: %s" % e)
    rcv.invoice = real
    srv.shutdown()


def main():
    binary, lnd = find_node(), find_lightningd()
    if not binary or not lnd or not shutil.which("bitcoind"):
        print("skipped: needs sequentiad, bitcoind and lightningd")
        return 0
    ok, log = Checker(), []
    rig = StrictRig(binary)
    ln = None
    try:
        ln = Lightning(lnd)
        run(ok, rig, ln, log)
    finally:
        rig.stop()
        if ln:
            ln.stop()
        out = os.environ.get("LEVO_LIGHTNING_LOG")
        if out:
            Path(out).write_text("\n".join(log) + "\n")
        for line in log:
            print("  " + line)
    return ok.report()


if __name__ == "__main__":
    sys.exit(main())
