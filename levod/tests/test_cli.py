#!/usr/bin/env python3
"""bin/levo, against a real node and a real levod.

The CLI is the whole platform for anyone who runs a node: it signs in, lists,
locks, prices, builds, signs, broadcasts and reclaims, and it is seven hundred
lines that no other test in this repository executes. Everything here has been
proved through the platform's own API instead, which is not the same thing --
the CLI has its own input handling, its own unit conversions, its own wallet
calls and its own idea of what levod answers.

It starts a throwaway sequentiad, a levod against it, and drives `bin/levo` as
a person would. It reports itself skipped when there is no node binary, the way
`test_node.py` does.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import secp256k1 as K  # noqa: E402
from test_node import COIN, Rig, find_node, free_port  # noqa: E402


class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def ok(self, cond, what, detail=""):
        if cond:
            self.passed += 1
        else:
            self.failed.append("%s%s" % (what, (" (%s)" % detail) if detail else ""))

    def eq(self, got, want, what):
        self.ok(got == want, what, "got %r, want %r" % (got, want))

    def report(self):
        for f in self.failed:
            print("  FAIL %s" % f)
        print("%d passed, %d failed" % (self.passed, len(self.failed)))
        return 0 if not self.failed else 1


class Levod:
    """The real server, against the rig's node, on a port of its own."""

    def __init__(self, rig, port, payment_asset):
        self.state = Path(tempfile.mkdtemp()) / "state.json"
        env = dict(
            os.environ,
            LEVOD_PORT=str(port), LEVOD_HOST="127.0.0.1",
            LEVOD_STATE=str(self.state), LEVOD_SECRET="cli-test-secret",
            LEVOD_RPC_URL=rig.url, LEVOD_RPC_USER="levo", LEVOD_RPC_PASSWORD="levo",
            LEVOD_PAYMENT_ASSET=payment_asset, LEVOD_PAYMENT_LABEL="PAY",
            LEVOD_HRP="ert", LEVOD_WATCH_SECONDS="3600",
            LEVOD_API_ONLY="1",          # the CLI drill serves no app
            # A regtest mines on demand, so the tip must never be reused: a
            # block can arrive and be acted on in the same second.
            LEVOD_CHAIN_TTL="0",
            # Every account may list here: the tier gate has its own tests, and
            # a fresh regtest has no stake at all.
            LEVOD_TIERS=json.dumps([{"name": "Everyone", "min_stake": 0,
                                     "cap": 1000000, "may_list": True}]),
        )
        self.log = open(os.path.join(tempfile.mkdtemp(), "levod.log"), "w+")
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "levod" / "server.py")],
                                     stdout=self.log, stderr=subprocess.STDOUT, env=env)
        self.url = "http://127.0.0.1:%d" % port
        for _ in range(80):
            try:
                urllib.request.urlopen(self.url + "/api/health", timeout=2).read()
                return
            except Exception:
                if self.proc.poll() is not None:
                    self.log.seek(0)
                    raise RuntimeError("levod exited:\n" + self.log.read())
                time.sleep(0.25)
        raise RuntimeError("levod did not start")

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=20)
        except Exception:
            self.proc.kill()
        self.log.close()


