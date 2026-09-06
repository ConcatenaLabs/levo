"""Amounts for people to read.

Everything Levo computes is in atoms, because atoms are what the chain
understands. Everything Levo says to a person is in the asset's own units,
because "minimum purchase is 1000000000 atoms" tells nobody anything and
"minimum purchase is 10 HLX" tells everybody.
"""

import re


def fmt(atoms, decimals=8, label=""):
    """'1,234.5 USDX' for 123450000000 atoms at 8 decimals."""
    atoms = int(atoms)
    decimals = int(decimals)
    sign = "-" if atoms < 0 else ""
    whole, frac = divmod(abs(atoms), 10 ** decimals)
    text = "%s%s" % (sign, "{:,}".format(whole))
    if decimals and frac:
        text += "." + str(frac).rjust(decimals, "0").rstrip("0")
    return (text + " " + label) if label else text


def parse(text, decimals=8):
    """Atoms for a decimal string such as '12.5', or None if it is not one."""
    s = str(text if text is not None else "").strip()
    # A comma is a thousands separator here and nothing else: "1,000.5" is
    # read, "10,5" is not, because in half the world that means ten and a
    # half, and reading it as a hundred and five would buy ten times what
    # was meant. A comma that is not grouping three digits is refused.
    if "," in s:
        whole = s.split(".", 1)[0]
        if not re.fullmatch(r"[0-9]{1,3}(,[0-9]{3})+", whole):
            return None
        s = s.replace(",", "")
    if not s or s.startswith("-"):
        return None
    if s.count(".") > 1 or any(c not in "0123456789." for c in s):
        return None
    whole, _, frac = s.partition(".")
    if len(frac) > decimals:
        return None
    return int(whole or "0") * 10 ** decimals + int((frac + "0" * decimals)[:decimals] or "0")
