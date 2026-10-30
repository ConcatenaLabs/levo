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


def find_holdplugin():
    c = os.environ.get("LEVO_HOLDINVOICE") or os.path.expanduser(
        "~/seqln/contrib/holdinvoice-seq/holdinvoice.py")
    return c if os.path.isfile(c) else None


def find_lightningd():
    for c in (os.environ.get("LEVO_LIGHTNINGD"), shutil.which("lightningd")):
        if c and os.access(c, os.X_OK):
            return c
    return None


class Lightning:
    """bitcoind regtest and two lightningd, a channel from the buyer to the receiver."""

    def __init__(self, lightningd, holdplugin):
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
            if i == 1:
                # The project's node holds a payment until the lot is delivered.
                args.append("--plugin=" + holdplugin)
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

    def sale_of(slug):
        sale = plat.projects[slug].sale
        f = sale.funding
        return sale.terms, {"txid": f["txid"], "vout": f["vout"], "atoms": sale.locked_atoms}
    clock = {"skew": 0}
    rcv = RCV.Receiver(RECEIVER_SECRET, RCV.Cln(ln.receiver_command()), node, sale_of,
                       os.path.join(rig.root, "receiver.json"), ["helios"], invoice_expiry=600,
                       now=lambda: time.time() + clock["skew"])
    srv = ThreadingHTTPServer(("127.0.0.1", rport), RCV.make_handler(rcv, "ert"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # The receiver's own coins pay each lock's fee and fund its pot.
    import address as ADDR
    own = ADDR.from_script_pubkey(rcv.own_spk.hex(), "ert")
    for _ in range(4):
        w("sendtoaddress", address=own, amount="0.01", assetlabel=usd, fee_asset_label="bitcoin")
    rig.mine()

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
                           "bond_atoms": 30_000, "fee_allowance": 20_000,
                           "timeout_seconds": 3600}}
    plat.list_project(issuer, {"slug": "helios", "name": "Helios", "ticker": "HLX",
                               "summary": "s", "description": "d"}, terms)
    P = plat.projects["helios"]
    w("sendtoaddress", address=plat.sale_address(P.sale), amount=10_000, assetlabel=token,
      fee_asset_label="bitcoin")
    rig.mine()
    plat.confirm_lock(issuer, "helios")
    ok.eq(P.sale.status, "live", "a sale priced in BTC is listed and live")
    log.append("sale helios at %s, priced %d sats per token"
               % (plat.sale_address(P.sale), P.sale.terms.lightning.sats_for(COIN)))
    import watcher as W

    def order(tokens, buyer, address):
        return plat.lightning_reserve(buyer, "helios", {"token_atoms": str(tokens * COIN),
                                                        "token_address": address})

    def watch(h):
        return plat.lightning_status("helios", h)

    def pay_async(bolt11):
        res = {}

        def go():
            try:
                res["r"] = ln.cli(1, "pay", bolt11)
            except Exception as e:
                res["e"] = str(e)
        th = threading.Thread(target=go, daemon=True)
        th.start()
        return th, res

    def until(cond, what, secs=60):
        end = time.time() + secs
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.5)
        raise RuntimeError("timed out waiting for " + what)

    def holds(addr, asset):
        got = n("scantxoutset", "start", ["addr(%s)" % addr])
        return sum(RPCMOD.to_atoms(u["amount"]) for u in got["unspents"] if u.get("asset") == asset)

    # --- A holds only BTC, and pays over Lightning ----------------------------
    A = "02" + "aa" * 32
    a_addr = w("getnewaddress", "", "bech32")
    lot = order(100, A, a_addr)["lot"]
    ok.eq(lot["sats"], 100_000, "100 tokens at 1,000 sats are 100,000 sats")
    ok.eq(watch(lot["payment_hash"])["state"], "awaiting_payment", "an order waits for its payment")
    log.append("A ordered 100 HLX: invoice for %d sats; A holds no Sequentia asset" % lot["sats"])
    th, res = pay_async(lot["bolt11"])
    until(lambda: watch(lot["payment_hash"])["state"] == "paid", "the payment to be held")
    ok.ok(th.is_alive(), "the payment is held, not settled, while the lot is locked")
    done = rcv.poll()
    ok.ok(done and done[0][0] == "reserved", "the receiver locks the lot once the payment is held", str(done))
    rig.mine()
    done = rcv.poll()
    ok.ok(done and done[0][0] == "delivered", "then delivers it with the secret and settles", str(done))
    th.join(60)
    ok.eq((res.get("r") or {}).get("status"), "complete", "A's Lightning payment completes", )
    rig.mine()
    st = watch(lot["payment_hash"])
    ok.eq(st["state"], "delivered", "the order reads delivered")
    ok.eq(holds(a_addr, token), 100 * COIN, "A holds 100 HLX on chain, having paid only BTC")
    ok.eq(holds(a_addr, usd), 30_000 - 20_000, "and the fee pot's remainder")
    claim = n("getrawtransaction", st["claim_txid"], True, n("getbestblockhash"))
    log.append("A paid %d sats over Lightning (held until delivery); lock %s, delivery %s (%d vB, "
               "no signature); A holds 100 HLX" % (lot["sats"], st["reserve_txid"], st["claim_txid"],
                                                   claim["vsize"]))

    # --- B pays in the sale's Sequentia asset --------------------------------
    W.Watcher(plat, node, hrp="ert").poll()
    B = "02" + "bb" * 32
    a = w("getnewaddress", "", "bech32")
    txid = w("sendtoaddress", address=a, amount="600", assetlabel=usd, fee_asset_label="bitcoin")
    rig.mine()
    vout = [o["n"] for o in w("gettransaction", txid, True, True)["decoded"]["vout"]
            if o["scriptPubKey"].get("address") == a][0]
    b_addr = w("getnewaddress", "", "bech32")
    built = plat.build_buy(B, "helios", {"token_atoms": str(50 * COIN)},
                           {"token_address": b_addr, "inputs": [{"txid": txid, "vout": vout}],
                            "fee_atoms": 30_000, "fee_asset": usd})
    fin = w("finalizepsbt", w("walletprocesspsbt", built["pset"])["psbt"])
    btx = n("sendrawtransaction", fin["hex"])
    rig.mine()
    ok.eq(holds(b_addr, token), 50 * COIN, "B buys 50 HLX in the sale's asset through the SELL leaf")
    log.append("B bought 50 HLX for 500 USD in one transaction: %s" % btx)

    # --- C's payment goes away after the lock: the lot re-rests ---------------
    W.Watcher(plat, node, hrp="ert").poll()
    C_ = "02" + "cc" * 32
    lot_c = order(40, C_, w("getnewaddress", "", "bech32"))["lot"]
    th, res = pay_async(lot_c["bolt11"])
    until(lambda: watch(lot_c["payment_hash"])["state"] == "paid", "C's payment to be held")
    done = rcv.poll()
    ok.ok(done and done[0][0] == "reserved", "C's lot is locked", str(done))
    # The held payment fails back before delivery (as when it expires).
    ln.cli(0, "holdinvoicecancel", lot_c["payment_hash"])
    th.join(60)
    ok.ok("e" in res or (res.get("r") or {}).get("status") != "complete", "C's payment is failed back")
    rig.mine()
    ok.eq(rcv.poll(), [], "with no payment held the receiver does not deliver, and waits out the timeout")
    now = int(time.time())
    for i in range(12):
        n("setmocktime", now + 3600 + 900 + i * 60)
        rig.mine()
    done = rcv.poll()
    ok.ok(done and done[0][0] == "returned", "past the timeout the lot re-rests", str(done))
    rig.mine()
    st = watch(lot_c["payment_hash"])
    ok.eq(st["state"], "returned", "and C's order reads returned")
    rested = n("scantxoutset", "start", ["raw(%s)" % P.sale.script_pubkey])
    ok.ok(any(RPCMOD.to_atoms(u["amount"]) == 40 * COIN for u in rested["unspents"]),
          "the 40 HLX rest at the sale address again")
    log.append("C's payment failed back after the lock; the lot re-rested: %s; the sale holds %s"
               % (st.get("rerest_txid"), sorted(RPCMOD.to_atoms(u["amount"]) // COIN
                                                 for u in rested["unspents"])))

    # --- an order nobody pays expires, and nothing is locked -------------------
    lot_d = order(20, "02" + "dd" * 32, w("getnewaddress", "", "bech32"))["lot"]
    clock["skew"] = 700
    done = rcv.poll()
    ok.ok(any(d[0] == "expired" and d[1] == lot_d["payment_hash"] for d in done),
          "an unpaid order expires and locks nothing", str(done))
    clock["skew"] = 0

    # --- an invoice off the published price is refused ------------------------
    real = rcv.invoice

    def cheat(*a, **k):
        out = real(*a, **k)
        out["amount_msat"] += 1000
        return out
    rcv.invoice = cheat
    try:
        order(20, "02" + "ee" * 32, w("getnewaddress", "", "bech32"))
        ok.ok(False, "an invoice off the published price is refused")
    except M.PlatformError as e:
        ok.ok("published BTC price" in str(e), "an invoice off the published price is refused", str(e))
        log.append("refused: %s" % e)
    rcv.invoice = real
    srv.shutdown()


def main():
    binary, lnd, hold = find_node(), find_lightningd(), find_holdplugin()
    if not binary or not lnd or not hold or not shutil.which("bitcoind"):
        print("skipped: needs sequentiad, bitcoind, lightningd and the holdinvoice plugin "
              "($LEVO_HOLDINVOICE)")
        return 0
    ok, log = Checker(), []
    rig = StrictRig(binary)
    ln = None
    try:
        ln = Lightning(lnd, hold)
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