def run(ok, rig, levod, env):
    def levo(*args, **kw):
        """Run the CLI as a person would, and hand back what they would see."""
        r = subprocess.run([sys.executable, str(ROOT / "bin" / "levo")] + list(args),
                           capture_output=True, text=True, env=env, timeout=180)
        if kw.get("expect_failure"):
            ok.ok(r.returncode != 0, "%s fails" % " ".join(args[:2]), r.stdout[-200:])
        else:
            ok.eq(r.returncode, 0, "levo %s" % " ".join(args[:2]))
        return (r.stdout or "") + (r.stderr or "")

    w = rig.w

    # --- it knows who it is ------------------------------------------------
    out = levo("whoami")
    ok.ok("account" in out and "tier" in out, "whoami says who and what", out[:120])

    # --- a listing file it printed itself is one it accepts ----------------
    example = json.loads(subprocess.run(
        [sys.executable, str(ROOT / "bin" / "levo"), "create", "--example"],
        capture_output=True, text=True, env=env, timeout=60).stdout)
    ok.ok("project" in example and "terms" in example,
          "create --example prints a listing file")

    keys = json.loads(levo("keygen"))
    ok.ok(len(keys["reclaim_xonly"]) == 64 and len(keys["reclaim_secret_hex"]) == 64,
          "keygen prints a key pair")

    token = w("issueasset", assetamount=100_000, tokenamount=0, blind=False,
              fee_asset="bitcoin")["asset"]
    rig.mine()
    treasury = w("getnewaddress", "", "bech32")
    spec = {
        "project": {"slug": "cli-sale", "name": "CLI Sale", "ticker": "CLI",
                    "decimals": 8, "summary": "A sale driven from the command line.",
                    "description": "Listed, locked, bought and reclaimed by bin/levo."},
        "terms": {"token_asset": token, "price_num": 1, "price_den": 4,
                  "min_lot": 10 * COIN, "total_atoms": 1_000 * COIN,
                  "close_locktime": rig.n("getblockcount") + 8,
                  "treasury_address": treasury,
                  "reclaim_xonly": keys["reclaim_xonly"]},
    }
    listing = Path(tempfile.mkdtemp()) / "listing.json"
    listing.write_text(json.dumps(spec))
    out = levo("create", str(listing))
    ok.ok("listed cli-sale" in out, "create lists the project", out[:160])

    # --- locking sends the exact allocation, in one output -----------------
    out = levo("lock", "cli-sale")
    ok.ok("locked at" in out, "lock funds the sale and confirms it", out[-160:])
    rig.mine()
    detail = json.loads(levo("show", "cli-sale"))
    ok.eq(detail["sale"]["status"], "live", "and the sale is live")
    ok.eq(int(detail["sale"]["locked_atoms"]), 1_000 * COIN,
          "holding exactly what was published")

    # --- a second lock sends nothing ---------------------------------------
    #
    # levod refuses to lock a funded sale twice, correctly -- but it refused
    # after the tokens had gone, and they land at an address whose sell leaf
    # sells them to anyone at the sale's price. The refusal has to come first.
    before = sum(int(round(float(u["amount"]) * 1e8)) for u in w("listunspent", 0)
                 if u.get("asset") == token)
    out = levo("lock", "cli-sale", expect_failure=True)
    ok.ok("already funded" in out, "a second lock is refused", out[-200:])
    after = sum(int(round(float(u["amount"]) * 1e8)) for u in w("listunspent", 0)
                if u.get("asset") == token)
    ok.eq(after, before, "and not one token left the wallet")

    # --- verify says what the address enforces -----------------------------
    out = levo("verify", "cli-sale")
    ok.ok("RESULT" in out and "OK" in out, "verify rebuilds the address and agrees",
          out[-200:])
    ok.ok("committed" in out and "not committed" in out,
          "and says which terms the address is made of")

    ok.ok(all(not u.get("amountcommitment") for u in w("listunspent", 0)),
          "locking left every output in the wallet explicit, which is what a "
          "covenant can read")

    # --- a purchase, end to end --------------------------------------------
    out = levo("buy", "cli-sale", "--tokens", "100", "--dry-run")
    ok.ok("would broadcast" in out, "a dry run builds and is accepted by the node",
          out[-200:])
    out = levo("buy", "cli-sale", "--tokens", "100")
    ok.ok("bought." in out, "and a real buy is broadcast", out[-200:])
    rig.mine()
    detail = json.loads(levo("show", "cli-sale"))
    ok.eq(detail["sale"]["status"], "partial", "the sale is partial after it")
    ok.eq(int(detail["sale"]["locked_atoms"]), 900 * COIN, "holding the remainder")
    positions = json.loads(levo("whoami").split("\n")[0]) if False else None

    # --- a record that did not go through can be finished later ------------
    out = levo("record", "cli-sale", "--txid", "ff" * 32, "--tokens", "10",
               expect_failure=True)
    ok.ok("has not seen" in out, "recording a transaction the node never saw is refused",
          out[-160:])
    ok.ok("wait a few seconds" in out or "on chain either way" in out,
          "and says a just-broadcast purchase may simply not have arrived yet",
          out[-200:])

    # --- verify judges the LISTING, not how far the sale has got -----------
    #
    # A sold-out, reclaimed or just-bought sale has no unspent funding, and
    # calling that a failure told a reader "do not buy from this listing" about
    # every sale that reached its natural end -- and exited 1, which any script
    # gating on it would read as a compromised sale.
    out = levo("verify", "cli-sale")
    ok.ok("OK" in out and "FAILED" not in out,
          "verify still passes after a purchase has moved the sale", out[-240:])

    # --- sales lists it, and says how many there are -----------------------
    out = levo("sales")
    ok.ok("cli-sale" in out, "sales lists it", out[:200])
    out = levo("sales", "--status", "open")
    ok.ok("cli-sale" in out, "and the open filter finds it")

    # --- what it refuses ----------------------------------------------------
    out = levo("buy", "cli-sale", "--tokens", "0.000000001", expect_failure=True)
    ok.ok("decimal" in out or "minimum" in out,
          "a purchase below the token's precision is refused with a reason", out[-160:])
    out = levo("buy", "cli-sale", "--tokens", "100", "--fee-asset", "NOSUCHLABEL",
               expect_failure=True)
    ok.ok("dumpassetlabels" in out,
          "an unknown fee asset names where the labels come from", out[-160:])

    # --- what survives Levo itself -----------------------------------------
    #
    # A sale is a covenant on a public chain and Levo is a place that shows it.
    # If the platform is gone -- the state file lost, the server retired -- the
    # tokens are still there and the project's key still opens the reclaim
    # path. What is needed is the terms the address was made of.
    terms_file = Path(tempfile.mkdtemp()) / "sale.json"
    out = levo("terms", "cli-sale", "--out", str(terms_file))
    ok.ok(terms_file.is_file(), "terms writes the file that rebuilds the sale")
    kept = json.loads(terms_file.read_text())
    ok.eq(kept["script_pubkey"], detail["sale"]["script_pubkey"],
          "carrying the address it derives")
    ok.ok("no Levo involved at all" in kept["keep"], "and saying what it is for")

    # --- the reclaim, after the close --------------------------------------
    close = int(spec["terms"]["close_locktime"])
    while rig.n("getblockcount") <= close:
        rig.mine()
    out = levo("reclaim", "cli-sale", "--reclaim-key", keys["reclaim_secret_hex"])
    ok.ok("reclaimed" in out.lower() or "broadcast" in out.lower(),
          "reclaim sweeps what did not sell", out[-240:])
    rig.mine()
    detail = json.loads(levo("show", "cli-sale"))
    ok.ok(int(detail["sale"]["locked_atoms"]) == 0 or detail["sale"]["status"] in
          ("closed", "reclaimed"), "and the covenant is empty",
          "%s / %s" % (detail["sale"]["status"], detail["sale"]["locked_atoms"]))
    # A swept sale has no unspent funding, which is what the end of a sale
    # looks like -- not a listing to be warned away from.
    out = levo("verify", "cli-sale")
    ok.ok("OK" in out and "FAILED" not in out,
          "verify passes on a sale that has been reclaimed", out[-260:])
    ok.ok("Nothing is resting at it now" in out,
          "and says separately that nothing rests there", out[-260:])

    # --- and the same sweep with no Levo in it at all -----------------------
    #
    # A second sale, closed, reclaimed from its terms file alone: no session, no
    # API call, nothing but a node, the file and the key.
    keys2 = json.loads(levo("keygen"))
    spec2 = json.loads(json.dumps(spec))
    spec2["project"]["slug"] = "rescue-me"
    spec2["project"]["ticker"] = "RSC"
    spec2["terms"]["reclaim_xonly"] = keys2["reclaim_xonly"]
    spec2["terms"]["close_locktime"] = rig.n("getblockcount") + 3
    spec2["terms"]["total_atoms"] = 500 * COIN
    listing2 = Path(tempfile.mkdtemp()) / "listing.json"
    listing2.write_text(json.dumps(spec2))
    levo("create", str(listing2))
    levo("lock", "rescue-me")
    rig.mine()
    rescue_terms = Path(tempfile.mkdtemp()) / "rescue.json"
    levo("terms", "rescue-me", "--out", str(rescue_terms))
    # A sale somebody has BOUGHT from, which is the sale a rescue is for: the
    # covenant now rests on a remainder smaller than the total it published,
    # and a rescue that insisted on the published total could sweep only a sale
    # nobody had ever touched.
    out = levo("buy", "rescue-me", "--tokens", "50")
    ok.ok("bought." in out, "a purchase moves the sale that will be rescued", out[-160:])
    rig.mine()
    while rig.n("getblockcount") <= int(spec2["terms"]["close_locktime"]):
        rig.mine()
    # No --hrp: the file remembers which chain it was written on, and a
    # rescue that guessed a prefix derived an address that is not the sale's.
    out = levo("rescue", "--terms", str(rescue_terms),
               "--reclaim-key", keys2["reclaim_secret_hex"], "--dry-run")
    ok.ok("would broadcast" in out,
          "a sale can be reclaimed from its terms file alone, with no Levo", out[-200:])
    out = levo("rescue", "--terms", str(rescue_terms), "--hrp", "ert",
               "--reclaim-key", keys2["reclaim_secret_hex"])
    ok.ok("reclaimed." in out, "and really swept", out[-160:])


