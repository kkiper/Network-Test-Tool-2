"""Interface-name and MAC-address helpers.

IOS prints the same port several ways (``Gi1/3``, ``Gig 1/3``,
``GigabitEthernet1/3``). Everything inside the engine uses the full name, so
every parser runs names through :func:`normalize_interface`.
"""

import re

_PREFIXES = [
    # (short forms, full name). Longest/most specific first.
    (("tengigabitethernet", "tengig", "ten", "te"), "TenGigabitEthernet"),
    (("twentyfivegige", "twe"), "TwentyFiveGigE"),
    (("gigabitethernet", "gig", "gi", "ge"), "GigabitEthernet"),
    (("fastethernet", "fa"), "FastEthernet"),
    (("ethernet", "eth", "et"), "Ethernet"),
    (("vlan", "vl"), "Vlan"),
    (("port-channel", "po"), "Port-channel"),
    (("appgigabitethernet", "ap"), "AppGigabitEthernet"),
]

_NAME_RE = re.compile(r"^\s*([A-Za-z-]+)\s*([0-9][0-9/.:]*)\s*$")


def normalize_interface(name):
    """Return the full IOS name for ``name`` (``Gi1/3`` -> ``GigabitEthernet1/3``).

    Names that don't look like an interface come back unchanged.
    """
    if name is None:
        return None
    m = _NAME_RE.match(name)
    if not m:
        return name.strip()
    prefix, number = m.group(1).lower(), m.group(2)
    for shorts, full in _PREFIXES:
        if prefix in shorts or (len(prefix) >= 2 and full.lower().startswith(prefix)):
            return full + number
    return m.group(1) + number


def short_interface(name):
    """``GigabitEthernet1/3`` -> ``Gi1/3`` (for compact display)."""
    full = normalize_interface(name)
    for short, long in (("Te", "TenGigabitEthernet"), ("Gi", "GigabitEthernet"),
                        ("Fa", "FastEthernet")):
        if full.startswith(long):
            return short + full[len(long):]
    return full


def drawing_label(name):
    """``GigabitEthernet1/10`` -> ``GI 1/10``, matching how the drawings label ports."""
    short = short_interface(name)
    m = re.match(r"^([A-Za-z]+)(\d.*)$", short)
    return "%s %s" % (m.group(1).upper(), m.group(2)) if m else short


def normalize_mac(mac):
    """Return a MAC as ``aa:bb:cc:dd:ee:ff`` whatever format it came in."""
    digits = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    if len(digits) != 12:
        return (mac or "").strip().lower()
    digits = digits.lower()
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))
