"""A small JSON-RPC client for a Sequentia node.

Levo reads the chain and never writes to it. The node is asked about stake
(`getstakerinfo`), about outputs (`gettxout`, `scantxoutset`, `getrawmempool`,
`getrawtransaction`), about the tip and its blocks (`getblockchaininfo`,
`getblockhash`, `getblock`, `getblockheader`), and about fees (`getfeeexchangerates`, `dumpassetlabels`,
`getmempoolinfo`). There is no wallet call here on purpose: levod holds no keys
and can move no funds, so a compromised levod can misinform users but cannot
rob them.

Amounts come back from the node as decimals. They are parsed as `Decimal`, not
as floats, because a float loses atoms above 2**53 and a sale's size is not a
number to round.
"""

import json
import os
import urllib.request
import urllib.error
from base64 import b64encode
from decimal import Decimal
from pathlib import Path


class RPCError(RuntimeError):
    pass


def to_atoms(value, decimals=8):
    """Atoms for an amount the node reported, exactly."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    d = Decimal(str(value)) if not isinstance(value, Decimal) else value
    return int((d * (10 ** decimals)).to_integral_value())


class NodeRPC:
    def __init__(self, url=None, user=None, password=None, cookie=None, timeout=20):
        self.url = url or os.environ.get("LEVOD_RPC_URL", "http://127.0.0.1:18776")
        self.timeout = timeout
        self._user = user or os.environ.get("LEVOD_RPC_USER")
        self._password = password or os.environ.get("LEVOD_RPC_PASSWORD")
        self._cookie = None
        if not self._user:
            cookie = cookie or os.environ.get("LEVOD_RPC_COOKIE")
            if cookie:
                self._cookie = Path(cookie).expanduser()
                if not self._cookie.is_file():
                    raise RPCError("LEVOD_RPC_COOKIE names %s, which does not exist; "
                                   "start the node first, or set LEVOD_RPC_USER and "
                                   "LEVOD_RPC_PASSWORD" % self._cookie)
        self._id = 0

    def with_timeout(self, seconds):
        """The same connection with a different patience, for the watcher."""
        other = NodeRPC.__new__(NodeRPC)
        other.__dict__.update(self.__dict__)
        other.timeout = seconds
        return other

    def _auth(self):
        user, password = self._user, self._password
        if not user and self._cookie is not None:
            # The node rewrites its cookie on every start, so it is read on
            # every call rather than once: a node restart must not leave levod
            # answering 401 for the rest of its life.
            try:
                user, password = self._cookie.read_text().strip().split(":", 1)
            except OSError:
                return None
        if not user:
            return None
        return b64encode(("%s:%s" % (user, password or "")).encode()).decode()

    def call(self, method, *params):
        self._id += 1
        body = json.dumps({"jsonrpc": "1.0", "id": self._id,
                           "method": method, "params": list(params)}).encode()
        req = urllib.request.Request(self.url, data=body,
                                     headers={"Content-Type": "application/json"})
        auth = self._auth()
        if auth:
            req.add_header("Authorization", "Basic " + auth)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.loads(r.read(), parse_float=Decimal)
        except urllib.error.HTTPError as e:
            # The node reports RPC-level failures with a non-2xx status and a
            # JSON body; surface its message rather than a bare 500.
            try:
                payload = json.loads(e.read(), parse_float=Decimal)
            except Exception:
                raise RPCError("%s: HTTP %s" % (method, e.code))
        except Exception as e:
            raise RPCError("%s: %s" % (method, e))
        if payload.get("error"):
            raise RPCError("%s: %s" % (method, payload["error"].get("message", payload["error"])))
        return payload.get("result")

    # --- the questions Levo actually asks ---------------------------------

    def staker_weights(self):
        """{staker public key hex: stake weight in SEQ atoms}.

        Weights keyed by SIGNER: what the chain uses to produce blocks. A
        delegated stake appears here under the pool operator, not its owner.
        """
        return {str(k).lower(): int(v) for k, v in (self.call("getstakerinfo") or {}).items()}

    def controller_weights(self):
        """{controller public key hex: {weight_atoms, delegated, signer}}.

        Weight keyed by the key that OWNS the stake, which is the question Levo
        is actually asking. A stake delegated to a pool still belongs to the
        controller -- delegation lends block-signing rights and never the coins,
        and only the controller can ever spend them -- so it counts for the
        controller here. The pool operator is credited with its own stake only,
        rather than with weight its depositors put up.

        Returns (weights, by_controller): True when the node answered by
        controller, False when it could only answer by signer (an older node),
        and None when the chain has no proof of stake at all, so there is
        nobody to report. Each is said out loud downstream, because silently
        reporting a delegator as having no stake would lock them out with no
        explanation.
        """
        try:
            raw = self.call("getstakerinfo", True, True) or {}
        except RPCError as e:
            if _no_staking(e):
                return {}, None            # no staking on this chain at all
            try:
                return ({k: {"weight_atoms": v, "delegated": False, "signer": None}
                         for k, v in self.staker_weights().items()}, False)
            except RPCError as e2:
                if _no_staking(e2):
                    return {}, None
                raise
        out = {}
        for k, v in raw.items():
            if isinstance(v, dict):
                out[str(k).lower()] = {
                    "weight_atoms": int(v.get("weight", 0)),
                    "delegated": bool(v.get("delegated")),
                    "signer": (v.get("signer") or "").lower() or None,
                }
            else:
                out[str(k).lower()] = {"weight_atoms": int(v),
                                       "delegated": False, "signer": None}
        return out, True

    def chain_info(self):
        return self.call("getblockchaininfo") or {}

    def chain_height(self):
        return int(self.chain_info()["blocks"])

    def chain_name(self):
        """The chain the node is on ('test', 'sequentia', 'elementsregtest'...)."""
        return str(self.chain_info().get("chain") or "")

    def median_time(self):
        """The chain's own clock: median time past, which is what a locktime
        is judged against. Trails wall time by a few block intervals."""
        info = self.chain_info()
        mt = info.get("mediantime")
        return int(mt) if mt is not None else None

    def txout(self, txid, vout, include_mempool=True):
        """The unspent output, or None if it does not exist or has been spent."""
        return self.call("gettxout", str(txid), int(vout), bool(include_mempool))

    def min_relay_fee_atoms_per_kvb(self):
        """The node's relay floor, in reference units per kvB, or None."""
        try:
            info = self.call("getmempoolinfo") or {}
            v = info.get("minrelaytxfee")
            return to_atoms(v) if v is not None else None
        except RPCError:
            return None


def _no_staking(err):
    """A node on a chain without proof of stake has no stakers to report.

    That is an answer -- nobody has a tier -- rather than an outage, so it must
    not turn every sign-in into 'the node refused the query'.
    """
    return "con_pos" in str(err) or "Proof-of-Stake" in str(err)