def main():
    binary = find_node()
    if not binary:
        print("no sequentiad found; skipping the CLI test "
              "(set SEQUENTIAD or SEQUENTIA_SRC)")
        return 0
    ok = Checker()
    rig = Rig(binary)
    levod = None
    try:
        pay = rig.w("issueasset", assetamount=1_000_000, tokenamount=0, blind=False,
                    fee_asset="bitcoin")["asset"]
        rig.mine()
        rig.n("setfeeexchangerates", {"bitcoin": COIN, pay: COIN})
        # `sequentia-cli` reads the datadir's own config, and the rig passes
        # everything on the command line, so it is written out here: without it
        # the CLI would look for a node on the default port.
        Path(rig.root, "elements.conf").write_text(
            "chain=elementsregtest\n[elementsregtest]\n"
            "rpcport=%d\nrpcuser=levo\nrpcpassword=levo\n" % rig.port)
        levod = Levod(rig, free_port(), pay)
        env = dict(os.environ, LEVO_URL=levod.url,
                   SEQUENTIA_CLI=str(Path(binary).parent / "sequentia-cli"),
                   SEQUENTIA_DATADIR=rig.root, SEQUENTIA_WALLET="levo",
                   LEVO_SESSION=os.path.join(tempfile.mkdtemp(), "session.json"))
        run(ok, rig, levod, env)
    except Exception:
        import traceback
        ok.failed.append("the drive raised:\n" + traceback.format_exc())
    finally:
        if levod:
            levod.stop()
        rig.stop()
    return ok.report()


if __name__ == "__main__":
    sys.exit(main())
