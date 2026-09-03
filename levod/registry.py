"""What a registry says an asset is.

On Sequentia an asset is a 64-hex id, and its name lives in a contract
committed at issuance. A registry maps ids to those contracts, so a wallet can
show "USDX" where the chain says
2a515539da5e6a60caa7766ecd65bac0c10d15717ddd2088844ba58f4d04b9de.

Levo asks one only to check what a project claims about its own token against
what the asset's contract says, and to show a reader which of the two they are
looking at. It is never a gate on whether an asset may be sold: a chain with no
privileged asset has no privileged registry either, an unregistered token is a
perfectly ordinary token, and a registry that is down must not stop a sale
being listed.
"""

import json
import urllib.error
import urllib.request

TIMEOUT = 4


class Answer:
    """What a registry said, and whether it said anything at all.

    `checked` is False when there was no registry to ask or it could not be
    reached, which is different from an asset that is genuinely not registered.
    """

    def __init__(self, checked=False, found=False, contract=None, error=None):
        self.checked = checked
        self.found = found
        self.contract = contract or {}
        self.error = error

    @property
    def ticker(self):
        return str(self.contract.get("ticker") or "") or None

    @property
    def name(self):
        return str(self.contract.get("name") or "") or None

    @property
    def precision(self):
        p = self.contract.get("precision")
        return int(p) if p is not None else None

    @property
    def domain(self):
        return ((self.contract.get("entity") or {}).get("domain")) or None

    def to_json(self):
        return {"checked": self.checked, "found": self.found,
                "ticker": self.ticker, "name": self.name,
                "precision": self.precision, "domain": self.domain,
                "error": self.error}


def look_up(base_url, asset_id, timeout=TIMEOUT, opener=None):
    """The registry's entry for an asset, as an Answer. Never raises."""
    if not base_url or not asset_id:
        return Answer()
    url = "%s/%s" % (str(base_url).rstrip("/"), str(asset_id).lower())
    try:
        get = opener or urllib.request.urlopen
        with get(url, timeout=timeout) as r:
            body = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return Answer(checked=True, found=False)
        return Answer(error="the registry answered %s" % e.code)
    except Exception as e:
        return Answer(error=str(e))
    contract = body.get("contract") or {}
    if not contract:
        return Answer(checked=True, found=False)
    return Answer(checked=True, found=True, contract=contract)


def disagreement(answer, ticker, decimals):
    """How a listing's claims differ from the registered contract, in words a
    project can act on, or None when they agree or nothing is registered."""
    if not (answer.checked and answer.found):
        return None
    said = []
    if answer.ticker and ticker and answer.ticker.upper() != str(ticker).upper():
        said.append("its ticker is %s, not %s" % (answer.ticker, ticker))
    if answer.precision is not None and int(decimals) != answer.precision:
        said.append("it divides into %d places, not %d" % (answer.precision, decimals))
    if not said:
        return None
    return ("this asset is registered%s and %s. A sale that names it otherwise "
            "would price and display it wrongly everywhere a wallet reads the "
            "registry" % ((" to %s" % answer.domain) if answer.domain else "",
                          " and ".join(said)))
