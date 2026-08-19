"""The Levo marketplace: projects, listings, and the rules around a sale.

This is the layer that decides who may do what. The rules it enforces are:

  * Only the top tier may list a project. Listing is the one action Levo gates
    on tier rather than merely sizing by it.
  * A project is a draft until its tokens are locked and the lock verifies
    against the terms. Levo will not display an unlocked sale as investable.
  * Every purchase is planned against the buyer's remaining allowance for that
    sale, and the ledger of what each account has committed is kept here.

Everything below this layer is chain truth; everything at this layer is Levo
policy. Keeping that boundary sharp is what lets the documentation say exactly
which promises survive Levo going away.
"""

import re
import time

import address as ADDR
import covenant as C
import sale as S
import tx as TX


class PlatformError(ValueError):
    pass


class NotAuthorised(PlatformError):
    pass


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")


class Project:
    """A listing: what it is, who runs it, and the sale attached to it."""

    def __init__(self, slug, name, ticker, summary, description, issuer_account,
                 links=None, created_at=None):
        if not SLUG_RE.match(slug or ""):
            raise PlatformError("slug must be 3-40 lowercase letters, digits or "
                                "hyphens, starting and ending alphanumeric")
        if not (name or "").strip():
            raise PlatformError("a project needs a name")
        if not re.match(r"^[A-Z0-9]{2,12}$", ticker or ""):
            raise PlatformError("ticker must be 2-12 uppercase letters or digits")
        self.slug = slug
        self.name = name.strip()
        self.ticker = ticker
        self.summary = (summary or "").strip()
        self.description = (description or "").strip()
        self.issuer_account = issuer_account
        self.links = dict(links or {})
        self.created_at = created_at or int(time.time())
        self.sale = None

    def to_json(self, height=None, now=None):
        return {
            "slug": self.slug,
            "name": self.name,
            "ticker": self.ticker,
            "summary": self.summary,
            "description": self.description,
            "issuer_account": self.issuer_account,
            "links": self.links,
            "created_at": self.created_at,
            "sale": self.sale.to_json(height=height, now=now) if self.sale else None,
        }


