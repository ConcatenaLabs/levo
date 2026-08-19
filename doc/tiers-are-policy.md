# Tier caps are policy, not consensus

Levo's tiers do two different jobs, and only one of them is enforced by the
chain. Conflating them would be the most consequential lie the interface could
tell, so this note states the split, and the code repeats it wherever a cap is
returned.

## What the covenant enforces

A sale's sell leaf checks, on every spend:

- the asset being sold is the published token, explicitly (a blinded value the
  covenant cannot read is refused outright);
- the payment asset is the published one;
- the treasury output pays at least `ceil(filled * price_num / price_den)`;
- the treasury output goes to the published scriptPubKey;
- `filled >= min_lot`, and any remainder is also `>= min_lot`;
- a remainder returns to the *identical* covenant scriptPubKey.

The reclaim leaf additionally enforces the close locktime and the project's key.

Because these are compiled into the leaves, they are committed inside the
taproot output key; and the internal key is NUMS, so there is no key path. A
funded sale cannot be repriced, redirected or withdrawn -- not by the project,
not by Levo.

## What it does not enforce

**A per-buyer maximum.** There is a floor in the leaf and no ceiling. There is
also no notion of buyer identity in the leaf at all: the sell path is
permissionless, needs no signature, and can be taken by anyone who can build the
transaction, whether or not they have ever seen Levo.

So a tier cap is an allocation policy Levo applies to every purchase it plans.
It is real in the sense that Levo refuses to plan a purchase beyond it and keeps
a cumulative ledger per account per sale. It is not real in the sense that
consensus would stop somebody who ignored Levo entirely.

## Why it is not fixed in the covenant

A sale could be bound to a whitelist by giving each approved buyer its own
tranche, or by requiring a platform signature on the sell path. Both work, and
both cost the property that makes this design worth having: a sale that settles
with no online party, keeps working if Levo disappears, and no operator can
censor. A platform signature in the sell leaf would make Levo able to block a
purchase, which is a longer step than it first looks.

The honest trade is to keep the covenant permissionless and label the caps
accurately.

## Where this is stated in the code

- `levod/sale.py` -- module docstring, and `Sale.plan_buy`.
- `levod/market.py` -- `Platform.plan_buy` attaches `cap.enforced_by = "levo"`.
- `levod/server.py` -- `GET /api/tiers` returns `caps_enforced_by`.
- `web/src/pages/HowItWorks.jsx` -- said in the same words to the reader.
