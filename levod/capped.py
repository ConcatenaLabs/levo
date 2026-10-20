"""The capped sale: a sale whose per-buyer caps are consensus, not Levo's policy.

A capped sale is the `sequentia/capped-sale` covenant of the
sequentia-contracts repository, a Simplicity program, vendored here in
`capped_sale/` and pinned by `capped_sale/PIN.json`:

    P2TR(NUMS, {{sale, H_TapData(terms || ALLOCATIONS)}, reclaim})

Its terms are the ordinary sale's -- the token, the payment asset, the price,
the treasury, the minimum lot, the close and the reclaim key -- plus an
allocation list: each buyer's x-only key and the most that key may buy, in
token atoms. The coin carries, as its state, the root of a 16-level Merkle tree
of (key, cap, bought) per buyer, every `bought` starting at zero.

A purchase is signed by a key on the list, over the whole transaction
(BIP340 over the program's sig_all_hash). The program checks the key is on the
list, that what it has bought plus this purchase does not pass its cap, that
the treasury is paid at output 2k for the coin at input k, and that the rest
rests again at output 2k+1 in the state with only that buyer's total raised.
So the cap holds against anyone, through Levo or not, and the sale settles
with nobody online but the buyer.

Because the state is in the address, a capped sale's address moves with every
purchase. Levo keeps the state it has seen (`bought`, one entry per list
position) and follows the coin from spend to spend.

A purchase's witness is built by running the program against the final
transaction, which this backend does through the contracts toolchain's `seqc`
binary (`LEVOD_SEQC`). Levo never signs: `seqc` gives the signature hash, the
buyer's own key signs it, and Levo assembles the witness around that signature.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import secp256k1 as EC
import sequentia_address as SA
from script import NUMS

HERE = Path(__file__).with_name("capped_sale")
PIN = json.loads((HERE / "PIN.json").read_text())
DEPTH = SA.CAPPED_SALE_DEPTH
MAX_BUYERS = SA.CAPPED_SALE_MAX_BUYERS
BUDGET_PER_BYTE, BUDGET_OFFSET, BUDGET_MAX = 4, 50, 4000050
# The covenant input's witness stack, serialized, at its largest: 2,616 bytes
# measured for a partial purchase (sequentia-contracts harness c1), rounded up.
# A fee is estimated with it before the witness exists.
WITNESS_BYTES = 2700


class CappedError(ValueError):
    pass


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _load():
    for name, want in PIN["files"].items():
        path = Path(__file__).parent / name
        got = _sha(path.read_bytes())
        if got != want:
            raise RuntimeError(
                "%s does not match capped_sale/PIN.json (%s, pinned %s): the vendored "
                "capped-sale covenant has changed, and every address derived from it "
                "may have moved" % (name, got[:16], want[:16]))
    d = SA.loads((HERE / "descriptor.json").read_text())
    if d["template_hash"] != PIN["template_hash"]:
        raise RuntimeError("the vendored descriptor is not the pinned template")
    source = (HERE / "capped_sale.simf").read_text()
    return d, source


DESCRIPTOR, SOURCE = _load()
if DESCRIPTOR["template"]["internal_key"] != NUMS.hex():
    raise RuntimeError("the capped sale's internal key must be NUMS")


def _leaf_body():
    for (kind, name, body), _ in SA._leaves(SA.model(DESCRIPTOR)["tree"]):
        if kind == "simplicity" and name == "sale":
            return body
    raise RuntimeError("the vendored descriptor has no sale leaf")


CMR = _leaf_body()["cmr"]


def internal(display_hex):
    """An asset id as the program reads it: the reverse of the display form."""
    return bytes.fromhex(display_hex)[::-1].hex()


def check_allocations(allocations):
    """[(key_hex, cap_atoms)], checked: each key a curve point, once, each cap
    positive, at most 65,536 buyers."""
    if not isinstance(allocations, list) or not allocations:
        raise CappedError("a capped sale needs an allocation list: at least one buyer's key and cap")
    if len(allocations) > MAX_BUYERS:
        raise CappedError("an allocation list holds at most %d buyers" % MAX_BUYERS)
    out, seen = [], set()
    for n, a in enumerate(allocations):
        if isinstance(a, dict):
            key, cap = a.get("key"), a.get("cap_atoms")
        else:
            key, cap = a
        key = str(key or "").strip().lower()
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise CappedError("allocation %d: a buyer's key is 64 hex characters, an x-only public key" % (n + 1))
        if EC.lift_x(int(key, 16)) is None:
            raise CappedError("allocation %d: %s… is not an x-only public key, so no signature could "
                              "ever buy with it" % (n + 1, key[:12]))
        if key in seen:
            raise CappedError("allocation %d: the key %s… is on the list twice" % (n + 1, key[:12]))
        seen.add(key)
        try:
            cap = int(str(cap).strip())
        except (TypeError, ValueError):
            raise CappedError("allocation %d: the cap is a whole number of token atoms" % (n + 1))
        if not 0 < cap < 1 << 64:
            raise CappedError("allocation %d: the cap is a positive number of token atoms" % (n + 1))
        out.append((key, cap))
    return out


class CappedCovenant:
    """A capped sale's covenant: its terms, its allocation list, and the
    state Levo has seen it in (`bought`, one entry per list position)."""

    capped = True

    def __init__(self, terms):
        self.terms = terms
        self.allocations = check_allocations(terms.allocations)
        self.keys = [k for k, _ in self.allocations]
        self.caps = [c for _, c in self.allocations]
        self.bought = [0] * len(self.allocations)
        self._cache = {}

    # --- instances ------------------------------------------------------------

    def params(self):
        t = self.terms
        return {"TOKEN": internal(t.token_asset), "PAY_ASSET": internal(t.payment_asset),
                "PRICE_NUM": "%016x" % t.price_num, "PRICE_DEN": "%016x" % t.price_den,
                "TREASURY": _sha(t.treasury_spk), "MIN_LOT": "%016x" % t.min_lot,
                "CLOSE": "%08x" % t.close_locktime, "PROJECT_KEY": t.reclaim_xonly}

    def entries(self, bought=None):
        bought = self.bought if bought is None else bought
        return [(bytes.fromhex(k), c, b) for k, c, b in zip(self.keys, self.caps, bought)]

    def root(self, bought=None):
        return SA.capped_sale_root(self.entries(bought)).hex()

    def derived(self, bought=None):
        bought = tuple(self.bought if bought is None else bought)
        key = (bought, tuple(self.caps), tuple(self.keys))
        if key not in self._cache:
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[key] = SA.derive(DESCRIPTOR, self.params(), {"ALLOCATIONS": self.root(bought)})
        return self._cache[key]

    def spk(self, bought=None):
        return bytes.fromhex(self.derived(bought)["script_pubkey"])

    @property
    def script_pubkey(self):
        return self.spk()

    @property
    def spk_hex(self):
        return self.script_pubkey.hex()

    @property
    def first_spk_hex(self):
        """The address the sale is funded at: every buyer at zero."""
        return self.spk([0] * len(self.keys)).hex()

    @property
    def reclaim_leaf(self):
        return bytes.fromhex(self.derived()["leaves"]["reclaim"]["script"])

    @property
    def tap(self):
        cov = self

        class _Tap:
            internal_key = NUMS

            @staticmethod
            def control_block(name):
                return bytes.fromhex(cov.derived()["leaves"][name]["control_block"])
        return _Tap()

    def reclaim_witness(self, project_sig):
        return [bytes(project_sig), self.reclaim_leaf, self.tap.control_block("reclaim")]

    def sell_witness(self):
        raise CappedError("a capped sale's purchase is signed by the buyer; its witness is built "
                          "around that signature, after the transaction exists")

    def verify_funding(self, onchain_spk):
        """The sale's current address, from the terms, the list and the state
        Levo holds, against the scriptPubKey on chain."""
        if isinstance(onchain_spk, str):
            onchain_spk = bytes.fromhex(onchain_spk)
        if bytes(onchain_spk) != self.script_pubkey:
            raise ValueError("funded scriptPubKey %s does not match the capped sale's terms, "
                             "allocation list and state, which derive %s"
                             % (bytes(onchain_spk).hex(), self.spk_hex))
        return True

    # --- buyers -----------------------------------------------------------------

    def position(self, key_hex):
        key = str(key_hex or "").strip().lower()
        try:
            return self.keys.index(key)
        except ValueError:
            raise CappedError("that key is not on this sale's allocation list, and the covenant "
                              "sells only to the keys on it")

    def left_for(self, position):
        return self.caps[position] - self.bought[position]

    def after(self, position, amount, bought=None):
        nb = list(self.bought if bought is None else bought)
        nb[position] += int(amount)
        if nb[position] > self.caps[position]:
            raise CappedError("that is past the buyer's cap")
        return nb

    def proof(self, position, bought=None):
        return SA.capped_sale_proof(self.entries(bought), position)

    def follow(self, rest_spk_hex, amount, bought=None):
        """Which buyer made a purchase of `amount` whose rest landed at
        `rest_spk_hex`: the one position whose successor state derives that
        script. For a purchase Levo did not build."""
        base = list(self.bought if bought is None else bought)
        for i in range(len(self.keys)):
            if base[i] + amount > self.caps[i]:
                continue
            nb = list(base)
            nb[i] += amount
            if self.spk(nb).hex() == rest_spk_hex.lower():
                return i, nb
        return None

    # --- the witness ------------------------------------------------------------

    def _request(self, tx_hex, utxos_hex, index, genesis, bought=None):
        return {"source": SOURCE, "tx": tx_hex, "utxos": utxos_hex, "index": int(index),
                "control_block": self.derived(bought)["leaves"]["sale"]["control_block"],
                "genesis": genesis, "prune": True}

    def witness_values(self, position, amount, sig_hex, bought=None):
        bought = list(self.bought if bought is None else bought)
        t = self.terms
        lit = "[" + ", ".join("(0x%s, %s)" % (s.hex(), "true" if left else "false")
                              for s, left in self.proof(position, bought)) + "]"
        v = lambda value, ty: {"value": value, "type": ty}
        return {"TOKEN": v("0x" + internal(t.token_asset), "u256"),
                "PAY_ASSET": v("0x" + internal(t.payment_asset), "u256"),
                "PRICE_NUM": v(str(t.price_num), "u64"), "PRICE_DEN": v(str(t.price_den), "u64"),
                "TREASURY": v("0x" + _sha(t.treasury_spk), "u256"),
                "MIN_LOT": v(str(t.min_lot), "u64"),
                "ALLOCATIONS": v("0x" + self.root(bought), "u256"),
                "BUYER_KEY": v("0x" + self.keys[position], "Pubkey"),
                "CAP": v(str(self.caps[position]), "u64"),
                "BOUGHT": v(str(bought[position]), "u64"),
                "AMOUNT": v(str(int(amount)), "u64"),
                "PROOF": v(lit, "[(u256, bool); %d]" % DEPTH),
                "BUYER_SIG": v("0x" + sig_hex, "Signature")}

    def sighash(self, tx_hex, utxos_hex, index, genesis, seqc=None, bought=None):
        """The message the buyer signs: the program's sig_all_hash for this
        input of this transaction."""
        return run_seqc(self._request(tx_hex, utxos_hex, index, genesis, bought), seqc)["sighash_all"]

    def witness(self, tx_hex, utxos_hex, index, genesis, position, amount, sig_hex, seqc=None,
                bought=None):
        """The covenant input's witness stack, the program run and pruned
        against the transaction with the buyer's signature. Refused, with the
        program's own reason, unless the program accepts it."""
        sig_hex = str(sig_hex or "").strip().lower()
        if len(sig_hex) != 128 or any(c not in "0123456789abcdef" for c in sig_hex):
            raise CappedError("the buyer's signature is 64 bytes of hex")
        msg = bytes.fromhex(self.sighash(tx_hex, utxos_hex, index, genesis, seqc, bought))
        if not EC.schnorr_verify(msg, bytes.fromhex(sig_hex), bytes.fromhex(self.keys[position])):
            raise CappedError("that signature is not the buyer's over this purchase: it does not "
                              "verify under the key on the allocation list")
        req = self._request(tx_hex, utxos_hex, index, genesis, bought)
        req["witness"] = self.witness_values(position, amount, sig_hex, bought)
        try:
            r = run_seqc(req, seqc)
        except CappedError as e:
            # Satisfying the program runs it; a run that fails is the
            # program refusing this purchase, and seqc says where.
            raise CappedError("the sale program refuses this purchase: %s" % str(e).replace("seqc: ", ""))
        if not r.get("executed"):
            raise CappedError("the sale program refuses this purchase: %s" % r.get("exec_error"))
        stack = [r["witness_hex"], r["program_hex"], CMR, self.derived(bought)["leaves"]["sale"]["control_block"]]
        size = _compact(len(stack)) + sum(_compact(len(s) // 2) + len(s) // 2 for s in stack)
        budget = min(BUDGET_PER_BYTE * size + BUDGET_OFFSET, BUDGET_MAX)
        if (r["cost_milli"] + 999) // 1000 > budget:
            raise CappedError("this purchase's program would cost more than its witness earns")
        return stack


def _compact(n):
    return 1 if n < 0xfd else 3 if n <= 0xffff else 5


def seqc_path(seqc=None):
    return seqc or os.environ.get("LEVOD_SEQC") or "seqc"


def run_seqc(req, seqc=None):
    """One request to `seqc run`, the contracts toolchain's program runner."""
    path = seqc_path(seqc)
    try:
        p = subprocess.run([path, "run"], input=json.dumps(req).encode(), capture_output=True,
                           timeout=60)
    except FileNotFoundError:
        raise CappedError("capped sales need the contracts toolchain's seqc binary (LEVOD_SEQC); "
                          "%s was not found" % path)
    except subprocess.TimeoutExpired:
        raise CappedError("seqc did not answer within a minute")
    if p.returncode != 0:
        raise CappedError("seqc failed: %s" % p.stderr.decode(errors="replace")[-300:])
    r = json.loads(p.stdout.decode())
    if "error" in r:
        raise CappedError("seqc: %s" % r["error"])
    if r.get("cmr") != CMR:
        raise CappedError("seqc compiles the capped sale to %s, not the pinned %s: it is not the "
                          "pinned compiler" % (r.get("cmr"), CMR))
    return r
