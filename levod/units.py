"""Amounts for people to read.

Everything Levo computes is in atoms, because atoms are what the chain
understands. Everything Levo says to a person is in the asset's own units,
because "minimum purchase is 1000000000 atoms" tells nobody anything and
"minimum purchase is 10 HLX" tells everybody.
"""


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
    s = str(text if text is not None else "").strip().replace(",", "")
    if not s or s.startswith("-"):
        return None
    if s.count(".") > 1 or any(c not in "0123456789." for c in s):
        return None
    whole, _, frac = s.partition(".")
    if len(frac) > decimals:
        return None
    return int(whole or "0") * 10 ** decimals + int((frac + "0" * decimals)[:decimals] or "0")