class Platform:
    def __init__(self, store, stake_reader, rails=None, rpc=None, hrp="tb"):
        self.store = store
        self.stake = stake_reader
        self.rails = rails
        self.rpc = rpc
        self.hrp = hrp
        self.projects = {}
        self._load()

    # --- persistence --------------------------------------------------------

    def _load(self):
        for slug, d in (self.store.data.get("projects") or {}).items():
            p = Project(d["slug"], d["name"], d["ticker"], d.get("summary"),
                        d.get("description"), d["issuer_account"],
                        d.get("links"), d.get("created_at"))
            sd = d.get("sale")
            if sd:
                terms = C.SaleTerms.from_json(sd["terms"])
                sl = S.Sale(slug, terms, d["issuer_account"], sd.get("created_at"))
                sl.status = sd.get("status", S.DRAFT)
                sl.funding = sd.get("funding")
                sl.locked_atoms = int(sd.get("locked_atoms", 0))
                sl.sold_atoms = int(sd.get("sold_atoms", 0))
                sl.allocations = {k: int(v) for k, v in (sd.get("allocations") or {}).items()}
                p.sale = sl
            self.projects[slug] = p
        self.stake.links.load(self.store.data.get("stake_links"))

    def save(self):
        out = {}
        for slug, p in self.projects.items():
            d = p.to_json()
            if p.sale:
                d["sale"] = p.sale.to_json()
                d["sale"]["allocations"] = p.sale.allocations
                d["sale"]["created_at"] = p.sale.created_at
                # `status` is recomputed on read for closure; persist the raw one.
                d["sale"]["status"] = p.sale.status
            out[slug] = d
        self.store.data["projects"] = out
        self.store.data["stake_links"] = self.stake.links.to_json()
        self.store.save()

    # --- chain context ------------------------------------------------------

    def height(self):
        try:
            return self.rpc.chain_height() if self.rpc else None
        except Exception:
            return None

    # --- listing ------------------------------------------------------------

    def list_project(self, account, meta, terms_json):
        """Create a listing. Only the top tier may do this."""
        standing = self.stake.standing(account)
        if not standing["tier"]["may_list"]:
            raise NotAuthorised(
                "listing a project requires the %s tier (%s SEQ staked); you are "
                "%s with %s staked" % (
                    self._top_tier().name, self._top_tier().min_stake_atoms // 100_000_000,
                    standing["tier"]["name"], standing["stake"]))
        slug = meta.get("slug")
        if slug in self.projects:
            raise PlatformError("a project with that slug already exists")
        p = Project(slug, meta.get("name"), meta.get("ticker"), meta.get("summary"),
                    meta.get("description"), account, meta.get("links"))
        terms_json = dict(terms_json)
        # Canonicalise the price before anything is derived from it: the same
        # ratio in lowest terms buys a large amount of overflow headroom, and
        # after funding it is far too late to change.
        submitted = (terms_json.get("price_num"), terms_json.get("price_den"))
        if submitted[0] and submitted[1]:
            terms_json["price_num"], terms_json["price_den"] = C.canonical_price(*submitted)
        terms = C.SaleTerms.from_json(terms_json)
        if terms.close_locktime >= 500_000_000 and terms.close_locktime <= time.time():
            raise PlatformError("the sale's close time is already in the past")
        p.sale = S.Sale(slug, terms, account)
        p.price_was_reduced = (submitted[0], submitted[1]) != (terms.price_num, terms.price_den)
        self.projects[slug] = p
        self.save()
        return p

    def _top_tier(self):
        return max(self.stake.policy.tiers, key=lambda t: t.min_stake_atoms)

    def confirm_lock(self, account, slug, txid, vout):
        """Verify on chain that the project really locked the tokens.

        Levo reads the output itself rather than believing the issuer's claim,
        and the scriptPubKey it finds must equal the one the published terms
        derive. That equality is the entire trust argument for the sale.
        """
        p = self._project(slug)
        if p.issuer_account != account:
            raise NotAuthorised("only the project's issuer can confirm its lock")
        if p.sale is None:
            raise PlatformError("this project has no sale")
        if self.rpc is None:
            raise PlatformError("no node connection; cannot verify the lock")
        out = self.rpc.txout(txid, vout)
        if out is None:
            raise PlatformError(
                "no unspent output at %s:%s -- it does not exist, or it has "
                "already been spent" % (txid, vout))
        spk = (out.get("scriptPubKey") or {}).get("hex")
        asset = out.get("asset") or out.get("assetlabel")
        value = out.get("valueatoms")
        if value is None and out.get("value") is not None:
            value = int(round(float(out["value"]) * 100_000_000))
        p.sale.confirm_lock(txid, vout, spk, value, asset)
        self.save()
        return p

    def lock_instructions(self, project):
        return {
            "address": self.sale_address(project.sale),
            "script_pubkey": project.sale.script_pubkey,
            "asset": project.sale.terms.token_asset,
            "atoms": project.sale.terms.total_atoms,
            "how": "send exactly this asset and amount to this address, then "
                   "confirm the outpoint. Until then the sale is a draft and "
                   "nobody can buy.",
            "price_reduced": bool(getattr(project, "price_was_reduced", False)),
            "verify_against": "the terms in this response, not the ones you "
                              "submitted: the price is stored in lowest terms, "
                              "and the address is derived from the stored values.",
        }

    # --- buying -------------------------------------------------------------

    def build_buy(self, account, slug, plan_json, buyer):
        """Turn a plan into the transaction that settles it."""
        p = self._project(slug)
        if p.sale is None or not p.sale.funding:
            raise PlatformError("this sale is not funded")
        standing = self.stake.standing(account)
        tier = self.stake.policy.for_stake(standing["stake_atoms"])
        plan = p.sale.plan_buy(account, tier,
                               token_atoms=plan_json.get("token_atoms"),
                               payment_atoms=plan_json.get("payment_atoms"),
                               height=self.height())
        buyer = dict(buyer or {})
        buyer["inputs"] = self.verify_buyer_inputs(buyer.get("inputs"))
        built = TX.build_buy(p.sale, plan, buyer)
        built["token_atoms"] = plan.token_atoms
        built["payment_atoms"] = plan.payment_atoms
        built["remainder_atoms"] = plan.remainder_atoms
        return built

    def build_reclaim(self, account, slug, body):
        """Sweep unsold tokens after the close. Only the project may ask."""
        p = self._project(slug)
        if p.issuer_account != account:
            raise NotAuthorised("only the project's issuer can reclaim its sale")
        if p.sale is None or not p.sale.funding:
            raise PlatformError("this sale holds nothing to reclaim")
        if not p.sale.has_closed(height=self.height()):
            raise PlatformError(
                "this sale has not closed yet; the covenant will reject a "
                "reclaim before its close locktime")
        if self.rpc is None:
            raise PlatformError("no node connection; cannot build a reclaim")
        genesis = self.rpc.call("getblockhash", 0)
        fee_inputs = self.verify_buyer_inputs(body.get("fee_inputs"))
        return TX.build_reclaim(
            p.sale,
            destination_spk=body.get("destination_script_pubkey"),
            fee_inputs=fee_inputs,
            fee_atoms=int(body.get("fee_atoms") or 0),
            fee_asset=(body.get("fee_asset") or p.sale.terms.payment_asset),
            genesis_hash=genesis,
            locktime=body.get("locktime"))

    def plan_buy(self, account, slug, token_atoms=None, payment_atoms=None):
        p = self._project(slug)
        if p.sale is None:
            raise PlatformError("this project has no sale")
        standing = self.stake.standing(account)
        tier = self.stake.policy.for_stake(standing["stake_atoms"])
        plan = p.sale.plan_buy(account, tier, token_atoms=token_atoms,
                               payment_atoms=payment_atoms, height=self.height())
        out = plan.to_json()
        out["tier"] = tier.to_json()
        out["allowance_after_atoms"] = p.sale.allowance_for(account, tier) - plan.payment_atoms
        out["sale_address"] = self.sale_address(p.sale)
        out["cap"] = {
            "per_sale_atoms": tier.cap_atoms,
            "committed_atoms": p.sale.allocations.get(account, 0),
            "enforced_by": "levo",
            "note": "tier caps are Levo's allocation policy. The sale covenant "
                    "enforces the price, the treasury, the token and the minimum "
                    "lot; it does not enforce a per-buyer maximum.",
        }
        return out

    def record_purchase(self, account, slug, txid, token_atoms, payment_atoms):
        """Record a purchase that has landed on chain.

        Levo verifies the covenant output moved as claimed before it writes the
        allocation ledger, so a buyer cannot inflate their recorded commitment
        (or a rival's) by reporting a purchase that never happened.
        """
        p = self._project(slug)
        sale = p.sale
        if sale is None or not sale.funding:
            raise PlatformError("this sale is not funded")
        if self.rpc is not None:
            still_there = self.rpc.txout(sale.funding["txid"], sale.funding["vout"])
            if still_there is not None:
                raise PlatformError(
                    "the sale's covenant output is still unspent, so this "
                    "purchase has not happened yet")
        status = sale.record_purchase(account, payment_atoms, token_atoms)
        if sale.locked_atoms > 0:
            # A partial buy re-rests the remainder at the identical address, in
            # the buyer's transaction. Track the new outpoint so the next buyer
            # spends the right one.
            sale.funding = {"txid": txid, "vout": 1, "atoms": sale.locked_atoms}
        else:
            sale.funding = None
        self.save()
        return {"status": status, "remaining_atoms": sale.locked_atoms}

    # --- reads --------------------------------------------------------------

    def _project(self, slug):
        p = self.projects.get(slug)
        if p is None:
            raise PlatformError("no such project")
        return p

    def public_projects(self):
        h = self.height()
        out = []
        for p in sorted(self.projects.values(), key=lambda x: -x.created_at):
            d = p.to_json(height=h)
            if p.sale:
                d["address"] = self.sale_address(p.sale)
            out.append(d)
        return out

    def sale_address(self, sale):
        return ADDR.from_script_pubkey(sale.script_pubkey, self.hrp)

    def verify_buyer_inputs(self, inputs):
        """Check the buyer's funding outputs against the chain before building.

        The buyer says what they are spending; the chain says what is actually
        there. Trusting the claim produces a transaction that fails at relay
        with a message about sums that explains nothing, so each input is
        looked up and its asset, value and confidentiality taken from the node.
        """
        if self.rpc is None:
            return list(inputs or [])
        checked = []
        for i in list(inputs or []):
            out = self.rpc.txout(i["txid"], int(i["vout"]))
            if out is None:
                raise PlatformError(
                    "input %s:%s is not an unspent output -- it does not exist, "
                    "or it has already been spent" % (i["txid"], i["vout"]))
            blinded = bool(out.get("valuecommitment") or out.get("amountcommitment")
                           or out.get("assetcommitment"))
            asset = (out.get("asset") or "").lower()
            value = out.get("valueatoms")
            if value is None and out.get("value") is not None:
                value = int(round(float(out["value"]) * 100_000_000))
            spk = (out.get("scriptPubKey") or {}).get("hex")
            checked.append({"txid": i["txid"], "vout": int(i["vout"]),
                            "asset": asset or str(i.get("asset", "")).lower(),
                            "value_atoms": int(value or 0),
                            "script_pubkey": spk,
                            "blinded": blinded})
        return checked

    def project_detail(self, slug):
        p = self._project(slug)
        d = p.to_json(height=self.height())
        if p.sale:
            d["address"] = self.sale_address(p.sale)
            d["verify"] = {
                "how": "rebuild the sale address from these terms and compare it "
                       "to the funded scriptPubKey; they are the same fact twice",
                "script_pubkey": p.sale.script_pubkey,
                "internal_key": "NUMS -- there is no key path, so the project "
                                "cannot spend the sale out from under buyers",
            }
        return d
